"""InstructionMixin：ToolLoop 的职责切片（逐字搬移，零逻辑改动）。"""
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
from omni_core.tools.vision_runtime import VisionRuntime
from omni_core.local.states import AgentState
from omni_core.local.trajectory import TrajectoryStore
from omni_core.local import telemetry
from omni_core.local.curator import Curator
from devices import ExecutionModule
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


class InstructionMixin:
    def _log_knowledge_injection(self, traj: Optional[Any], record: Dict[str, Any]) -> None:
        """T4.6（O5+）：知识/技能注入行为落轨迹（字符数、技能数），补齐可观测维度。

        轨迹存储不可用时静默跳过（埋点不得影响主流程）。
        """
        if traj is None:
            return
        try:
            traj.log_think(json.dumps({"kind": "knowledge_injection", **(record or {})},
                                      ensure_ascii=False), "knowledge", "")
        except Exception:
            pass

    def _memory_in_system(self) -> bool:
        """F4.2：memory 是否仍走 system prompt（= memory_in_user 关闭时的回退路径）。

        memory_in_user=true（缺省）→ 记忆迁至尾部重插，此处返回 False（system 不再注入）。
        """
        try:
            return not bool(config.get_config("runtime.long_task.memory_in_user", True))
        except Exception:
            return False

    def _load_instructions_snapshot(self, spec):
        """F4.1b：run 开始时读一次纪律文件（AGENTS.md）并锁定快照。

        * 总开关 ``runtime.long_task.inject_instructions``（缺省 true，false = 零注入）；
        * 两层：``~/.omniagent/AGENTS.md``（global）+ ``tasks/<task_id>/AGENTS.md``（task），
          task 层在后（更近，语义上优先）；
        * 上限 ``runtime.long_task.instructions_limit``（缺省 8192），超限截断并标注。

        返回 ``AgentsSnapshot``（均不存在 → 空块，instructions 逐字节不变）；
        读取/配置异常一律退化为空快照，绝不阻塞任务。
        """
        try:
            from omni_core.local.knowledge_inject import load_agents_snapshot
        except Exception:
            return None
        try:
            if not bool(config.get_config("runtime.long_task.inject_instructions", True)):
                return load_agents_snapshot([])
            from omni_core.local.runtime_paths import global_agents_file, task_agents_file

            layers = [("global", global_agents_file())]
            if getattr(spec, "task_id", ""):
                layers.append(("task", task_agents_file(spec.task_id)))
            limit = int(config.get_config("runtime.long_task.instructions_limit", 8192) or 8192)
            return load_agents_snapshot(layers, limit=limit)
        except Exception:
            return load_agents_snapshot([])

    @staticmethod
    def _merge_instructions(system_prompt: str, snapshot: Any) -> str:
        """F4.1b：把纪律块并入 system prompt 尾部。

        空块 → 原样返回（与现状**逐字节一致**，默认行为不变）。
        这是「不改 system prompt 组装」红线的**显式豁免**——仅限纪律块拼接这一处，
        kernel 的 ``build_system_prompt`` 组装逻辑不在改动范围内。
        """
        block = getattr(snapshot, "block", "") or ""
        if not block:
            return system_prompt or ""
        return (system_prompt or "") + "\n\n" + block

    def _build_memory_injection(self, is_sub: bool):
        """F4.2：记忆块——尾部重插的**唯一**来源（纪律文件已迁 system）。

        * ``runtime.long_task.memory_in_user``（缺省 true）且为**主链**且
          ``knowledge.memory`` 开启时并入；false 时记忆仍走 system prompt（一键回退）。

        返回 ``(block_text, labels)``；无内容 → ``("", [])``（零注入）。
        """
        try:
            _mem_in_user = bool(config.get_config("runtime.long_task.memory_in_user", True))
            if (not is_sub) and _mem_in_user and self.knowledge_cfg.get("memory"):
                from omni_core.local.knowledge_inject import (
                    compose_injection_block,
                    load_memory_text,
                )

                _mt = load_memory_text()
                if _mt.strip():
                    _limit = int(config.get_config("runtime.long_task.instructions_limit", 8192) or 8192)
                    return compose_injection_block([("memory", _mt)], limit=_limit)
        except Exception:
            pass
        return "", []

