"""tool_loop 拆分：模块级数据结构、任务规格与辅助函数。

原 omni_core/local/tool_loop.py 的纯函数/类/常量，逐字搬移，零逻辑改动。
"""
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

"""主控制循环（tool_loop）：把控制流完全交给大脑，本地只做编排与派发。

循环形态（单大脑 / 兼容旧行为）：
    observe → 组 payload → brain.chat(tools) → 派发 tool 调用 → observe → ...

M10 统一入口（run_task，见 doc/plans/multi-agent-redesign-2026-09-13.md）：
    主 agent 自己跑任务，默认跑到完成；需要并行时调 dispatch → Send 扇出子 agent
    （旧的「单大脑直跑」与「两层编排」两条路径已合并）：

    main agent（主模型，可用全部工具 + dispatch）
      ├─ 不派发 → 自己做完 → task_done → 结束
      └─ dispatch(items) → Send 扇出 N 个子 agent（并发）→ 结果回灌 → 主 agent 继续

    for each subtask（示意，已由编排图接管）：
        本地模型（executor / worker）跑内层 ReAct loop（走同一套 ToolRegistry/Vision/Execution）
        成功 → 回报大脑；触发升级条件 → 暂停并 escalate 回在线大脑反思
    在线大脑在边界/失败升级时反思 → 重规划剩余 / 收尾

设计红线：
- 内核（ToolLoop + 通用闭环）零场景硬编码；能力==工具。
- 大脑（manager）只做规划，所有子任务按架构约定一律走本地模型（worker）；模型选择由结构固定，
  大脑不做「模型路由」实时判断。
- 升级阈值全部来自 config（runtime.escalation.*），不在内核硬编码。
"""
VERIFY_TOOL_SCHEMA = brain_tools.VERIFY_TOOL_SCHEMA
ESCALATE_TOOL_SCHEMA = brain_tools.ESCALATE_TOOL_SCHEMA
TASK_DONE_TOOL_SCHEMA = brain_tools.TASK_DONE_TOOL_SCHEMA
RECORD_TOOL_SCHEMA = brain_tools.RECORD_TOOL_SCHEMA
_WORKER_EXTRA_SCHEMAS = list(brain_tools.WORKER_EXTRA_SCHEMAS)
def _strip_after_think(text: str) -> str:
    """剥掉 <think>...</think> 思考段，返回其后正文。

    - 有闭合标签：取最后一个 </think> 之后的内容；
    - 有开标签未闭合（流式中间态）：返回空（尚无正文）；
    - 无标签：原样返回（strip）。
    """
    t = text or ""
    end = t.rfind("</think>")
    if end != -1:
        return t[end + len("</think>"):].strip()
    if "<think>" in t:
        return ""
    return t.strip()
