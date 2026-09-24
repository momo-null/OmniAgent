"""ToolLoop 主类（拆分后的聚合壳）：保留 __init__ 与基础设施方法。

其余方法经 Mixin 继承；逐字搬移，零逻辑改动。
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
from omni_core.tools import (
    build_plugin_registry,
    configure_shell,
    activate_environment,
    build_mcp_servers,
    configure_local_model_from_config,
)
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
from omni_core.local.loop.instructions import InstructionMixin
from omni_core.local.loop.sdk_bridge import SdkBridgeMixin
from omni_core.local.loop.graph_runner import GraphRunnerMixin
from omni_core.local.loop.emitter import EmitterMixin
from omni_core.local.loop.finish import FinishMixin


class ToolLoop(
    InstructionMixin,
    SdkBridgeMixin,
    GraphRunnerMixin,
    EmitterMixin,
    FinishMixin,
):
    def __init__(
        self,
        brain_cfg: dict,
        verbose: bool = True,
        max_history: int = 0,
        executor_cfg: Optional[dict] = None,
        escalation_cfg: Optional[dict] = None,
        on_debug=None,
        on_thinking=None,
        on_tool_call=None,
        on_llm_delta=None,
        full_access: Optional[bool] = None,
        config_snapshot: Optional[dict] = None,
        agent_id: str = "main",
    ):
        # 详细日志通道：内核把 prompt/response/react 旁路推给前端（右栏「运行日志」）
        self.on_debug = on_debug
        # 对话区实时通道：把「思考 + 工具调用」推到对话框（左栏），与 debug 旁路分离
        self.on_thinking = on_thinking
        self.on_tool_call = on_tool_call
        # 真流式打字机（问题3-B）：每 token 增量回调（已绑定 role/block_id，由调用方注入）
        self.on_llm_delta = on_llm_delta
        # 待关联的工具调用参数：emitter 在每轮 LLM 响应后缓存，on_step 实际执行时按名匹配
        self._pending_calls: List[tuple] = []
        # 阶段 0.5：运行级配置快照——优先用调用方注入的 run 级快照，
        # 运行期内不再重读全局 config（设置面板运行期变更不影响本运行）。
        self._cfg = config_snapshot if config_snapshot is not None else (config.load_config() or {})
        self.agent_id = agent_id
        _cfg = self._cfg
        _rt = (_cfg.get("runtime") or {})

        # M9：取消 runtime.mode 三态。这里只做「多模型接入」——
        # 主模型（brain）可以在线，也可以直接配成本地端点；是否另起本地模型由
        # runtime.executor（子 agent 模型）与 llm.local_as_tool（本地模型工具）决定。
        self.brain = LLMClient(brain_cfg, on_debug=self._dbg("brain"))
        self.brain_model = (brain_cfg or {}).get("model", "?")
        # T3.2：保留大脑端点配置原样，供读取模型级上下文上限（maxInputTokens）
        self.brain_cfg = dict(brain_cfg or {})
        # 环境（host / emulator …）：按 runtime.backend 单选激活；内核只经
        # ExecutionModule 使用契约三成员（kind / text_of / verify_done）。
        self.exec = activate_environment(_cfg)
        self.exec_model = "none"
        # max_history: 单大脑模式保留最近 N 轮（0=不裁剪，适合云端大脑大上下文）。
        # 本地小模型（如 4B@8K ctx）设为较小值可防止多步后上下文溢出；
        # worker（两层内层 loop）改用 escalation.worker_history_keep（按轮裁剪，语义更清晰）。
        self.max_history = max_history
        self.verbose = verbose
        self.brain_capabilities = (brain_cfg or {}).get("capabilities", {}) or {}
        # 思考路由模式（native/think-tag/none）：决定思考如何被采集与展示
        self.reasoning_mode = (brain_cfg or {}).get("reasoning_mode", "native")
        self.executor_reasoning_mode = (executor_cfg or {}).get("reasoning_mode", "native")
        # 大脑长任务配置（仅单大脑 run_task 模式用；两层模式下大脑无状态，无需压缩）
        self.brain_long_task = (brain_cfg or {}).get("long_task", {}) or {}
        # M8 长任务节奏：预算用到该比例时给 agent 一条陈述性提示（0 = 关闭）
        self.budget_hint_ratio = float(
            ((_rt.get("long_task") or {}).get("budget_hint_ratio", 0.75)) or 0.0
        )

        # 视觉通道已彻底插件化（plugins/vision）：运行时由插件 startup 自构造，
        # 内核不再构造 / 读取 runtime.vision，也不注入 runtime["vision"]。
        self.runtime = {
            "exec": self.exec,
        }

        # 工具清单 = 环境自带（env）+ 插件（plugin）+ 内核 builtin（core）。
        # 环境已在上方按 runtime.backend 激活；下面装载插件与内建限流。
        _tools_cfg = (_rt.get("tools") or {})
        configure_shell(_rt.get("shell_exec") or {})
        # M9：本地模型以工具形态注入（是否暴露由 llm.local_as_tool.enabled 决定）。
        self.local_model_as_tool = configure_local_model_from_config(_cfg, on_debug=self._dbg("local_model"))
        # 外部 MCP：交给 SDK 原生 MCPServer；连接生命周期由 sdk_loop 在运行期负责。
        _mcp_cfg = config.load_mcp_config()
        self.mcp_servers = (
            build_mcp_servers(_mcp_cfg.get("servers"), log=self._log)
            if _mcp_cfg.get("enabled")
            else []
        )
        # 插件装载：扫 plugin_dirs，按各插件自有 enabled 决定注册与否；单包失败只降级。
        # 插件只拿到只读 env_kind，**不拿环境厚句柄**（见 PluginContext）。
        from omni_core.tools.loader import PluginContext, load_plugins
        self.plugin_report = load_plugins(
            _cfg,
            ctx=PluginContext(config=_cfg, wired=True, env_kind=self.exec.kind),
        )
        if self.plugin_report.failed:
            self._log(
                "插件装载失败 %d 个: %s"
                % (
                    len(self.plugin_report.failed),
                    ", ".join(str(f.get("name")) for f in self.plugin_report.failed),
                )
            )
        self.registry = build_plugin_registry()
        self.tool_schemas = self.registry.schemas
        self.env_kind = self.exec.kind

        # 第二路模型：子 agent（worker）用的模型，可配本地高频模型
        self.executor = None
        self.executor_capabilities = {}
        self.executor_is_planner = False  # True 表示子 agent 模型与主模型同源
        # M-new：子 agent 模型同样经 resolver 解析（新 schema 优先，回退旧 executor）
        if executor_cfg is None:
            from omni_core.brain.resolve import resolve_agent_model
            exec_cfg = resolve_agent_model(_cfg, "worker")
        else:
            exec_cfg = executor_cfg
        if exec_cfg and exec_cfg.get("enabled"):
            try:
                self.executor = LLMClient(exec_cfg, on_debug=self._dbg("executor"))
                self.executor_capabilities = (exec_cfg or {}).get("capabilities", {}) or {}
                self.exec_model = exec_cfg.get("model", "none")
            except Exception as e:
                self._log(f"executor(本地4B) 初始化跳过: {type(e).__name__}: {e}")
        # M-fix：executor 启用但本机端点不可达 → 回退主模型，避免子任务静默失败烧步数
        if self.executor is not None and self.executor is not self.brain:
            if model_health_ok(exec_cfg.get("base_url")) is False:
                dbg = self._dbg("executor")
                if dbg:
                    dbg("executor_unavailable", {
                        "title": "executor 本地模型不可用，回退主模型",
                        "model": self.exec_model,
                        "base_url": exec_cfg.get("base_url", ""),
                        "reason": "端点不可达（本地模型未启动？），子任务将由主模型直接执行",
                    })
                self.executor = None
        # 未独立配置子 agent 模型：由主模型兼任（默认单 agent 时本就用不到）。
        if self.executor is None:
            self.executor = self.brain
            self.executor_capabilities = self.brain_capabilities
            self.exec_model = self.brain_model
            self.executor_is_planner = True

        # M3b.4 升级阈值：默认 + config 覆盖
        self.escalation = {**_DEFAULT_ESCALATION, **(escalation_cfg or (_rt.get("escalation") or {}))}

        # M4a 轨迹落盘配置（runtime.trajectory.*；资产跟 task_id 走）
        _traj_cfg = (_rt.get("trajectory") or {})
        self.traj_enabled = bool(_traj_cfg.get("enabled", True))

        # 停止标志：用户主动停止（POST /api/runtime/stop）置位，内层循环每步检查后优雅退出
        self._stop_requested = False
        # M8：编排运行期句柄（软注入 / 硬停止需要）
        self._graph: Any = None
        self._graph_config: Optional[Dict[str, Any]] = None
        self._run_task: Any = None
        # S1（stop 不生效修复）：当前 chunk 的 SDK Task 句柄（Runner.run/run_streamed，
        # 由 run_subtask_sdk 经 on_sdk_task 回传；request_stop 据此硬取消，防僵尸 run）
        self._sdk_task: Any = None
        # M8：待投递的人类注入（运行期通道，见 inject_message 文档）
        self._injections: List[str] = []
        self._injections_lock = threading.Lock()
        self.traj_cfg = _traj_cfg

        # M4b.3 Curator 配置（runtime.curator.*；触发式，任务完成后跑一次）
        _cur_cfg = (_rt.get("curator") or {})
        self.curator_enabled = bool(_cur_cfg.get("enabled", True))

        # §3.4 知识层弱注入配置（runtime.knowledge.memory.enabled，默认关闭）
        # 严格遵守诚实基线：验证期不默认生效，开启后才在系统提示尾部追加知识块。
        # 注：原 knowledge.skills 弱匹配召回已随 T2.4（O4' 技能目录化）移除，
        #     技能改由目录 + load_skill 按需加载，不再有独立开关。
        _knowledge_cfg = (_rt.get("knowledge") or {})
        self.knowledge_cfg = {
            "memory": bool((_knowledge_cfg.get("memory") or {}).get("enabled", False)),
        }

        # 系统提示：用当前模型的真实能力 + 工具清单动态组装（模型无关）。
        self.messages: List[Dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(self.brain_capabilities, self.tool_schemas, self.reasoning_mode)}
        ]
        self.step = 0

        # M4a.4 状态机：当前状态 + 转移序列（仅追踪，不改行为）
        self.state = AgentState.INIT
        self._state_seq: List[str] = []

        # M4a.1/2 运行级计数器（供 RunRecord + telemetry；每次 run 重置）
        self._brain_calls = 0       # 在线大脑被调用次数（worker 本地调用不计入）
        self._decision_steps = 0    # 决策步数（每步向大脑要一次决策）
        self._action_count = 0      # 派发工具次数
        self._retry_count = 0       # 无效动作次数（升级 / verify 失败）
        self._failures = 0          # 失败次数（升级 + run 级失败）
        self._recoveries = 0        # 失败后恢复次数（升级后仍有子任务完成）

    def _log(self, *a):
        if self.verbose:
            print("[loop]", *a, flush=True)

    def _set_state(self, s: AgentState) -> None:
        # 纯追踪层：连续相同状态去重（如 task_done 早退已置 DONE，post-loop / 两层整体 DONE
        # 再置一次——状态未变，记录重复边无信息量且会破坏合法转移断言）。
        if self._state_seq and self._state_seq[-1] == s.value:
            self.state = s
            return
        self.state = s
        self._state_seq.append(s.value)

    def _reset_run_counters(self) -> None:
        self._brain_calls = 0
        self._decision_steps = 0
        self._action_count = 0
        self._retry_count = 0
        self._failures = 0
        self._recoveries = 0

    def request_stop(self) -> None:
        """由外部（API /stop）调用，请求当前运行中的 loop 退出。

        M8 双通道：
        - 硬停止：直接 cancel 编排图的 Task（当前 turn 立刻中断）；
        - 兜底：置位标志，单大脑模式与子 agent 循环每步协作式检查退出。

        S1（2026-09-21 stop 不生效修复）：在 cancel graph 任务之外，再 cancel
        当前 chunk 的 SDK Task（Runner.run / run_streamed，经 on_sdk_task 句柄
        回传）。此前只取消 graph 编排任务，碰不到 async-bridge loop 上正在跑的
        Runner——graph 侧已落「用户主动停止」而 Runner 继续调 MCP（僵尸 run）。
        句柄逐 chunk 更新；对已完成的句柄 cancel 为 no-op，安全。
        """
        self._stop_requested = True
        from omni_core.async_bridge import cancel_task
        cancel_task(self._run_task)
        cancel_task(self._sdk_task)

    def _set_sdk_task(self, task: Any) -> None:
        """S1：接收 run_subtask_sdk 回传的当前 chunk Task 句柄（request_stop 硬取消用）。"""
        self._sdk_task = task

    def inject_message(self, text: str) -> bool:
        """M8 软注入：运行中插入人类纠偏，**不打断当前 turn**。

        双写（两者缺一不可，原因见下）：
        1. LangGraph `update_state` → 写进 checkpoint（`injected` 通道）：
           持久化、可观测，暂停/恢复场景由框架投递；
        2. 运行期投递队列 → 主 agent 下一轮开始时取走。

        为什么还要 2：本版本的 Pregel 在一次 `ainvoke` 期间沿用自己的内存态，
        外部 `update_state` 写的新 checkpoint 不会被这次正在跑的调用读到
        （已实测：只写 update_state 时主 agent 看不到注入）。因此运行期投递
        是必需的补充；checkpoint 侧仍保留，作为持久记录与恢复通道。

        Args:
            text: 人类指示原文。

        Returns:
            是否注入成功（图未运行 / 无 checkpointer 时返回 False）。
        """
        if not text or not text.strip():
            return False
        if self._graph is None or self._graph_config is None:
            return False
        note = text.strip()
        with self._injections_lock:
            self._injections.append(note)
        try:
            from omni_core.async_bridge import run_async
            run_async(self._graph.aupdate_state(self._graph_config, {"injected": [note]}))
        except Exception as e:
            # 写 checkpoint 失败不影响运行期投递（主 agent 仍会收到）
            self._log(f"[inject] 写入图状态失败（运行期投递仍生效）: {type(e).__name__}: {e}")
        return True

    def _drain_injections(self) -> List[str]:
        """取走并清空待投递的人类注入（主 agent 每轮开始时调用）。"""
        with self._injections_lock:
            items = list(self._injections)
            self._injections.clear()
        return items

    def reset_stop(self) -> None:
        self._stop_requested = False
        self._graph = None
        self._graph_config = None
        self._run_task = None
        self._sdk_task = None

    def _is_stop_requested(self) -> bool:
        return self._stop_requested

    def _make_store(self, task_id: str, run_id: Optional[str] = None) -> Optional[TrajectoryStore]:
        if not self.traj_enabled:
            return None
        if not task_id:
            return None  # 空 task_id（如单元测试 mock）：不落盘轨迹
        try:
            # B2（2026-09-22）：透传统一 run_id，使 TrajectoryStore 落盘的 run.json
            # 与 world.run_id / task.json.runs 三处 ID 完全一致（消除双源分裂 / 悬空）。
            # run_id 为 None 时 TrajectoryStore 保留原有自生成逻辑（向后兼容）。
            return TrajectoryStore(task_id, run_id)
        except Exception as e:
            self._log(f"trajectory store 初始化失败，本次不落盘: {type(e).__name__}: {e}")
            return None

    def _verify(self, spec: TaskSpec, world: WorldModel, condition: str = "") -> tuple:
        """M4a.3 / §9 显式校验：目标是否已达成。

        去场景化：完成判定委托给环境（``self.exec.verify_done``），
        内核不再直接读取 ocr_text / active_window 等屏幕字段（红线）；
        各环境自定命中语义（host 子串 / emulator 文字列表元素命中等）。

        无可校验条件时返回 (False, '无可校验条件')——verify 工具据此计 verify_fail；
        task_done 门控调用方须自行判断「无条件→信任大脑」。

        Returns:
            (passed: bool, reason: str)
        """
        cond = condition or spec.expected or spec.done_when
        if not cond:
            return False, "无可校验条件"
        percept = world.current_percept if hasattr(world, "current_percept") else {}
        return self.exec.verify_done(cond, percept)

    def _recheck_batch(self, prev_results, world) -> None:
        """T4.4（U5c）：对标记 ``need_verify`` 的子任务批次做二次校验（下一轮前）。

        仅成功回收的批次才复核（失败的本来就未达成）；校验不通过只打标记
        ``recheck="fail"``，保留未完成语义交由模型决策——**不做强制拦截、
        不做惩罚**（不翻转 success、不升级）。默认（无 need_verify 标记）
        完全零干预，行为回归基线。
        """
        for r in (prev_results or []):
            if not isinstance(r, dict) or not r.get("need_verify"):
                continue
            if not r.get("success"):
                continue
            cond = r.get("done_when") or ""
            try:
                passed, why = self._verify(_recheck_spec(cond), world)
            except Exception as e:
                passed, why = False, f"复核异常: {type(e).__name__}: {e}"
            r["recheck"] = "pass" if passed else "fail"
            r["recheck_reason"] = str(why or "")

    def _build_user(self, spec: TaskSpec, world: WorldModel, step: int = 0) -> str:
        """组装给大脑的用户消息。首轮仅含**任务目标与完成条件**；后续轮次仅一句中性继续。

        §9 / B1（loop 只编排、不替模型观察）：**不命令模型何时观察**——
        感知工具（observe / read_screen_text / …）与其它工具平级，由模型按需自主调用；
        环境身份已由 system prompt 的「运行时上下文 - 当前环境」给出。
        """
        if step == 0:
            base = (
                f"【任务目标】{spec.objective}\n"
                f"【完成条件】{spec.done_when or '(由你判断)'}"
            )
            # B1：续跑上下文（N8 崩溃恢复）置于目标之后，让大脑先知道已完成的部分；
            # 仅陈述事实、不含行为命令（同一原则见本函数 docstring / §9）
            hint = getattr(world, "_resume_hint", "") or ""
            if hint:
                base = f"{base}\n\n{hint}"
            return base
        # 后续轮次：**不再注入任何环境块**——observe 已是被声明为 percept 的普通工具，
        # 由大脑自主调用；也不重复目标，大脑从历史消息中已有完整上下文。
        return "继续。若已达成，调 task_done。"

