"""EmitterMixin：ToolLoop 的职责切片（逐字搬移，零逻辑改动）。"""
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


class EmitterMixin:
    def _dbg(self, role: str):
        """包装 on_debug，给每条日志打上来源角色（brain / executor）。"""
        if not self.on_debug:
            return None
        return lambda kind, payload: self.on_debug(kind, {**payload, "role": role})

    def _make_llm_emitter(self, role: str, traj: Optional[Any] = None):
        """构造 LLM 回合埋点回调：把每轮「推理链 + 口播」拆两路推到前端，并支持真流式增量。

        返回 ``(_emit, _delta)`` 两个回调：
        - ``_emit(reasoning, message, calls)``：块结束时回传完整解析（debug 旁路 / 轨迹 / 工具参数缓存）；
        - ``_delta(kind, text)``：流式时按 token 回调（kind ∈ {reasoning, message}），由
          SDK Runner 的 raw_response_event 驱动（见 sdk_loop._run_loop_streamed）。

        拆通道（问题1）：推理链走「深度思考」块，口播走「对话」气泡，二者在对话区时间线里
        一起流式呈现，不再把结论塞进 system/思考块而丢失。
        """
        dbg = self._dbg(role)
        model = self.brain_model if role == "brain" else self.exec_model
        # B3 修复：rm 此前未定义（NameError 被 except 吞掉，终态纠偏从未执行）
        rm = self.reasoning_mode if role == "brain" else self.executor_reasoning_mode
        # B7：回合序号——每个 LLM 回合独立块（_emit 收尾时递增），同回合的
        # delta 先于 emit 到达、共享同号，跨回合不再串成单一巨型 thinking 块
        seq = 0
        # 回合内累积缓冲：增量回调时携带「截至当前的完整文本」，前端按 block_id upsert
        rbuf: List[str] = []
        mbuf: List[str] = []

        def _delta(kind: str, text: str) -> None:
            if not self.on_llm_delta:
                return
            try:
                if kind == "reasoning":
                    rbuf.append(text)
                    self.on_llm_delta(role, "reasoning", f"{role}-R{seq}", "".join(rbuf), model)
                else:
                    mbuf.append(text)
                    self.on_llm_delta(role, "message", f"{role}-M{seq}", "".join(mbuf), model)
            except Exception:
                pass

        def _emit(reasoning: str, message: str, tool_calls) -> None:
            nonlocal seq, rbuf, mbuf
            # 注意：决策步/大脑调用计数已迁移到 sdk_loop.on_llm_end 钩子（按模型调用粒度，
            # 见 F1.2），此处 _emit 只负责推理/口播的呈现与落盘，不再重复计数。
            # T4.6（RH-1）：请求指纹（系统提示哈希 + 工具列表哈希 + 会话长度）落轨迹
            if traj is not None:
                try:
                    fp = _request_fingerprint(
                        getattr(self, "_fp_system_prompt", "") or "",
                        getattr(self, "_fp_tool_names", None) or [],
                        seq,
                    )
                    traj.log_think(
                        json.dumps({"kind": "request_fingerprint", "role": role,
                                    "fingerprint": fp, "round": seq}, ensure_ascii=False),
                        role, model,
                    )
                except Exception:
                    pass
            content = (reasoning + "\n\n" if reasoning else "") + message
            if dbg:
                dbg("llm_response", {
                    "model": model,
                    "content": content or "",
                    "reasoning": reasoning or "",
                    "message": message or "",
                    "tool_calls": tool_calls or [],
                    "finish_reason": None,
                })
            # 轨迹落盘：推理链写进 trajectory.jsonl（kind=think），供历史复盘
            if reasoning and traj is not None:
                try:
                    traj.log_think(reasoning, role, model)
                except Exception:
                    pass
            # 非流式回退：未启用增量回调时，仍把完整「推理+口播」推到对话区（兼容旧路径/测试）
            if not self.on_llm_delta and self.on_thinking:
                full = (reasoning + "\n\n" if reasoning else "") + message
                if full:
                    self.on_thinking(role, full, model)
            # 终态：用完整推理/口播 upsert 本回合块（即便流式中断，最终内容也完整）
            if self.on_llm_delta:
                try:
                    if reasoning:
                        self.on_llm_delta(role, "reasoning", f"{role}-R{seq}", reasoning, model)
                    if message:
                        if rm == "think-tag":
                            pure = _strip_after_think(message)
                            if pure:
                                self.on_llm_delta(role, "message", f"{role}-M{seq}", pure, model)
                        else:
                            self.on_llm_delta(role, "message", f"{role}-M{seq}", message, model)
                except Exception:
                    pass
            # 本回合结束：序号递增、缓冲清空（下一回合独立成块，不与上回合拼接）
            seq += 1
            rbuf = []
            mbuf = []
            # 缓存本轮回话的工具调用参数：等 on_step 实际执行时按名称匹配上结果
            if self.on_tool_call and tool_calls:
                for c in tool_calls:
                    c = c or {}
                    self._pending_calls.append((role, c.get("name", ""), c.get("arguments", "")))
        def _turn_end() -> None:
            """回合边界回调：切块 + 清空缓冲（由流式层在每次工具调用产出时触发）。

            流式路径下 ``_emit`` 每个 chunk 才被调一次（见 ``_run_loop_streamed``），
            若只靠它自增 ``seq``，整 chunk 的所有回合会共用 ``{role}-R0`` / ``{role}-M0``
            这一组 key，被按 id upsert 合并成「一条巨型思考 + 一条巨型口播」，且位置停在
            首次出现处——外部现象就是终态里「结果与思考跑到最前、工具调用全挤到后面」。
            故由调用方在每回合的工具调用产出时回调本函数，使每回合独立成块，
            内容与顺序都与真实时间线一致。
            """
            nonlocal seq, rbuf, mbuf
            seq += 1
            rbuf = []
            mbuf = []

        return _emit, _delta, _turn_end

