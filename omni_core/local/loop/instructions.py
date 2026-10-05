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
from omni_core.local.states import AgentState
from omni_core.local.trajectory import TrajectoryStore
from omni_core.local import telemetry
from omni_core.local.runtime_paths import (
    task_trajectory, task_collected,
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

    def _load_instructions_snapshot(self, spec):
        """F4.1b：run 开始时读一次纪律文件（AGENTS.md）并锁定快照。

        * 总开关 ``runtime.long_task.inject_instructions``（缺省 true，false = 零注入）；
        * 三层：``~/.omniagent/AGENTS.md``（global）+ ``projects/<pid>/AGENTS.md``
          （project，按任务归属解析，知识分层 v2）+ ``tasks/<task_id>/AGENTS.md``
          （task），越靠后越近、语义上优先；
        * 上限 ``runtime.long_task.instructions_limit``（缺省 8192），超限截断并标注。

        AGENTS.md 纪律文件（B2 澄清，防误判为死平面）：**由用户/团队维护，内核不写入**
        （也无编辑 UI，只在文件系统里维护）；无文件即零注入（默认行为）。它不是
        运行时的产物，任何自动写入 AGENTS.md 的行为都违反
        「用户单写稳定纪律」的放置原则。

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
            from omni_core.local.runtime_paths import (
                global_agents_file, project_agents_file, task_agents_file,
            )

            layers = [("global", global_agents_file())]
            if getattr(spec, "task_id", ""):
                # 项目层：与 project memory / skills 同一归属解析（无记录回退 default）
                layers.append(("project", project_agents_file(
                    TaskStore.project_of(str(spec.task_id)))))
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

    def _merge_character(self, system_prompt: str) -> str:
        """P0：角色卡（character.md）并入 system prompt——稳定人格设定。

        与 AGENTS.md 同属「用户单写、run 内不变」的稳定内容，run 开始读一次；
        角色卡缺失/空 → 原样返回（零注入，默认行为不变）。
        覆盖语义：用户当轮明确指令优先于角色卡（块首已声明）。
        """
        try:
            from omni_core.local.knowledge_inject import load_character_text
            ct = load_character_text()
            if ct.strip():
                block = (
                    "# 角色设定（character.md）\n"
                    "以下为你的人格与相处方式设定，请据此与用户相处；"
                    "用户当轮的明确指令优先于此设定。\n\n"
                    + ct.strip() + "\n"
                )
                return (system_prompt or "") + "\n\n" + block
        except Exception:
            pass
        return system_prompt or ""

    def _build_memory_injection(self, is_sub: bool, task_id: str = "", query: str = "",
                                budget_chars: int = -1):
        """F4.2：记忆检索段（TAM 移植）+ 用户画像块——尾部重插的来源（纪律文件已迁 system）。

        * 记忆检索段：gate ``runtime.knowledge.memory.enabled``；池子低于
          ``memory_tam._INJECT_TRIGGER`` 时零注入（与无记忆行为一致）；
        * 画像块 gate：``runtime.knowledge.profile.enabled``（默认开，P0）。

        返回 ``(block_text, labels)``；无内容 → ``("", [])``（零注入）。
        """
        try:
            if not is_sub:
                from omni_core.local.knowledge_inject import (
                    compose_injection_block,
                    load_profile_text,
                )

                _limit = int(config.get_config("runtime.long_task.instructions_limit", 8192) or 8192)
                parts: List[Tuple[str, str]] = []
                # 记忆检索段（TAM 移植）：按任务目标从项目记忆库检索相关 atoms
                if self.knowledge_cfg.get("memory"):
                    try:
                        from omni_core import memory_tam
                        _pid = TaskStore.project_of(str(task_id)) if task_id else ""
                        _mt = memory_tam.inject_text(_pid, query or "")
                        if _mt.strip():
                            parts.append(("memory", _mt))
                    except Exception:
                        pass
                # 画像（P0）：knowledge.profile.enabled 开启才注入（默认开）
                if self.knowledge_cfg.get("profile"):
                    _pt = load_profile_text()
                    if _pt.strip():
                        parts.append(("user_profile", _pt))
                if parts:
                    return compose_injection_block(parts, limit=_limit)
        except Exception:
            pass
        return "", []

