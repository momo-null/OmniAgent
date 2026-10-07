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

    def _memory_stable_block(self, is_sub: bool, task_id: str = ""):
        """TAM 对齐：记忆**稳定段**进 system prompt——scene 摘要导航 + 用户画像。

        形态对齐 TAM auto-recall（stable/dynamic 拆分）：
        * ``<scene-navigation>``：L2 场景摘要 + 更新时间 + read_scene 指引——**只放索引**，
          全文由模型按需调 ``read_scene`` 读取（渐进披露）；gate ``runtime.knowledge.memory.enabled``；
        * ``<user-persona>``：用户画像（TAM 的 L3 persona 位置）；gate ``knowledge.profile.enabled``。

        与纪律块同款约束：run 开始读一次、run 内不变（scene/画像维护只在任务收尾 flush
        触发，run 中途文件不变），命中前缀缓存。子任务（is_sub）零注入，语义不变。
        返回 ``(block, layers)``；全空 → ``("", [])``（零注入，逐字节不变）。
        """
        if is_sub:
            return "", []
        parts: List[str] = []
        layers: List[str] = []
        if self.knowledge_cfg.get("memory"):
            try:
                from omni_core.local import scene_executor
                _pid = TaskStore.project_of(str(task_id)) if task_id else ""
                _entries = scene_executor.load_scene_nav(_pid)
                if _entries:
                    _rows = "\n".join(
                        f"- {_e['filename']}（热度 {_e['heat']}）：{_e['summary']}"
                        for _e in _entries)
                    _block = ("<scene-navigation>\n"
                              "以下是场景记忆索引（摘要）。需要某场景的完整环境事实时，"
                              "调用 read_scene 工具（scene_name=文件名）读取全文；"
                              "摘要与实际观测冲突时以观测为准。\n"
                              f"{_rows}\n</scene-navigation>")
                    parts.append(_block)
                    layers.append("scene_nav")
            except Exception:
                pass
        if self.knowledge_cfg.get("profile"):
            try:
                from omni_core.local.knowledge_inject import load_profile_text
                _pt = load_profile_text()
                if _pt.strip():
                    parts.append(f"<user-persona>\n{_pt.strip()}\n</user-persona>")
                    layers.append("user_profile")
            except Exception:
                pass
        if not parts:
            return "", []
        return "\n\n".join(parts), layers

    def _plain_finish_judge(self, traj: Optional[Any]):
        """收尾意图门（Omni 加固,TAM 无此机制）：纯文本收尾判「信任大脑」成功前的语义复核。

        背景：畸形调用表现为「文本被当普通输出、调用根本不派发」,而 oneshot 语义会把
        纯文本收尾判成功 → 任务静默假成功（doc/plans/memory-architecture.md §6）。
        判断**全 LLM**（是否在尝试发起但未派发调用）,零厂商格式字面量——与
        「语义判断全 LLM、脚本只做簿记」红线同构;仅 oneshot 无校验条件路径触发。
        judge 异常/无 brain_cfg/配置关 → 返回 None（fail-open=现状）。
        """
        brain_cfg = getattr(self, "brain_cfg", None)
        if not brain_cfg:
            return None
        try:
            if not bool(config.get_config("runtime.long_task.finish_intent_check", True)):
                return None
        except Exception:
            pass

        def _judge(text: str) -> Optional[str]:
            if not text or not text.strip():
                return None
            try:
                from omni_core.local import llm_judge
                # 提取式判断(比二分类稳):让模型找出所有"本应作为工具调用发出"的片段——
                # 无论以什么形式出现(结构化、伪标签包裹、自然语言描述),判定权全在 LLM,
                # 内核零格式字面量。真机实测:二分类问法对"开头像收尾+结尾藏伪调用"的
                # 文本会漏判;提取问法不会(2026-10-07 真机验证)。
                data = llm_judge.chat_json(
                    brain_cfg,
                    '你是 agent 输出审查器。下面是一段 agent 的输出文本。'
                    '从中找出所有「本应作为工具调用发出」的片段:即命令、脚本、文件写入、'
                    'API/工具调用等本应由工具执行的内容,无论它以什么形式出现'
                    '(结构化调用、被标签/标记包裹的伪调用、自然语言描述的待执行步骤、'
                    '代码块中的待执行脚本)。\n'
                    '注意:纯结论、数字汇报、表格、说明文字不算;'
                    '已经执行完成、仅在汇报结果的命令也不算(那是收尾陈述的组成部分)。\n'
                    '示例(节选):文本为「扫描完成,共 5 个文件。接下来写入报告:\n'
                    "<调用 action=\"write\" target=\"report.md\">...内容...</调用>」\n"
                    '→ {"fragments": ["<调用 action=\\"write\\" target=\\"report.md\\">...内容...</调用>"]};'
                    '文本为「扫描完成,最大文件 4.2GB」→ {"fragments": []}\n'
                     '只返回 JSON:{"fragments": ["<片段原文>", "..."]};没有则 {"fragments": []}',
                    text[:4000], timeout=90.0)
            except Exception:
                return None
            frags = data.get("fragments") if isinstance(data, dict) else None
            if not isinstance(frags, list) or not frags:
                return None
            self._log_knowledge_injection(traj, {
                "kind": "finish_intent_check", "verdict": "attempted_tool_call",
                "fragments": len(frags), "chars": len(text or "")})
            return ("你上一条输出中似乎包含未真正派发的工具调用（调用以普通文本形式写出，"
                    "因而没有执行）。请用结构化 tool_calls 重新发出同样的调用；"
                    "不要把工具调用写进普通文本。")

        return _judge

    def _memory_lead_in(self, is_sub: bool, task_id: str = "", query: str = "") -> str:
        """TAM 对齐：L1 检索记忆 **run 开始注入一次**（user 消息，历史后、任务输入前）。

        频率语义对齐 TAM auto-recall（每轮一次）：run 即 Omni 的「一轮任务」——
        起始 items 里的消息由后续块 ``to_input_list()`` 自然继承，天然不重发。
        **不得**改回每步尾部注入：检索记忆属动态内容，尾部重发 = 每步强化一次，
        反复 priming 抬高生成退化概率（memory-architecture.md §5）。
        gate ``runtime.knowledge.memory.enabled``；池子低于阈值时零注入（TAM 原语义）。
        """
        if is_sub or not self.knowledge_cfg.get("memory"):
            return ""
        try:
            from omni_core import memory_tam
            _pid = TaskStore.project_of(str(task_id)) if task_id else ""
            _mt = memory_tam.inject_text(_pid, query or "")
            if self.on_debug:
                try:
                    _hits = sum(1 for _l in _mt.splitlines() if _l.startswith("- ")) if _mt else 0
                    self.on_debug("memory_inject", {
                        "title": "记忆注入",
                        "pool": memory_tam.count_atoms(_pid)
                                + memory_tam.count_global_atoms(),
                        "hits": _hits,
                        "placement": "lead_in",
                    })
                except Exception:
                    pass
            return _mt or ""
        except Exception:
            return ""