class _SubtaskGate:
    """L2 门控：完成判定 / 备注写入（供 SDK 元工具 task_done / verify / record 调用）。

    属 L2 护城河（设计 §3.5）：不进框架循环，只在框架回调它时做 L2 决策。
    完成判定统一走 backend.verify_done（去场景化 §9，内核不读感知字段）。

    验证档位（2026-09-17 用户确认，方案 a）：删 LLM-as-judge——judge 验证的是
    world.notes（record 的形式产物）而非真实产出，验证对象错了；内嵌验证四层
    保留（提示纪律4 / verify 工具 / done_when 字面匹配 / 方案 B 收尾提示）。
    """

    def __init__(self, loop, spec, world) -> None:
        self.loop = loop
        self.spec = spec
        self.world = world
        self.verify_count = 0
        self.no_confidence = False
        # T4.6（O5+）：最近一次校验结果（供轨迹「校验状态真值化」读取）
        self.last_verify_passed: Optional[bool] = None

    def verify(self, condition: str = ""):
        """校验当前环境是否达成条件；失败累加 verify_count（供升级阈值判断）。"""
        passed, why = self.loop._verify(self.spec, self.world, condition=condition)
        self.last_verify_passed = bool(passed)
        if not passed:
            self.verify_count += 1
        return passed, why

    @property
    def has_condition(self) -> bool:
        # 只认显式完成条件（expected/done_when）。objective 不纳入：
        # 单链路统一后闲聊也走 run_task，objective 恒非空——若纳入则闲聊
        # 无法自然收尾（会被「无实质产出」拒收）。无条件 → 信任大脑。
        return bool(self.spec.expected or self.spec.done_when)

    def peek(self):
        """只看不记账的完成判定（显式完成条件短路用，不计 verify_count）。"""
        passed, why = self.loop._verify(self.spec, self.world)
        self.last_verify_passed = bool(passed)
        return passed, why

    def verify_done(self):
        """task_done 门控：有明确完成条件则字面校验，否则信任大脑（防误伤）。"""
        if not self.has_condition:
            return True, "无校验条件，信任大脑"
        passed, why = self.loop._verify(self.spec, self.world)
        self.last_verify_passed = bool(passed)
        return passed, (f"校验通过：{why}" if passed else f"校验未通过：{why}")


    def note(self, text: str) -> dict:
        """把一条发现写进世界模型备注（跨步骤/子任务不丢）。"""
        t = (text or "").strip()
        if t:
            self.world.add_note(t)
        return {"ok": True, "recorded": t, "notes_total": len(self.world.notes)}
_ARTIFACT_MAX = 6          # 单个子任务最多保留的关键产物条数
_ARTIFACT_SHOW_MAX = 3     # 主任务展示时最多展示条数
_ARTIFACT_ITEM_CHARS = 200  # 单条产物截断字符数
_WORLD_SUMMARY_CHARS = 600  # 世界模型摘要追加上限
def _subtask_artifacts(world: Any) -> List[str]:
    """U5a：抽取子任务记录的关键产物（备注优先，其次世界事实），去重截断。"""
    try:
        items = (list(getattr(world, "notes", None) or [])[-3:]
                 + list(getattr(world, "facts", None) or [])[-3:])
    except Exception:
        return []
    out: List[str] = []
    for t in items:
        t = str(t or "").strip()
        if t and t not in out:
            out.append(t)
    return out[:_ARTIFACT_MAX]
def _request_fingerprint(system_prompt: str, tool_names, session_len: int) -> str:
    """T4.6（RH-1）：生成请求指纹 ``<系统提示哈希>-<工具列表哈希>-<会话长度>``。

    仅用于链路溯源（同一指纹 = 同一系统提示 + 同一工具集 + 同一会话长度），
    不依赖任何厂商字段。
    """
    import hashlib

    def _h(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]

    names = "|".join(sorted(str(t) for t in (tool_names or [])))
    return f"{_h(str(system_prompt or ''))}-{_h(names)}-{int(session_len or 0)}"
def _recheck_spec(condition: str):
    """T4.4：仅供二次复核使用的轻量 TaskSpec（只带完成条件，不落盘不跑任务）。"""
    try:
        return TaskSpec(objective=str(condition or ""),
                        done_when=str(condition or ""),
                        expected=str(condition or "") or None)
    except Exception:
        return None
def _failure_point(res: Dict[str, Any]) -> str:
    """U5a：失败原因首行摘要（成功则为空）。"""
    if bool(res.get("success")):
        return ""
    reason = str(res.get("reason") or "").strip()
    if not reason:
        return ""
    first = reason.splitlines()[0].strip()
    return first[:_ARTIFACT_ITEM_CHARS]
