"""SdkBridgeMixin：ToolLoop 的职责切片（逐字搬移，零逻辑改动）。"""
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


class SdkBridgeMixin:
    def _compress_history(self, brain, old_turns: List[List[Dict[str, Any]]], world=None,
                          source: str = "", prefix: str = "") -> List[Dict[str, Any]]:
        """把较早的多轮交互压成一段摘要，作为单个『轮次』返回。

        调用大脑自身做摘要（compress=true）；失败则退化为截断说明，不崩溃。
        返回的列表含一条 user 消息，可直接并入 turns（flatten 时摊平为一条消息）。

        M4b.1: 压缩后将关键进展 merge_progress 回 world-model facts（防 JPEG 效应）。
        M8: 带 ``source``（分支标识）写入，使共享黑板上的压缩产物可溯源。
        """
        flat = []
        for t in old_turns:
            for m in t:
                content = m.get("content")
                if content is None and m.get("tool_calls"):
                    content = "<tool_call>"
                flat.append(f"[{m.get('role', '?')}] {content or ''}")
        text = "\n".join(flat)
        # T3.2：热前缀——把当前系统提示并入待压缩文本，使摘要贴合当前任务上下文
        if prefix:
            text = f"[系统提示] {prefix}\n" + text
        summary = "(历史已压缩，无摘要)"
        if self.brain_long_task.get("compress", True):
            try:
                reply = brain.chat(
                    [
                        {"role": "system", "content": "你是上下文压缩器。把以下多轮交互压成一段简短中文摘要，"
                         "保留：任务目标、已完成动作、关键屏幕状态、未决问题，不超过150字。"},
                        {"role": "user", "content": text},
                    ],
                    tools=None,
                )
                summary = (reply.content or "").strip() or summary
            except Exception as e:
                self._log(f"历史压缩失败: {type(e).__name__}: {e}，退化为截断")
        # M4b.1: 将摘要中的关键进展（按句号/换行拆分）写回 world-model，避免反复压缩导致信息磨灭
        if world is not None and isinstance(world, WorldModel) and summary != "(历史已压缩，无摘要)":
            try:
                # 简单拆分：按中文句号 / 换行取关键句
                parts = summary.replace("；", "。").replace("\n", "。").split("。")
                for p in parts:
                    p = p.strip()
                    if p and len(p) > 3:
                        world.add_fact("[压缩提取] " + p, source=source)
            except Exception:
                pass
        return [{"role": "user", "content": f"[历史压缩摘要] {summary}"}]

    def _resolve_main_wallclock(self, budget, spec) -> float:
        """F2.4：主链墙钟解析。

        主链墙钟取自 ``runtime.long_task.wallclock_sec``（缺省 0 = 不检查，保持现状），
        与 ``config.get_config`` 读取 ``runtime.long_task.repeat_guard`` 同源（顶层 runtime.long_task）。
        与子任务墙钟（``runtime.escalation.wallclock_sec``，缺省 120s，worker 防打转安全网）
        彻底解耦——二者来源、语义、缺省都不同。

        大步数预算（有效预算 >200 或 spec.max_steps >200）且未配置墙钟时，主链默认
        *不检查* 墙钟，并打一条 INFO 说明联动逻辑，避免「配 1000 步却被 120s 默认墙钟腰斩」。
        """
        sec = float(config.get_config("runtime.long_task.wallclock_sec", 0) or 0)
        if sec == 0:
            _big = (budget is not None and budget > 200) or (
                getattr(spec, "max_steps", None) is not None and spec.max_steps > 200
            )
            if _big:
                self._log(
                    "INFO 主链墙钟未配置（runtime.long_task.wallclock_sec=0）：检测到大步数预算，"
                    "主链默认不检查墙钟（避免被 120s 默认墙钟腰斩）；如需限时终止请显式配置该值（秒）。"
                )
        return sec

    def _run_via_sdk(
        self,
        spec: TaskSpec,
        brain,
        system_prompt: str,
        world: WorldModel,
        *,
        max_steps: Optional[int] = None,
        allow_escalate: bool = False,
        wallclock_sec: float = 0.0,
        traj: Optional[TrajectoryStore] = None,
        compress_after: int = 0,
        allow_dispatch: bool = False,
        user_input: Optional[str] = None,
        is_sub: bool = False,
        board_source: str = "",
        budget_hint_ratio: float = 0.0,
        on_llm: Optional[Callable[[str, str, List[Any]], None]] = None,
        on_llm_delta: Optional[Callable[[str, str], None]] = None,
        on_turn_end: Optional[Callable[[], None]] = None,
        history_keep: int = 0,
        compress_threshold: float = 0.0,
        hard_ceiling: float = 0.0,
        ctx_window: int = 0,
        prune_threshold: int = 0,
        prune_head: int = 0,
        prune_tail: int = 0,
        max_input_tokens: int = 0,
        retain_ratio: float = 0.0,
        history_items: Optional[List[Dict[str, str]]] = None,
        todo_store: Any = None,
        skill_catalog: Optional[str] = None,
    ) -> Dict[str, Any]:
        """用 Agents SDK 的 Runner 跑一个子任务（循环/FC/派发/回填全归框架）。

        L2 只保留四件事：完成判定与升级门控（元工具）、世界模型/轨迹落盘
        （RunHooks 后处理）、预算与停止检查、长任务上下文压缩。

        M7：``allow_dispatch`` 为真时主 agent 多一个 `dispatch` 元工具，
        一调即停并把派发计划经 gate 交回编排层（结果里 `dispatch_plan`）。
        """
        from omni_core.brain.sdk_loop import run_subtask_sdk

        gate = _SubtaskGate(self, spec, world)
        # 主 agent（allow_dispatch=True）自动接入待办持久化（tasks/<task_id>/todo.json）；
        # 子 agent 不暴露 todo_write，因此不构造 store。外部可显式注入 todo_store 覆盖。
        if todo_store is None and allow_dispatch:
            try:
                from omni_core.local.runtime_paths import task_dir

                todo_store = _TodoStore(os.path.join(str(task_dir(spec.task_id)), "todo.json"))
                todo_store.load()  # 每次运行加载既有待办（落盘失败不影响主流程）
            except Exception:
                todo_store = None
        # T2.4：绑定当前 task_id，供 load_skill「私有优先、全局兜底」定位任务私有技能；
        # 技能目录消息始终生成（skill 属 core 常开；分组门禁已删）。
        try:
            from omni_core.tools.skill_tool import set_skill_task_context

            set_skill_task_context(spec.task_id)
        except Exception:
            pass
        # F1.1：先取结构化 catalog（供埋点统计条数/来源），再格式化为 User 消息。
        catalog: Optional[List[Dict[str, Any]]] = None
        if skill_catalog is None:
            try:
                from omni_core.local.knowledge_inject import (
                    build_skill_catalog,
                    format_skill_catalog_message,
                )

                catalog = build_skill_catalog(spec.task_id)
                skill_catalog = format_skill_catalog_message(catalog)
            except Exception:
                skill_catalog = None
                catalog = None
        t_step = [0.0]
        role = "executor" if is_sub else "brain"
        # F4.1b：纪律文件（AGENTS.md）进入 system prompt——run 开始读一次并锁定快照。
        # 放置原则：用户单写、run 内不变的稳定纪律 → system（缓存命中 + 覆盖语义正确
        # 「用户当轮指令覆盖一切」+ 注入面收敛）；memory 属增长内容，仍走会话流尾部。
        _instr_snapshot = self._load_instructions_snapshot(spec)
        system_prompt = self._merge_instructions(system_prompt, _instr_snapshot)
        if getattr(_instr_snapshot, "injected", False):
            self._log_knowledge_injection(traj, {
                "kind": "instructions_injected",
                "chars": len(_instr_snapshot.block),
                "layers": list(_instr_snapshot.labels),
                "placement": "system",
                "locked": True,
                "fingerprint": _instr_snapshot.fingerprint(),
            })

        def _on_instr_drift(labels, _traj=traj):
            """run 中途纪律文件被改动：告警并沿用起始快照（下个 run 才生效）。"""
            self._log(
                f"纪律文件在 run 内被改动（{list(labels)}）：沿用 run 起始快照，"
                f"system 保持逐字节稳定；改动自下一次运行生效"
            )
            self._log_knowledge_injection(_traj, {
                "kind": "instructions_drift", "layers": list(labels)})

        # T4.6（RH-1）：指纹输入（系统提示 + 工具名列表），供 _emit 生成请求指纹。
        # F4.1b（O5+ 不变式）：此处传入的是**拼接纪律块之后**的 system → 指纹随
        # AGENTS.md 内容跨 run 变化、run 内不变，保证「Model-visible ⟺ logged」。
        try:
            self._fp_system_prompt = system_prompt or ""
            _tools_for_fp = list(tools or []) or list(getattr(self, "tool_schemas", None) or [])
            self._fp_tool_names = sorted(
                str(getattr(t, "name", t) if not isinstance(t, dict) else t.get("name", t))
                for t in _tools_for_fp
            )
        except Exception:
            self._fp_tool_names = []
        # T4.6（O5+）：技能目录注入落轨迹（记录字符数 / 技能数 / 名称 / 来源，补齐可观测维度）
        if skill_catalog:
            try:
                _cat = catalog or []
                self._log_knowledge_injection(traj, {
                    "kind": "skill_catalog",
                    "chars": len(skill_catalog),
                    "skills": len(_cat),
                    "names": [s.get("name") for s in _cat],
                    "sources": {
                        "task": sum(1 for s in _cat if s.get("source") == "task"),
                        "global": sum(1 for s in _cat if s.get("source") == "global"),
                    },
                })
            except Exception:
                pass
        # 新一轮：清空上轮遗留的待关联参数（避免跨 run 串味）
        self._pending_calls = []

        def _on_step(tool_name: str, result: Any) -> None:
            """L2 后处理钩子：结果 -> 世界模型 / 轨迹（不读感知字段，走 backend 抽象）。"""
            import time as _t

            self._action_count += 1
            # 工具调用结果埋点：让前端日志可见「每步 react（工具结果）」
            _dbg = self._dbg(role)
            if _dbg:
                _s = result if isinstance(result, str) else str(result)
                if len(_s) > 400:
                    _s = _s[:400] + "...(截断)"
                _dbg("tool_result", {"tool": tool_name, "result": _s})
            # 对话区实时工具调用（左栏卡片）：关联本轮回话参数（按工具名 FIFO 匹配）
            if self.on_tool_call:
                role_model = self.brain_model if role == "brain" else self.exec_model
                args = ""
                for i, (r, n, a) in enumerate(self._pending_calls):
                    if n == tool_name:
                        args = a
                        del self._pending_calls[i]
                        break
                else:
                    # 没匹配到（如元工具 task_done 未进缓存）：清掉积压最旧项，防止越积越多
                    if self._pending_calls:
                        self._pending_calls.pop(0)
                _args = args if isinstance(args, str) else str(args)
                if len(_args) > 600:
                    _args = _args[:600] + "...(截断)"
                _res = result if isinstance(result, str) else str(result)
                if len(_res) > 600:
                    _res = _res[:600] + "...(截断)"
                self.on_tool_call(role, tool_name, _args, _res, role_model)
            latency = (_t.time() - t_step[0]) * 1000 if t_step[0] else 0.0
            t_step[0] = _t.time()
            if traj is not None:
                try:
                    traj.log_step(
                        state=self.state.value,
                        observation=world.snapshot(),
                        action={"tool": tool_name, "args": {}},
                        result=result,
                        metrics={"latency_ms": round(latency, 1)},
                        # T4.6（O5+）：校验状态真值化——读取网关最近一次校验结果，
                        # 不再固定 False（此前导致轨迹里校验状态永远不真）。
                        verified=bool(getattr(gate, "last_verify_passed", False)),
                    )
                except Exception:
                    pass
            if not isinstance(result, dict):
                return
            world.log_action({"tool": tool_name, "args": {}}, result)
            # 世界模型分发：按工具**自声明的 percept 元数据**，内核零工具名字面量。
            from omni_core.tools.base import TOOL_REGISTRY
            _percept = getattr(TOOL_REGISTRY.get(tool_name), "meta", {}).get("percept")
            if _percept == "state":
                world.update(result, self.exec)
            elif _percept == "collected":
                try:
                    world.add_collected_bulk(result.get("entries") or [])
                except Exception:
                    pass
            if result.get("no_confidence"):
                gate.no_confidence = True

        def _summarize(old_items: List[Any], system_instructions: Optional[str] = None):
            """长任务压缩：复用大脑自身做摘要（与旧循环同一套语义）。

            T3.2：``system_instructions`` 为当前系统提示（可选），作为热前缀一并交给
            压缩器，使摘要与当前任务上下文保持一致；旧的单参调用方式完全兼容。
            """
            try:
                from omni_core.brain.llm import _to_chat_messages

                msgs = _to_chat_messages(None, old_items)
                # M8：压缩产物带来源写回共享黑板（兄弟分支也能看见被压掉的上下文）
                turn = self._compress_history(brain, [msgs], world, source=board_source,
                                              prefix=system_instructions or "")
                text = "\n".join(m.get("content", "") for m in turn if isinstance(m, dict))
                return text or None
            except Exception:
                return None

        # T4.7：把 request（temperature / max_tokens）真正下发给模型——此前 SDK 主链路
        # 未传 model_settings，配置里的 request.max_tokens 形同虚设（模型用服务端默认）。
        # 同时登记「输出触顶上限」回调：被截断不报错，只会产出半截 JSON/工具参数。
        _model_settings = None
        _max_output_tokens = 0
        try:
            _req = (self.brain_cfg or {}).get("request") or {}
            _mt = int(_req.get("max_tokens", 0) or 0)
            if _mt > 0:
                _max_output_tokens = _mt
                from agents import ModelSettings

                _model_settings = ModelSettings(
                    temperature=float(_req.get("temperature", 0.3) or 0.3),
                    max_tokens=_mt,
                )
        except Exception:
            _model_settings = None
            _max_output_tokens = 0

        def _on_truncate(used: int, limit: int) -> None:
            self._log(
                f"输出可能被截断：本次输出 {used} tokens 触达上限 {limit}（半截 JSON/"
                f"工具参数会导致工具调用失败），可调大 brain.request.max_tokens"
            )
            if traj is not None:
                try:
                    traj.log_think(
                        json.dumps({"kind": "output_truncated", "used": used,
                                    "limit": limit}, ensure_ascii=False),
                        "llm", "",
                    )
                except Exception:
                    pass

        # F4.1b/F4.2：尾部注入块——**仅记忆**（纪律文件已迁 system prompt）。
        # 构建一次，每请求由 Model 包装追加到压缩后的请求尾部（不落会话历史）。
        _tail_block, _tail_layers = self._build_memory_injection(is_sub)
        _on_inject = None
        if _tail_block:
            def _on_inject(chars, layers, _traj=traj):
                self._log_knowledge_injection(
                    _traj, {"kind": "memory_injected", "chars": chars, "layers": layers})
        res = run_subtask_sdk(
            brain,
            instructions=system_prompt,
            user_input=user_input or self._build_user(spec, world, step=0),
            tools=self.registry.sdk_tools(),
            gate=gate,
            mcp_servers=self.mcp_servers,
            max_steps=max_steps,
            wallclock_sec=wallclock_sec,
            task_mode=getattr(spec, "task_mode", "oneshot"),
            # F4.1b：纪律文件快照（run 级锁定）——仅供 run 中途漂移检测/告警，不改请求
            instructions_snapshot=_instr_snapshot,
            on_instructions_drift=_on_instr_drift,
            tail_inject_block=_tail_block,
            tail_inject_layers=_tail_layers,
            on_inject=_on_inject,
            verify_fail_max=int(self.escalation.get("verify_fail_max", 3)) if allow_escalate else 0,
            should_stop=self._is_stop_requested,
            on_step=_on_step,
            on_state=lambda s: self._set_state(AgentState(s)),
            compress_after=compress_after,
            # T3.2：粘性压缩（max_input_tokens>0）同样需要摘要回调，与旧 chunk 压缩共用
            summarize=_summarize if (compress_after or max_input_tokens) else None,
            allow_dispatch=allow_dispatch,
            budget_hint_ratio=budget_hint_ratio,
            history_keep=history_keep,
            compress_threshold=compress_threshold,
            hard_ceiling=hard_ceiling,
            ctx_window=ctx_window,
            prune_threshold=prune_threshold,
            prune_head=prune_head,
            prune_tail=prune_tail,
            max_input_tokens=max_input_tokens,
            retain_ratio=retain_ratio,
            history_items=history_items,
            todo_store=todo_store,
            skill_catalog=skill_catalog,
            on_llm=on_llm,
            on_llm_delta=on_llm_delta,
            on_turn_end=on_turn_end,
            # S1（stop 不生效修复）：回传当前 chunk 的 SDK Task 句柄，request_stop 硬取消
            on_sdk_task=self._set_sdk_task,
            max_output_tokens=_max_output_tokens,
            on_truncate=_on_truncate,
            model_settings=_model_settings,
        )
        # M7：主 agent 的派发计划（由 dispatch 元工具写到 gate 上）
        res["dispatch_plan"] = list(getattr(gate, "dispatch_plan", None) or [])

        # --- L2 统计与终态（不介入框架循环） ---
        # F1.2：指标回填——每次模型调用 +1（on_llm_end 钩子记账，跨块累计）；
        # 在线大脑（role=brain）才计 brain_calls，子 agent（executor）不计入（参照旧循环语义）。
        _llm = int(res.get("llm_calls", 0) or 0)
        self._decision_steps += _llm
        if role == "brain":
            self._brain_calls += _llm
        # F1.2：retry_count 接通真实重试计数（verify 连续失败 + repeat-guard 累计失败）
        self._retry_count += int(res.get("repeat_failures", 0) or 0)
        self._retry_count += gate.verify_count
        # 注：action_count 不在此回填——sdk_loop 每次真实工具调用经 on_tool_end 钩子
        # 触发 _on_step（本文件 line 1234 处 self._action_count += 1）已正确累计，
        # 与 brain_calls 同量级；此处若再 += res["steps"] 会重复计数。
        if res.get("escalated"):
            self._failures += 1
        try:
            world._last_verify_fail = gate.verify_count
        except Exception:
            pass
        # M7：DONE/FAILED 是「整轮任务」的终态。以下两种情况都不是整轮结束，
        # 否则主 agent 下一轮会从终态跳转，状态机不再合法（前端 SSE 也会误报结束）：
        #   - 子 agent 跑完（主 agent 还要继续）；
        #   - 主 agent 派发了子任务（控制权只是临时交去扇出）。
        if not is_sub and not res.get("dispatch_plan"):
            if res.get("paused"):
                self._set_state(AgentState.PAUSED)
            else:
                self._set_state(AgentState.DONE if res["success"] else AgentState.FAILED)
        return res

