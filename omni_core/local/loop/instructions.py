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
        """记忆**稳定段**进 system prompt——使用规则 + scene 导航 + 用户画像。

        * ``<memory-usage-rules>``（P1，2026-11-02 方案 C）：L1 检索结果永不自动注入，
          system 常驻的是「必查场景 + 每轮合计 ≤3 次」的使用规则；gate ``memory.enabled``；
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
            # P1（2026-11-02，方案 C）：L1 检索结果**永不自动注入**——常驻 system 的是
            # 使用规则，内容由模型经 memory_recall（及 P2 的 conversation_search）自取。
            # NP-2（plan personal-agent-memory §13.3.2）：叙事能力声明零内容化 + 「叙事
            # 不得转述到对外消息」；七工具配额口径统一。
            _rules = (
                "<memory-usage-rules>\n"
                "长期记忆（你与主人的事实/规则/偏好/事件）不会自动提供给你，需要时自己查：\n"
                "- 必查：被问过往事件、以前的约定、人物关系、主人的相关信息时，"
                "先调 memory_recall 查过再回答，不凭印象编造；\n"
                "- 要原话、具体时间、具体是谁说的：memory_recall 只有摘要或查不到时，"
                "调 conversation_search 检索原始聊天记录（关键词用 2-4 字最有效）；\n"
                "- 你的经历叙事（日记/月记/对人的印象卡）同样不自动提供：被问「上次/那天/"
                "我们/某个人」时先调 narrative_search 找条目（返回日期/摘要），"
                "要读全文再调 narrative_read；\n"
                "- 叙事内容只供你自己参考——不得把日记/月记/对人的印象直接转述到对外消息"
                "里，也不得把它们当成客观事实引用；\n"
                "- 查不到就如实说「没有相关记录」；一个 run 内自取过往（memory_recall + "
                "conversation_search + 历史自取工具 + read_scene + narrative_search + "
                "narrative_read）合计不超过 3 次，其中 narrative_* 不超过 2 次，"
                "不要反复换关键词硬查；\n"
                "- 与当前请求无关的记忆内容不要主动提及。\n"
                "</memory-usage-rules>")
            # P6-2（H-6 §16.3）表达节奏开关（缺省关）：开启时加一句「自我披露 / 把话
            # 递回去」的分寸；关闭时稳定段逐字节不变（验收口径，不做主动闲聊）。
            try:
                import config as _cfg_mod
                if (bool(_cfg_mod.get_config(
                        "knowledge.narrative.we_memory.enabled", False))
                        and bool(_cfg_mod.get_config(
                            "knowledge.narrative.we_memory.rhythm", False))):
                    _rules = _rules.replace(
                        "</memory-usage-rules>",
                        "- 聊到你们共同经历过的事时，可以用自己的口气自然提起，也可以把"
                        "话递回去问问对方当时的感觉；不硬塞回忆、不为提起而提起。\n"
                        "</memory-usage-rules>")
            except Exception:
                pass
            parts.append(_rules)
            layers.append("memory_rules")
            try:
                from omni_core.local import scene_executor
                _pid = TaskStore.project_of(str(task_id)) if task_id else ""
                # NP-2（§13.3.2）：导航取消日记/月记固定配额（diary_cap=0, month_cap=0）——
                # 稳定段只列主题块；叙事条目由 narrative_search 按需检索（R6）。
                # 双保险过滤 diary-*：无论 load_scene_nav 的配额实现如何，
                # 固定日记条目不得进稳定段（INV-11 / N-3）。
                _entries = [e for e in scene_executor.load_scene_nav(
                    _pid, diary_cap=0, month_cap=0)
                    if not str(e.get("filename") or "").startswith("diary")]
                if _entries:
                    # 2026-11-02 对齐原仓库 `Path + 热度 + Summary`（M-1 的"只给文件名"
                    # 让模型无法判断该读哪篇，diary-* 文件名零信息量）。每条三行，
                    # 摘要截 120 字；日记/月记渲染日期（heat 恒 0），主题块渲染热度。
                    _rows: List[str] = []
                    for _e in _entries:
                        _fn = str(_e.get("filename") or "")
                        _summary = str(_e.get("summary") or "").strip()
                        _upd = str(_e.get("updated") or "")[:10]
                        if _fn.startswith("diary"):
                            _meta = f"更新 {_upd}"
                        else:
                            _meta = f"热度 {int(_e.get('heat') or 0)} · 更新 {_upd}"
                        _rows.append(f"- {_fn}（{_meta}）")
                        if _summary:
                            _rows.append(f"  {_summary[:120]}")
                    _block = ("<scene-navigation>\n"
                              "以下是可用场景记忆的索引（文件名 + 摘要）。摘要与本轮相关"
                              "但信息不足时，调用 read_scene 工具（scene_name=文件名）"
                              "读取全文；内容已足够时不必读。不要在无关请求中主动提及"
                              "场景内容。\n"
                              f"{'\\n'.join(_rows)}\n</scene-navigation>")
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
        # M1（plan personal-agent-memory §6.1）：``<current-context>`` 工作记忆稳定段——
        # run 起始由 context_signal.build_run 单点构造一次、run 内不变；≤200 字符
        # （context.context_chars），每 run 只注入一次。空信号零注入（逐字节不变）。
        if not is_sub:
            try:
                from omni_core.local.loop import context_signal as _cs
                _ctx = _cs.render()
                if _ctx:
                    parts.append(_ctx)
                    layers.append("context_signal")
            except Exception:
                pass
        if not parts:
            return "", []
        return "\n\n".join(parts), layers

    def _plain_finish_judge(self, traj: Optional[Any]):
        """收尾意图门（Omni 加固,TAM 无此机制）：纯文本收尾判「信任大脑」成功前的语义复核。

        背景：畸形调用表现为「文本被当普通输出、调用根本不派发」,而 oneshot 语义会把
        纯文本收尾判成功 → 任务静默假成功（doc/plans/implemented/memory-architecture.md §6）。
        判断**全 LLM**（是否在尝试发起但未派发调用）,零厂商格式字面量——与
        「语义判断全 LLM、脚本只做簿记」红线同构;仅 oneshot 无校验条件路径触发。

        取向门（2026-11-04）：**对话取向（mode=chat）不付这道门**。它防的是「oneshot 任务
        把没派发的畸形调用当结论、静默假成功」,而 chat 的纯文本收尾**就是交付物本身**——
        没有「任务成功」这件事可被误判。judge 是一次不计入 brain_calls 的额外模型往返
        （timeout 90s）,压在用户已经看完答案之后：本机同一句闲聊实测,口播完成 → run 结束
        的门内尾巴 **34.29s**,关门后 **0.04s**（A/B 口径：runtime.long_task
        .finish_intent_check=false）。task/auto 取向语义不变。

        judge 异常/无 brain_cfg/配置关/对话取向 → 返回 None（fail-open=现状）。
        """
        brain_cfg = getattr(self, "brain_cfg", None)
        if not brain_cfg:
            return None
        if str(getattr(self, "mode", "auto") or "auto") == "chat":
            return None
        try:
            # 总闸（设置页「通用」→ 收尾意图门）：只对 task/auto 有效——上面那行 chat
            # 退出在读键之前，所以 chat 不受本开关影响，不存在"开关开着 + chat 被关掉"的组合。
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

