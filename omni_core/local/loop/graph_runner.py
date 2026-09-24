"""GraphRunnerMixin：ToolLoop 的职责切片（逐字搬移，零逻辑改动）。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
import warnings
import re
from datetime import datetime, timezone
import config
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from omni_core.brain.llm import LLMClient, model_health_ok
from omni_core.brain.prompt import build_system_prompt
from omni_core.brain import tools as brain_tools
from omni_core.local.world_model import WorldModel
from omni_core.local.states import AgentState
from omni_core.local.trajectory import TrajectoryStore
from omni_core.local import telemetry
from omni_core.local.curator import Curator
from omni_core.local.runtime_paths import (
    task_trajectory, task_collected, auto_project_id,
)
from omni_core.local.task_store import TaskStore, ProjectStore

from omni_core.local.loop.parts import (
    VERIFY_TOOL_SCHEMA,
    ESCALATE_TOOL_SCHEMA,
    TASK_DONE_TOOL_SCHEMA,
    RECORD_TOOL_SCHEMA,
    _WORKER_EXTRA_SCHEMAS,
    _strip_after_think,
    _SubtaskGate,
    _ARTIFACT_MAX,
    _ARTIFACT_SHOW_MAX,
    _ARTIFACT_ITEM_CHARS,
    _WORLD_SUMMARY_CHARS,
    _subtask_artifacts,
    _request_fingerprint,
    _recheck_spec,
    _failure_point,
    _format_prev_results,
    _DEFAULT_ESCALATION,
    TaskSpec,
    _safe_str,
    _compact_for_brain,
    _TodoStore,
)


class GraphRunnerMixin:
    def run_task(self, spec: TaskSpec) -> Dict[str, Any]:
        """统一入口：跑一次任务（M10）。

        只有一条路径——「主 agent + 可选派发子 agent」的编排图：
        - 默认单 agent：主 agent 自己把任务做完就结束；
        - 需要并行时主 agent 调 `dispatch`，编排层用 Send 扇出子 agent 并发执行。
        """
        # 绑定当前任务的临时工作目录：脚本 / 截图等临时产物默认落 tasks/<task_id>/tmp/。
        from omni_core.tools.workspace import set_task
        set_task(spec.task_id)
        try:
            return self._run_graph(spec)
        finally:
            WorldModel.release(spec.task_id)
            self._cleanup_task_tmp(spec.task_id)
            set_task(None)

    @staticmethod
    def _cleanup_task_tmp(task_id: str) -> None:
        """任务终态：清理临时产物目录 ``tasks/<task_id>/tmp/``。

        只删 ``tmp/``，**绝不碰同目录的持久资产**（trajectory / world_model / skills…）。
        """
        if not task_id:
            return
        try:
            import shutil
            from omni_core.local.runtime_paths import task_tmp
            shutil.rmtree(task_tmp(task_id), ignore_errors=True)
        except Exception:
            pass

    @staticmethod
    def _normalize_subtasks(raw) -> list:
        """归一化大脑返回的 subtasks（模型无关防御层）。

        不同在线模型对嵌套 tool 参数的序列化习惯不同：
        - 标准: [{"desc":..., "done_when":...}, ...]
        - u2 等: 可能把整个数组二次编码成 JSON 字符串
        - 退化: 单个 dict / 字符串列表 / 纯文本
        统一转成 list[dict]，无法解析则返回 []（由调用方兜底）。
        """
        if raw is None:
            return []
        if isinstance(raw, str):
            s = raw.strip()
            try:
                raw = json.loads(s)
            except Exception:
                # 纯文本描述 → 当作单个子任务
                return [{"desc": s, "done_when": ""}] if s else []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if isinstance(item, str):
                s = item.strip()
                try:
                    parsed = json.loads(s)
                    item = parsed if isinstance(parsed, dict) else {"desc": s, "done_when": ""}
                except Exception:
                    item = {"desc": s, "done_when": ""}
            if isinstance(item, dict) and (item.get("desc") or item.get("done_when")):
                out.append({"desc": str(item.get("desc", "")),
                            "done_when": str(item.get("done_when", ""))})
        return out

    def _run_main(self, spec: TaskSpec, world: WorldModel,
                  store: Optional[TrajectoryStore], prev_results: List[Dict[str, Any]],
                  allow_dispatch: bool, budget: Optional[int] = None,
                  injected: Optional[List[str]] = None) -> Dict[str, Any]:
        """M7 主 agent 跑一轮：默认自己把任务做完；需要并行时产出派发计划。

        - 主 agent 用主模型（`self.brain`），持有全部能力工具 + L2 元工具；
        - 调 `task_done` 且通过完成判定 → done，图直接收尾（默认路径）；
        - 调 `dispatch` → 返回派发计划，图用 Send 扇出给子 agent 并发执行。
        """
        if self._is_stop_requested():
            return {"done": False, "plan": [], "steps": 0, "reason": "用户主动停止"}

        user = self._build_user(spec, world, step=0)
        # U5b：历史子任务结果 -> 产物 / 校验结果 / 失败点位 + 世界状态摘要
        if prev_results:
            try:
                _ws = world.summary() or ""
            except Exception:
                _ws = ""
            _block = _format_prev_results(prev_results, _ws)
            user = (f"{user}\n\n{_block}"
                    + "\n请基于以上结果继续；达成目标后可直接给出结论收尾，或调用 task_done。")
        # M8 软注入：人类纠偏优先级最高，追加在本轮用户消息末尾（不打断当前 turn）
        for note in (injected or []):
            user = f"{user}\n\n【人类指示（最高优先级）】{note}"

        # M10：长任务压缩（原「单大脑」路径的能力，统一入口后主 agent 一样要吃）
        compress_after = 0
        if self.brain_long_task.get("enabled"):
            compress_after = int(self.brain_long_task.get("max_turns", 16))
        # D1/D2：token 占比触发压缩（抢在 context rot 前）；无占比配置退化为轮数。
        # 大脑走压缩（不硬裁），故 history_keep=0。
        _lt = self.brain_long_task or {}
        compress_threshold = float(_lt.get("compress_threshold", 0.0) or 0.0)
        hard_ceiling = float(_lt.get("hard_ceiling", 0.0) or 0.0)
        ctx_window = int(_lt.get("context_window", 0) or 0)
        # T2.2 Prune-First：压缩前对超长 tool 输出做首尾截断（0 = 关闭，零干预）
        _prune_cfg = _lt.get("prune") or {}
        # 缺省取代码常量（config.CONTEXT_DEFAULTS）；显式配 0 = 关闭
        _ctx_def = config.CONTEXT_DEFAULTS.get("brain") or {}
        _prune_def = (_ctx_def.get("long_task") or {}).get("prune") or {}
        prune_threshold = int(_prune_cfg.get("threshold_chars", _prune_def.get("threshold_chars", 0)) or 0)
        prune_head = int(_prune_cfg.get("head_chars", _prune_def.get("head_chars", 0)) or 0)
        prune_tail = int(_prune_cfg.get("tail_chars", _prune_def.get("tail_chars", 0)) or 0)
        # T3.2（U1b+）：模型级输入预算（缺省取代码常量；显式配 0 = 关闭粘性压缩）
        # + 压缩后尾部保留比例（retain_ratio）；阈值由 run_subtask_sdk 动态计算。
        _mit = self.brain_cfg.get("maxInputTokens", None)
        if _mit is None:
            _mit = self.brain_cfg.get("max_input_tokens", None)
        max_input_tokens = int(_mit) if _mit is not None else int(_ctx_def.get("maxInputTokens", 0) or 0)
        retain_ratio = float(_lt.get("retain_ratio", 0.0) or 0.0)
        _on_llm, _on_llm_delta, _on_llm_turn_end = self._make_llm_emitter("brain", store)
        from omni_core.local.runtime_paths import task_dir, task_skills, global_skills
        _runtime_ctx = {
            "task_id": spec.task_id,
            "env_kind": self.exec.kind,
            "env_platform": getattr(self.exec, "platform", ""),
            "task_dir": str(task_dir(spec.task_id)),
            "task_skills_dir": str(task_skills(spec.task_id)),
            "global_skills_dir": str(global_skills()),
        }
        system_prompt = build_system_prompt(
            self.brain_capabilities, self.tool_schemas, runtime_context=_runtime_ctx,
        )
        # §3.4 知识层弱注入（默认关闭；全量 try 兜底，绝不阻塞任务执行）
        # T2.4（O4'）：技能摘要不再注入 system prompt——改由新机制提供：
        # 技能目录以固定模板 User 消息注入（knowledge_inject.build_skill_catalog），
        # 模型按需调用 load_skill 工具加载完整指令（omni_core.tools.skill_tool.load_skill）。
        # 历史记忆注入逻辑保留；F4.2（KJ-1）：memory_in_user=true 时迁至尾部重插（见
        # _build_memory_injection），此处仅作 false 回退（一键回退到 system 注入）。
        # 注：纪律文件（AGENTS.md）**不在此处**——F4.1b 起由 _run_via_sdk 统一在 run 起始
        # 读快照后并入 system 尾部（同样作用于子任务），此处不重复拼接。
        if self.knowledge_cfg["memory"] and self._memory_in_system():
            try:
                from omni_core.local.knowledge_inject import (
                    build_knowledge_block, load_memory_text,
                )
                mem_text = load_memory_text()
                block = build_knowledge_block(mem_text, [])
                if block:
                    system_prompt = system_prompt + "\n\n" + block
                    # T4.6（O5+）：知识注入落轨迹（字符数 / 技能数）
                    self._log_knowledge_injection(store, {
                        "kind": "knowledge_injected",
                        "chars": len(block),
                        "skills": 0,
                        "memory": bool(mem_text),
                    })
                    dbg = self._dbg("knowledge")
                    if dbg:
                        dbg("knowledge_injected", {
                            "chars": len(block),
                            "memory": bool(mem_text),
                        })
            except Exception as e:
                self._log(f"knowledge inject skipped: {type(e).__name__}: {e}")
        # F2.4：主链墙钟对齐——取自 runtime.long_task.wallclock_sec（缺省 0=不检查）。
        # 与子任务墙钟（runtime.escalation.wallclock_sec，缺省 120s，worker 防打转）解耦。
        main_wallclock_sec = self._resolve_main_wallclock(budget, spec)
        res = self._run_via_sdk(
            spec, self.brain,
            system_prompt,
            world, max_steps=budget, allow_escalate=False, traj=store,
            wallclock_sec=main_wallclock_sec,
            allow_dispatch=allow_dispatch, user_input=user,
            budget_hint_ratio=self.budget_hint_ratio,
            compress_after=compress_after,
            history_keep=0,
            compress_threshold=compress_threshold,
            hard_ceiling=hard_ceiling,
            ctx_window=ctx_window,
            prune_threshold=prune_threshold,
            prune_head=prune_head,
            prune_tail=prune_tail,
            max_input_tokens=max_input_tokens,
            retain_ratio=retain_ratio,
            history_items=list(getattr(spec, "history", None) or []),
            on_llm=_on_llm, on_llm_delta=_on_llm_delta, on_turn_end=_on_llm_turn_end,
        )
        return {
            "done": bool(res.get("success")),
            "plan": list(res.get("dispatch_plan") or []),
            "steps": int(res.get("steps", 0) or 0),
            "reason": res.get("reason", "") or "",
            "escalated": bool(res.get("escalated")),
            "escalate_reason": res.get("escalate_reason", "") or "",
            "provider_error": bool(res.get("provider_error")),
            "budget_exhausted": "budget_exhausted" in (res.get("reason") or ""),
        }

    def _run_graph(self, spec: TaskSpec) -> Dict[str, Any]:
        """M10 统一实现：去分层的通用多 agent 编排（默认单 agent 完成，主 agent 决定派发）。

        不再是「planner 规划 + worker 执行」的固定两层：
        - 主 agent（主模型）自己跑任务，默认跑到完成；
        - 需要并行时主 agent 调 `dispatch` 产出派发计划，编排层用 LangGraph `Send`
          扇出给子 agent（执行模型）**并发**执行；
        - 子 agent 结果聚合进共享黑板并回灌主 agent，轮次由 `max_rounds` 兜底。
        """
        if self.executor is None:
            raise RuntimeError("executor 未初始化（不应发生）：ToolLoop 构造异常")
        self._reset_run_counters()
        self._set_state(AgentState.INIT)
        run_id = uuid.uuid4().hex[:12]          # 统一 run_id（B2）
        # 契约自洽修复：TaskSpec.task_id 声明为「空则由调用方（/run）生成」，本方法
        # 下方也明确把空 task_id 当「无身份独立实例」处理（不 load 磁盘残留）。但资产
        # 落盘路径 tasks/<task_id>/ 要求合法标识符，空值会让 task_dir 抛
        # ValueError，整条编排以「编排异常」收场——两处语义不自洽。此处以 run_id
        # 派生一次性合法 identity，使空 task_id 真正可用：不与任何历史任务共享磁盘
        # 资产（独立实例语义），同时保留轨迹 / world-model 的落盘归属。
        if not (spec.task_id or "").strip():
            spec = replace(spec, task_id=f"t_{run_id}")
        # B2：透传统一 run_id（1885 行生成，早于本调用），确保轨迹落盘 ID 同源
        store = self._make_store(spec.task_id, run_id)
        # M4b.1: 持久 world_model（无状态大脑注入 + 子目标 checkpoint + run 结束 save）
        # M6: 共享黑板——主 agent 与各子 agent 都落在同一实例上
        world = WorldModel.shared(spec.task_id)
        # A2：跨 run 续上——先恢复磁盘上已有的事实/备注/采集/目标/状态，
        # 否则 release() 每轮 pop + 不 load 会丢失上一轮记下的所有东西（Part1/N8 基础）。
        # 仅当 task_id 非空才 load：空 task_id 是无身份独立实例，不应拉取磁盘残留。
        if spec.task_id:
            try:
                world.load()
            except Exception:
                pass
        world.run_id = run_id
        # F2.1：当前 turn 用户输入为 objective 唯一权威源。新 run 开始无条件用本轮
        # 输入覆盖 world.objective（解决「续跑残留上一轮 objective 导致重复开局侦察」）。
        # 防御：objective 超长（>2000，疑似整段 system prompt 被塞入的污染形态）时
        # 拒绝持久化到 world_model.md frontmatter（不污染磁盘），本 run 仍用本轮输入。
        _OBJ_MAX = 2000
        world.objective = spec.objective
        world._objective_overlong = bool(spec.objective) and len(spec.objective) > _OBJ_MAX
        # B1：N8 崩溃/重启续跑——读最新 checkpoint，把已完成子目标+当前世界进度
        # 注入首轮提示，避免重复已完成部分、丢数小时进度。无 checkpoint 则跳过。
        try:
            _cps = WorldModel.list_checkpoints(spec.task_id)
            if _cps:
                _cp = WorldModel.load_checkpoint(spec.task_id, _cps[0])
                if _cp:
                    _sg = (_cp.get("subgoal") or {}).get("desc", "")
                    # 只陈述事实（已完成什么 + 当前进度），**不下命令**：
                    # 「接着推进 / 不要重复」属模型自主判断，loop 不替模型决策
                    # （同一原则见 core._build_user：任务消息只放事实）。
                    world._resume_hint = (
                        f"（续跑上下文）上一轮已完成子目标「{_sg}」。\n"
                        f"当前世界进度：\n{world.summary()[:600]}"
                    )
        except Exception:
            pass

        from omni_core.async_bridge import create_task, wait_task
        from omni_core.orchestration.graph import build_agent_graph

        _dispatch_cfg = ((self._cfg).get("runtime") or {}).get("dispatch") or {}
        max_rounds = int(_dispatch_cfg.get("max_rounds", 3))
        max_parallel = int(_dispatch_cfg.get("max_parallel", 4))
        # M-fix：executor 即主模型（未配置/不可达回退）时，无独立 worker 可派发，
        # 关闭 dispatch 让大脑「知道」并自行完成，避免空派发。
        allow_dispatch = bool(_dispatch_cfg.get("enabled", True)) and not self.executor_is_planner

        # 注入执行单元（闭包捕获 self / world / store / spec，复用既有逻辑）
        def _main_fn(state, prev_results, injected=None):
            # 主 agent 本轮预算 = 总预算 - 已用（子 agent 也从这个池子里分）
            budget = None
            if spec.max_steps:
                budget = max(1, int(spec.max_steps) - int(state.get("budget_used", 0)))
            # 软注入：图状态（恢复用） + 运行期队列（运行中投递）合并后交给主 agent
            notes = list(injected or []) + self._drain_injections()
            # T4.4（U5c）：子任务回收完成、下一轮执行前，对标记需复核的批次二次校验
            self._recheck_batch(prev_results, world)
            return self._run_main(spec, world, store, prev_results, allow_dispatch,
                                  budget, notes)

        def _sub_fn(item, budget):
            inner = TaskSpec(
                objective=item.get("desc", ""),
                done_when=item.get("done_when", ""),
                expected=item.get("done_when") or None,
                task_id=spec.task_id,
                project_id=spec.project_id,
            )
            res = self._run_subtask(
                inner, store, parent_world=world,
                parent_objective=spec.objective, max_steps=budget,
                board_source=inner.objective[:40],
            )
            # 子任务成功即写 checkpoint（失败/升级不写，避免恢复到一个坏状态）
            if res.get("success"):
                world.checkpoint({"desc": item.get("desc", ""),
                                  "done_when": item.get("done_when", "")})
            # T4.4（U5c）：把「需复核」标记与完成条件带回编排层（供下一轮二次校验）
            res = dict(res or {})
            res["desc"] = item.get("desc", "")
            res["done_when"] = item.get("done_when", "")
            if item.get("need_verify"):
                res["need_verify"] = True
            return res

        def _finalize_fn(state, success, reason, steps, rounds, escalated, escalate_reason,
                         provider_error=False):
            # 仅回填计数 + 返回核心字段；落盘/Curator 由下方 _finish 统一处理
            self.step = steps or self.step
            if escalated:
                self._failures = max(self._failures, 1)
            # 子 agent 结果摘要（供 API/排障：谁派了什么、各自成败）
            subtask_results = [
                {"desc": r.get("desc", ""), "success": bool(r.get("success")),
                 "reason": r.get("reason", "") or "",
                 # T4.3（U5a）：交接面字段透传至任务汇总
                 "done_when_hit": bool(r.get("done_when_hit")),
                 "artifacts": list(r.get("artifacts") or []),
                 "failure_point": str(r.get("failure_point") or "")}
                for r in (state.get("results") or [])
            ]
            return {
                "success": success,
                "reason": reason,
                "steps": steps,
                "escalated": escalated,
                "escalate_reason": escalate_reason,
                "provider_error": bool(provider_error),
                "rounds": rounds,
                "subtask_results": subtask_results,
            }

        # M8：checkpointer 是软注入（update_state）与崩溃恢复的载体；
        # thread_id 用 task_id，使同一任务的注入能落到正在跑的那条 run 上。
        try:
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
        except Exception:      # 缺依赖时退化为无 checkpointer（注入不可用，其余行为不变）
            checkpointer = None
            self._log("[graph] checkpointer 不可用，软注入将不可用")

        graph = build_agent_graph(
            run_main_fn=_main_fn,
            run_sub_fn=_sub_fn,
            finalize_fn=_finalize_fn,
            max_rounds=max_rounds,
            max_parallel=max_parallel,
            checkpointer=checkpointer,
        )

        self.step = 0
        self._set_state(AgentState.EXECUTING)
        init_state = {
            "objective": spec.objective,
            "done_when": spec.done_when,
            "task_id": spec.task_id,
            "project_id": spec.project_id,
            "max_steps": spec.max_steps,
            "round": 0,
            "plan": [],
            "results": [],
            "consumed": 0,
            "injected": [],
            "injected_seen": 0,
            "budget_used": 0,
            "done": False,
        }
        # 暴露给外部：软注入（update_state）/ 硬停止（cancel）都要用到
        self._graph = graph
        self._graph_config = {"configurable": {"thread_id": spec.task_id or run_id}}

        try:
            # 必须 ainvoke：子 agent 节点是 async，同步 invoke 会退化成串行
            self._run_task = create_task(graph.ainvoke(init_state, self._graph_config))
            final = wait_task(self._run_task)
        except asyncio.CancelledError:
            self._log("[graph] 用户硬停止，运行被取消")
            world.save()
            base = self._finish(False, "用户主动停止", self.step, store, spec)
            base["escalated"] = False
            base["escalate_reason"] = ""
            base["collected"] = [dict(c) for c in world.collected]
            base["notes"] = list(world.notes)
            base["collected_count"] = len(world.collected)
            return base
        except Exception as e:
            # 硬停止到达的形态不止一种（asyncio.CancelledError /
            # concurrent.futures.CancelledError），统一按「用户主动停止」处理
            if type(e).__name__ == "CancelledError":
                self._log("[graph] 用户硬停止，运行被取消")
                world.save()
                stopped = self._finish(False, "用户主动停止", self.step, store, spec)
                stopped["escalated"] = False
                stopped["escalate_reason"] = ""
                stopped["collected"] = [dict(c) for c in world.collected]
                stopped["notes"] = list(world.notes)
                stopped["collected_count"] = len(world.collected)
                return stopped
            self._log(f"[graph] 编排异常: {type(e).__name__}: {e}")
            world.save()
            base = self._finish(False, f"编排异常: {e}", self.step, store, spec)
            base["escalated"] = False
            base["escalate_reason"] = ""
            base["collected"] = [dict(c) for c in world.collected]
            base["notes"] = list(world.notes)
            base["collected_count"] = len(world.collected)
            return base

        res = final.get("result", {})
        success = bool(res.get("success", False))
        reason = res.get("reason", "未达成目标")
        escalated = bool(res.get("escalated", False))
        escalate_reason = res.get("escalate_reason", "")
        # U3：provider 服务侧错误标记——命中则任务置 paused，不写 finished_at
        provider_error = bool(res.get("provider_error", False))
        # M7：派发轮次与子 agent 结果摘要（供 API / 排障）
        rounds = int(res.get("rounds", 0) or 0)
        subtask_results = list(res.get("subtask_results") or [])
        self._set_state(AgentState.DONE if success else AgentState.FAILED)
        world.save()

        base = self._finish(success, reason, self.step, store, spec)
        base["escalated"] = escalated
        base["escalate_reason"] = escalate_reason
        base["provider_error"] = provider_error
        base["rounds"] = rounds
        base["subtask_results"] = subtask_results
        # M5: 把采集结果随运行结果一并返回，并落盘 tasks/<task_id>/collected.json
        base["collected"] = [dict(c) for c in world.collected]
        base["notes"] = list(world.notes)
        base["collected_count"] = len(world.collected)
        try:
            if spec.task_id:
                cpath = task_collected(spec.task_id)
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps({
                    "run_id": world.run_id,
                    "task_id": spec.task_id,
                    "objective": spec.objective,
                    "collected": base["collected"],
                    "notes": base["notes"],
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                base["collected_json"] = str(cpath)
        except Exception as e:
            self._log(f"[collect] 结果落盘失败（不影响主流程）: {type(e).__name__}: {e}")

        # task 索引：tasks/<task_id>/task.json（前端历史 task list 来源）
        # 仅当 task_id 合法时更新索引；空 task_id（如单元测试 mock）跳过，不阻断主流程。
        try:
            if spec.task_id:
                from omni_core.local.runtime_paths import validate_identifier
                validate_identifier(spec.task_id, "task_id")
                # U3：provider 服务侧错误命中时，任务置 paused 且不写 finished_at
                # （保留运行记录不变），由用户补充配额/稍后重试后从断点续跑。
                if provider_error:
                    TaskStore.update(
                        spec.task_id,
                        state="paused",
                        success=bool(base.get("success", False)),
                    )
                else:
                    TaskStore.update(
                        spec.task_id,
                        state=str(base.get("state", "")) or ("done" if base.get("success") else "failed"),
                        success=bool(base.get("success", False)),
                        finished_at=datetime.now(timezone.utc).isoformat(),
                    )
                # B2（2026-09-22）：runs 数组改为追加（去重）写入，避免覆盖历史运行
                # 记录 / 产生悬空 run_id。复用 TaskStore.add_run 原生去重 + 追加能力。
                TaskStore.add_run(spec.task_id, world.run_id)
        except Exception as e:
            self._log(f"[task] 索引落盘失败（不影响主流程）: {type(e).__name__}: {e}")
        return base

    def _run_subtask(self, inner_spec: TaskSpec, traj: Optional[TrajectoryStore] = None,
                     parent_world: Optional[WorldModel] = None,
                     parent_objective: str = "",
                     max_steps: Optional[int] = None,
                     board_source: str = "") -> Dict[str, Any]:
        """起一个本地模型内层 loop（独立消息上下文，走 self.executor）。

        max_steps: 本子任务允许的最大步数（由统一入口 run_task 从总预算分配）。
        """
        # M6：子 agent 仍用独立实例跑（避免并发下 percept/动作 ring buffer 互串），
        # 结束后统一 flush 到共享黑板，并带来源标记。
        sub_world = WorldModel()
        dbg = self._dbg("subtask")
        if dbg:
            dbg("subtask_start", {
                "title": "子任务开始",
                "objective": inner_spec.objective,
                "executor_model": self.exec_model,
                "is_planner": self.executor_is_planner,
            })
        t0 = time.time()
        _on_llm, _on_llm_delta, _on_llm_turn_end = self._make_llm_emitter("executor")
        from omni_core.local.runtime_paths import task_dir, task_skills, global_skills
        _exec_runtime_ctx = {
            "task_id": inner_spec.task_id,
            "env_kind": self.exec.kind,  # 与主链一致：worker 也要知道自己所在环境
            "env_platform": getattr(self.exec, "platform", ""),
            "task_dir": str(task_dir(inner_spec.task_id)),
            "task_skills_dir": str(task_skills(inner_spec.task_id)),
            "global_skills_dir": str(global_skills()),
        }
        res = self._run_via_sdk(
            inner_spec, self.executor,
            build_system_prompt(
                self.executor_capabilities, self.tool_schemas,
                runtime_context=_exec_runtime_ctx,
            ),
            sub_world, max_steps=max_steps, allow_escalate=True,
            wallclock_sec=float(self.escalation.get("wallclock_sec", 0) or 0),
            is_sub=True, board_source=board_source,
            budget_hint_ratio=self.budget_hint_ratio,
            history_keep=int(self.escalation.get("worker_history_keep", 3) or 0),
            on_llm=_on_llm, on_llm_delta=_on_llm_delta, on_turn_end=_on_llm_turn_end,
        )
        if dbg:
            dbg("subtask_end", {
                "title": "子任务结束",
                "success": bool(res.get("success")),
                "escalated": bool(res.get("escalated")),
                "reason": res.get("reason", ""),
                "steps": int(res.get("steps", 0) or 0),
            })
        # M2: 补齐编排层所需指标（供 OrchestrationPolicy 决策）
        res = dict(res)
        res["verify_fail"] = getattr(sub_world, "_last_verify_fail", 0)
        res["elapsed"] = time.time() - t0
        # T4.3（U5a）：交接面三件套——完成条件校验结果 / 关键产物 / 失败点位
        try:
            res["done_when_hit"] = bool(self._verify(inner_spec, sub_world)[0])
        except Exception:
            res["done_when_hit"] = bool(res.get("success"))
        res["artifacts"] = _subtask_artifacts(sub_world)
        res["failure_point"] = _failure_point(res)
        # M4b.1: 子任务结束后，将其环境状态回传给父 world_model。
        # 注意：失败/升级也要回传（第五轮真机暴露——只在成功时回传会让
        # 大脑反思时看到空状态，误判目标应用未打开而放弃）。
        # M6: 回传 = flush 到共享黑板，facts 带来源（子任务目标），供 view(scope) 溯源。
        if parent_world is not None and sub_world.state_text:
            parent_world.update(sub_world.current_percept, self.exec)
        if parent_world is not None:
            # M5: 子任务采集到的条目/备注也要回传（否则父 world 看不到采数据，整任务会误判漏采）
            parent_world.merge_collection(sub_world, source=inner_spec.objective[:40])
        return res