def _format_prev_results(prev_results: List[Dict[str, Any]],
                         world_summary: str = "") -> str:
    """U5b：格式化历史子任务结果，供主任务下一轮参考。

    - 每条结果追加关键产物（单条截断 200 字符、最多 3 条）；
    - 追加完成条件校验结果与失败点位；
    - 存在历史子任务结果时，自动追加截断后的世界模型摘要（最大 600 字符）。
    无历史结果时返回空串（零追加，默认行为不变）。
    """
    if not prev_results:
        return ""
    lines: List[str] = []
    for r in prev_results or []:
        flag = "成功" if r.get("success") else "失败"
        lines.append(f"- {r.get('desc', '')}: {flag}（{r.get('reason', '')}）")
        if r.get("done_when_hit"):
            lines.append("    · 完成条件校验: 通过")
        # T4.4（U5c）：二次复核结果（仅标记展示，不拦截、不惩罚）
        _rc = str(r.get("recheck") or "")
        if _rc == "pass":
            lines.append("    · 复核: 通过")
        elif _rc == "fail":
            _why = str(r.get("recheck_reason") or "").strip()
            lines.append(f"    · 复核未通过: {_why}" if _why else "    · 复核未通过")
        for a in list(r.get("artifacts") or [])[:_ARTIFACT_SHOW_MAX]:
            a = str(a or "").strip()
            if not a:
                continue
            suffix = "..." if len(a) > _ARTIFACT_ITEM_CHARS else ""
            lines.append(f"    · 产物: {a[:_ARTIFACT_ITEM_CHARS]}{suffix}")
        fp = str(r.get("failure_point") or "").strip()
        if fp:
            lines.append(f"    · 失败点位: {fp}")
    text = "【子任务结果】\n" + "\n".join(lines)
    if world_summary:
        text += f"\n\n【当前世界状态摘要】\n{str(world_summary)[:_WORLD_SUMMARY_CHARS]}"
    return text
_DEFAULT_ESCALATION = {
    "verify_fail_max": 3,
    "wallclock_sec": 120,
    "no_confidence_hard": True,
    "worker_history_keep": 3,
}
@dataclass
class TaskSpec:
    objective: str
    done_when: str = ""
    expected: Optional[str] = None
    task_id: str = ""             # 资产归属（tasks/<task_id>/）；空则由调用方（/run）生成
    project_id: Optional[str] = None  # 可选：工作目录 slug 分组（仅会话历史用）
    max_steps: Optional[int] = None   # 单次顶层任务总工具/决策预算；None=不限（持续运行直到 done/升级/停止）
    history: List[Dict[str, str]] = field(default_factory=list)
    # 会话历史（OpenAI 风格 user/assistant 消息，不含 system）：单链路统一后
    # 由 /chat 注入（session 记录重建），作为 agent 起始上下文——替代旧的
    # 单一历史入口：跨轮记忆唯一来源，不与其它注入叠加。
    corrections: List[str] = field(default_factory=list)
    # K4：用户纠偏消息（C₁=refuted 且含纠正内容），来自 session，作蒸馏第三来源。
    # 默认空；仅当 config runtime.curator.corrective_source=true 时由 /chat 填充。
    task_mode: str = "oneshot"
def _safe_str(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)
def _compact_for_brain(result):
    """压缩工具结果后再喂给在线大脑，省 token。

    - ui_tree：XML 原文对大脑几乎无用（Unity 等自绘 UI 只有一个 SurfaceView 节点），
      截断到 500 字符保留头部线索。
    - raw：som_marks 等已给出结构化 marks，原始文本纯重复，直接丢弃。
    - 其它超长字符串字段：截断到 2000 字符。
    不修改原 result（trajectory/world-model 仍存完整数据）。
    """
    if not isinstance(result, dict):
        return result
    out = {}
    for k, v in result.items():
        if k == "raw":
            continue
        if isinstance(v, str):
            limit = 500 if k == "ui_tree" else 2000
            if len(v) > limit:
                v = v[:limit] + f"...[截断，原长 {len(v)}]"
        out[k] = v
    return out
class _TodoStore:
    """主 agent 待办持久化后端（tasks/<task_id>/todo.json）。

    仅作为 todo_write 元工具的存储注入；load/save 均吞异常，
    保证存储故障不中断主流程。整体替换语义：save 直接覆盖整份清单。
    """

    def __init__(self, path: str):
        self.path = str(path)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save(self, data):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

