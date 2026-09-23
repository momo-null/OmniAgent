"""M7：去分层的通用多 agent 编排图（设计 §2）。

与旧图的差别：
- **没有 Manager/Worker 角色分层**：图上只有一个通用节点语义「跑某个 agent」。
- **默认单 agent**：主 agent 自己把任务做完就结束（不派发 = 不扇出）。
- **是否拆子 agent 由主 agent 决定**：主 agent 调 `dispatch` 元工具产出计划，
  编排层把它翻译成 LangGraph `Send` 扇出，子 agent **并发**执行。
- **结果聚合后回到主 agent**：子 agent 结果经 `results`（add reducer）聚合，
  主 agent 下一轮消费这些结果后自行决定继续 / 派发 / 收尾。
- **轮次兜底**：`max_rounds` 限制「派发 → 回收」的循环次数，防无限派发。

并行是硬要求的部分：
- 子 agent 节点是 `async def`，内部用 `asyncio.to_thread` 承载同步执行体
  （同步体内部会把 LLM 调用提交到共享 event loop），因此多个子 agent 真并发，
  而不是被 LangGraph 串行调度。
  图必须用 `ainvoke` 驱动；用同步 `invoke` 会退化成串行。
"""
from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, END
from langgraph.types import Send

# 主 agent 一轮的产出（由 ToolLoop 注入）
# 入参：(state, 上一轮子任务结果, 未消费的人类注入消息)
MainFn = Callable[[Dict[str, Any], List[Dict[str, Any]], List[str]], Dict[str, Any]]
# 子 agent 执行一个派发项
SubFn = Callable[[Dict[str, Any], Optional[int]], Dict[str, Any]]
FinalizeFn = Callable[..., Dict[str, Any]]


class OmniState(TypedDict, total=False):
    """图在节点间传递的共享状态（纯数据，无领域逻辑）。"""

    objective: str
    done_when: str
    task_id: str
    project_id: str
    max_steps: Optional[int]

    round: int                    # 主 agent 已跑轮次（1 = 首轮）
    plan: List[Dict[str, str]]    # 本轮派发计划（主 agent 产出）
    finish_reason: str            # 主 agent 给出的结束原因

    # 子 agent 结果：add reducer 负责并发聚合（LangGraph map-reduce 惯例）
    results: Annotated[List[Dict[str, Any]], operator.add]
    consumed: int                 # 主 agent 已消费到哪条结果

    # M8 软注入：人类纠偏消息（add 累加，主 agent 按游标消费；不打断当前 turn）
    injected: Annotated[List[str], operator.add]
    injected_seen: int

    done: bool
    budget_used: int
    budget_exhausted: bool
    last_steps: int
    last_escalated: bool
    last_escalate_reason: str
    last_provider_error: bool     # U3：主 agent 本轮是否命中 provider 服务侧错误
    result: Dict[str, Any]        # 终态结果（结束节点写入）


def build_agent_graph(
    *,
    run_main_fn: MainFn,
    run_sub_fn: SubFn,
    finalize_fn: FinalizeFn,
    max_rounds: int = 3,
    max_parallel: int = 4,
    checkpointer: Any = None,
) -> Any:
    """装配并编译「主 agent + Send 扇出子 agent」的编排图。

    零领域逻辑、零工具持有：所有执行体由调用方注入，本图只表达拓扑与并发。

    Args:
        run_main_fn: 跑一轮主 agent，返回 ``{done, plan, steps, reason, escalated, ...}``。
        run_sub_fn: 跑一个派发项，返回子任务结果字典。
        finalize_fn: 收尾，写入 ``result``。
        max_rounds: 「派发 → 回收」轮次上限（超过则收尾，防无限派发）。
        max_parallel: 单轮最多并发多少个子 agent。
        checkpointer: M8 软注入 / 崩溃恢复的载体（`update_state` 需要它）。
    """
    max_rounds = max(1, int(max_rounds))
    max_parallel = max(1, int(max_parallel))

    def _main(state: OmniState) -> Dict[str, Any]:
        """主 agent 节点：默认自己做完；需要时产出派发计划。"""
        prev = list(state.get("results", []))[int(state.get("consumed", 0)):]
        # M8 软注入：把未消费的人类纠偏消息交给主 agent（不打断它当前这轮）
        pending = list(state.get("injected", []))[int(state.get("injected_seen", 0)):]
        out = run_main_fn(state, prev, pending) or {}

        steps = int(out.get("steps", 0) or 0)
        plan = list(out.get("plan") or [])
        if len(plan) > max_parallel:
            plan = plan[:max_parallel]

        return {
            "round": int(state.get("round", 0)) + 1,
            "plan": plan,
            "done": bool(out.get("done")),
            "finish_reason": out.get("reason", "") or "",
            "budget_used": int(state.get("budget_used", 0)) + steps,
            "budget_exhausted": bool(out.get("budget_exhausted")),
            "last_steps": steps,
            "last_escalated": bool(out.get("escalated")),
            "last_escalate_reason": out.get("escalate_reason", "") or "",
            "last_provider_error": bool(out.get("provider_error")),
            "consumed": len(state.get("results", [])),
            "injected_seen": len(state.get("injected", [])),
        }

    async def _sub(state: OmniState) -> Dict[str, Any]:
        """子 agent 节点（并发）：一个派发项 = 一个 agent。

        用 `asyncio.to_thread` 承载同步执行体 —— 保证多个子 agent 真并发，
        且同步体内部的 `run_async`（共享 event loop）不会把 loop 线程堵死。
        """
        item = dict(state.get("item") or {})
        res = await asyncio.to_thread(run_sub_fn, item, state.get("item_budget"))
        payload = dict(res or {})
        payload.setdefault("desc", item.get("desc", ""))
        return {"results": [payload]}

    def _route(state: OmniState) -> Any:
        """主 agent 之后：完成 → 收尾；有派发计划 → Send 扇出；否则收尾。"""
        if state.get("done"):
            return "finalize"
        plan = state.get("plan") or []
        if not plan:
            return "finalize"
        if int(state.get("round", 0)) > max_rounds:
            return "finalize"
        if state.get("budget_exhausted"):
            return "finalize"

        remaining: Optional[int] = None
        max_steps = state.get("max_steps")
        if max_steps:
            remaining = max(1, int(max_steps) - int(state.get("budget_used", 0)))
        per = max(1, (remaining // len(plan))) if remaining else None

        return [
            Send("sub", {
                "item": it,
                "item_budget": per,
                "objective": state.get("objective", ""),
                "task_id": state.get("task_id", ""),
                "project_id": state.get("project_id"),
            })
            for it in plan
        ]

    def _finalize(state: OmniState) -> Dict[str, Any]:
        """收尾节点：成功由主 agent 的完成判定决定（不再有内核里的假成功词表）。"""
        if state.get("result"):
            return {}
        done = bool(state.get("done"))
        reason = state.get("finish_reason") or ("主 agent 完成" if done else "主 agent 未达成目标")
        return {
            "result": finalize_fn(
                state,
                success=done,
                reason=reason,
                steps=int(state.get("budget_used", 0)),
                rounds=int(state.get("round", 0)),
                escalated=bool(state.get("last_escalated")),
                escalate_reason=state.get("last_escalate_reason", ""),
                provider_error=bool(state.get("last_provider_error", False)),
            )
        }

    g = StateGraph(OmniState)
    g.add_node("main", _main)
    g.add_node("sub", _sub)
    g.add_node("finalize", _finalize)

    g.set_entry_point("main")
    # 主 agent → 收尾 / Send 扇出（扇出项跑完自动回到 main 的下一轮）
    g.add_conditional_edges("main", _route, {"finalize": "finalize", "sub": "sub"})
    g.add_edge("sub", "main")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
