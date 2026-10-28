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
from omni_core.tools import (
    build_plugin_registry,
    configure_shell,
    activate_environment,
    build_mcp_servers,
)
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
from omni_core.local.loop.instructions import InstructionMixin
from omni_core.local.loop.sdk_bridge import SdkBridgeMixin
from omni_core.local.loop.graph_runner import GraphRunnerMixin
from omni_core.local.loop.emitter import EmitterMixin
from omni_core.local.loop.finish import FinishMixin
from omni_core.local.loop import pool as loop_pool


# chat 模式的轻工具集（plan §3.1「轻工具 + dispatch」）：只给"说话 / 记东西 / 看场景 /
# 派活"四类。重执行（shell/文件/技能回放）与检索类工具在这一档不给——语音场景里
# 它们既慢又危险，真要干活由 dispatch 交给 worker。可被
# ``runtime.mode.chat_tools`` 覆盖（列表即白名单）。
# 元工具（task_done / verify / record / dispatch）由 build_meta_tools 单独装配，
# 不受此白名单约束——它们是收尾与派发机制，不是"能力"。
_CHAT_TOOLS = frozenset({
    "msg_send_text", "msg_send_card", "msg_recent",
    "memory_remember", "memory_recall", "memory_update", "memory_forget",
    "memory_overview",
    "read_scene", "read_task_digest",
})


def _schema_name(schema: Any) -> str:
    """从 OpenAI function schema 视图取工具名（兼容嵌套 / 平铺两种形态）。"""
    if not isinstance(schema, dict):
        return ""
    fn = schema.get("function")
    if isinstance(fn, dict):
        return str(fn.get("name") or "")
    return str(schema.get("name") or "")


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
        executor_cfg: Optional[dict] = None,
        escalation_cfg: Optional[dict] = None,
        on_debug=None,
        on_thinking=None,
        on_tool_call=None,
        on_llm_delta=None,
        full_access: Optional[bool] = None,
        config_snapshot: Optional[dict] = None,
        agent_id: str = "main",
        tools_enabled: bool = True,
        # 单链路参数化（plan §3.1）：auto(缺省) | chat | task —— 只改参数不改链路。
        # chat：轻工具集 + 人格版 system + 可选 voice 模型槽 + 不产 episode；
        # task/auto：全量工具 + 平台工作台 system + brain 主槽。
        mode: str = "auto",
        # 会话激活的人格（plan §3.1「人格 = 会话参数」）：检索/写入/画像三处都按它过滤。
        # 空 = 用配置里的缺省人格（单人设期的临时实现）。
        persona: str = "",
        # 本轮"派发时刻"（API 线程收到请求的时刻）：仅用于接线延迟打点
        # （llm_timing.since_dispatch_ms），不参与任何决策。
        dispatch_ts: float = 0.0,
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
        # S2 审批总开关（per-task「完全访问」）：graph_runner 绑定进 SecurityContext。
        # 此前为 inert 挂点（capability-unit 重构），S2 落地后转正。
        self.full_access = bool(full_access)
        # 能力门（运行级）：False = 纯对话运行——SDK Agent 的工具清单为空（能力插件 /
        # MCP / 内核元工具全部不暴露），模型请求里没有 tools 字段，物理上无法产生
        # tool_call。内核只接一个布尔，**不感知消息来源**；谁该关由调用方（网关/通道）决定。
        self.tools_enabled = bool(tools_enabled)
        # 模式（plan §3.1）：只决定参数（工具粗集 / system / 模型槽 / 写链取舍），
        # 不新增链路——所有模式跑同一个 run_task。
        _mode = str(mode or "auto").strip().lower()
        self.mode = _mode if _mode in ("auto", "chat", "task") else "auto"
        self.persona = str(persona or "")
        self.dispatch_ts = float(dispatch_ts or 0.0)
        self.ts_constructed = time.time()
        self._t_construct0 = time.time()
        _cfg = self._cfg
        _rt = (_cfg.get("runtime") or {})

        # 模型路由（2026-09-29）：端点唯一真源是 ~/.omniagent/models.json 目录，
        # 由调用方按槽位解析后注入；worker「目录里选了才启用」。
        # 传输层由 sdk_model 从组件池取共享 AsyncOpenAI：跨轮复用 httpx 连接池，
        # 免掉每轮 TCP/TLS 握手——首事件延迟的主要可优化项（per-run 状态仍在本对象上）。
        self.brain = LLMClient(brain_cfg, on_debug=self._dbg("brain"))
        self.brain_model = (brain_cfg or {}).get("model", "?")
        # T3.2：保留大脑端点配置原样，供读取模型级上下文上限（maxInputTokens）
        self.brain_cfg = dict(brain_cfg or {})
        # 环境（host / emulator …）：按 runtime.backend 单选激活；内核只经
        # ExecutionModule 使用契约三成员（kind / text_of / verify_done）。
        self.exec = activate_environment(_cfg)
        self.exec_model = "none"
        self.verbose = verbose
        self.brain_capabilities = (brain_cfg or {}).get("capabilities", {}) or {}
        # 思考路由模式（native/think-tag/none）：决定思考如何被采集与展示
        self.reasoning_mode = (brain_cfg or {}).get("reasoning_mode", "native")
        self.executor_reasoning_mode = (executor_cfg or {}).get("reasoning_mode", "native")
        # 主链长任务配置（无状态 run 时大脑侧不承载状态压缩，压缩由编排层管理）
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
        # 外部 MCP：交给 SDK 原生 MCPServer；连接生命周期由 sdk_loop 在运行期负责。
        # M1 常驻化：server 对象按 mcp.json 内容池化——同配置跨 run 复用**已连接**实例
        # （实测 connect 从 ~950ms 降到 2.8ms），断连由 sdk_loop 的探活自愈。
        _mcp_cfg = config.load_mcp_config()
        self.mcp_servers = loop_pool.mcp_servers(_mcp_cfg) if self.tools_enabled else []
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
        # 工具粗集（plan §3.1 渐进披露第一档）：mode 决定"能给哪一档"。
        # chat 只给轻工具 + dispatch（不给技能目录、不给重执行工具）；
        # auto/task 保持全量。白名单可配置（runtime.mode.chat_tools），缺省见 _CHAT_TOOLS。
        self.tool_allow: Optional[set] = None
        if self.mode == "chat":
            _allow = (_rt.get("mode") or {}).get("chat_tools")
            self.tool_allow = set(_allow if isinstance(_allow, list) and _allow else _CHAT_TOOLS)
        # schema 视图是嵌套形态 {"type":"function","function":{"name":...}}——
        # 直接 s.get("name") 恒 None，chat 档会被过滤成 0（踩过）
        self.tool_schemas = [s for s in self.registry.schemas
                             if self._tool_allowed(_schema_name(s))]
        self.env_kind = self.exec.kind

        # Layer 0（2026-09-30）：通用派发底座——执行单元注册表 slot→LLMClient。
        # 把「子 agent 是谁」从写死的 self.executor 升级为按逻辑名寻址的注册表；
        # 中枢（大脑）自行决定派给谁，匿名项回退默认槽，默认槽缺则主模型自派发。
        self.executors: Dict[str, LLMClient] = {}      # slot -> 执行单元
        self.executors_cfg: Dict[str, dict] = {}       # slot -> 端点配置
        self.exec_capabilities: Dict[str, dict] = {}   # slot -> 能力
        self.exec_models: Dict[str, str] = {}          # slot -> 模型名
        # 默认槽恒为 worker：匿名项走它；不在注册表 -> 主模型自派发（合法语义，见 plan §3.5）。
        self.default_executor_slot = "worker"
        # 向后兼容别名 / 观测标记（避免一次性改崩其他调用方）
        self.executor = None
        self.executor_capabilities = {}
        self.executor_is_planner = False  # 退化为纯观测标记（§3.5 门控解绑后不参与决策）
        self.executor_reasoning_mode = (executor_cfg or {}).get("reasoning_mode", "native")

        from omni_core.brain import router as model_router
        _dispatch_decl = (_rt.get("dispatch") or {})
        _declared_slots = list(_dispatch_decl.get("agents") or [])
        if executor_cfg is not None:
            # 直注入路径（测试与旧调用方 ToolLoop(executor_cfg=...)）：映射为 worker 槽
            _declared_slots = ["worker"]

        for _slot in _declared_slots:
            if _slot == "worker" and executor_cfg is not None:
                _slot_cfg = executor_cfg
            else:
                try:
                    _slot_cfg = model_router.resolve_slot(_slot)
                except Exception:  # noqa: BLE001
                    _slot_cfg = {}
            if not _slot_cfg:
                # 该用途未启用（目录无选择）：解析为空 dict 即语义「主模型兼任」
                continue
            try:
                _client = LLMClient(_slot_cfg, on_debug=self._dbg("executor"))
            except Exception as e:  # noqa: BLE001
                self._log(f"executor({_slot} 槽) 初始化跳过: {type(e).__name__}: {e}")
                continue
            # 健康检查按槽接管：不可达槽不入表（替代旧 M-fix 门控预判，见 plan §3.5 历史注记）。
            # M1：走池的 TTL 缓存——run 路径不再每轮同步等 2s 超时。
            if loop_pool.health_ok(_slot_cfg.get("base_url")) is False:
                _dbg = self._dbg("executor")
                if _dbg:
                    _dbg(f"executor_unavailable:{_slot}", {
                        "title": f"executor({_slot} 槽) 本地模型不可用，回退主模型",
                        "model": _slot_cfg.get("model", "none"),
                        "base_url": _slot_cfg.get("base_url", ""),
                        "reason": "端点不可达（本地模型未启动？），该槽子任务将由主模型执行",
                    })
                continue
            self.executors[_slot] = _client
            self.executors_cfg[_slot] = _slot_cfg
            self.exec_capabilities[_slot] = (_slot_cfg or {}).get("capabilities", {}) or {}
            self.exec_models[_slot] = _slot_cfg.get("model", "none")

        # 向后兼容别名：worker 槽在注册表 -> 用 worker；否则主模型兼任（自派发）
        self.executor = self.executors.get(self.default_executor_slot) or self.brain
        if self.default_executor_slot in self.executors:
            self.executor_capabilities = self.exec_capabilities.get(self.default_executor_slot) or {}
            self.executor_is_planner = False
            self.executor_reasoning_mode = (
                self.executors_cfg[self.default_executor_slot].get("reasoning_mode", "native")
            )
        else:
            # 默认槽缺 -> 主模型兼任，匿名项回退 brain 自派发（合法语义，非降级）
            self.executor_capabilities = self.brain_capabilities
            self.executor_is_planner = True
        self.exec_model = self.exec_models.get(self.default_executor_slot) or self.brain_model

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
        # M8（修复）：SDK 请求级人类注入通道（运行期投递主通道；见 inject_message / _human_channel）
        self._human_inj_channel = None
        self.traj_cfg = _traj_cfg


        # §3.4 知识层弱注入配置（runtime.knowledge.memory.enabled，默认关闭）
        # 严格遵守诚实基线：验证期不默认生效，开启后才在系统提示尾部追加知识块。
        # 注：原 knowledge.skills 弱匹配召回已随 T2.4（O4' 技能目录化）移除，
        #     技能改由目录 + load_skill 按需加载，不再有独立开关。
        _knowledge_cfg = (_rt.get("knowledge") or {})
        self.knowledge_cfg = {
            "memory": bool((_knowledge_cfg.get("memory") or {}).get("enabled", False)),
            # P0 用户画像：独立注入块，默认开（空文件零注入，不改变现有行为）
            "profile": bool((_knowledge_cfg.get("profile") or {}).get("enabled", True)),
        }

        # 系统提示：用当前模型的真实能力 + 工具清单动态组装（模型无关）。
        self.messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self._base_prompt()}
        ]
        self.step = 0

        # M4a.4 状态机：当前状态 + 转移序列（仅追踪，不改行为）
        self.state = AgentState.INIT
        self._state_seq: List[str] = []

        # M4a.1/2 运行级计数器（供 RunRecord + telemetry；每次 run 重置）
        self._brain_calls = 0       # 主链 brain 客户端被调用次数（子 agent 调用不计入）
        self._decision_steps = 0    # 决策步数（每步向大脑要一次决策）
        self._action_count = 0      # 派发工具次数
        self._retry_count = 0       # 无效动作次数（升级 / verify 失败）
        self._failures = 0          # 失败次数（升级 + run 级失败）
        self._recoveries = 0        # 失败后恢复次数（升级后仍有子任务完成）

        # M1 接线归因：构造段耗时落日志（pool 预热后应稳定在个位~几十 ms；
        # 若此处仍然大，说明还有每 run 的重对象没进池）
        self._log(f"loop 构造耗时 {int((time.time() - self._t_construct0) * 1000)}ms "
                  f"(mode={self.mode}, tools={len(self.tool_schemas)}, "
                  f"mcp={len(self.mcp_servers)})")

    def _tool_allowed(self, name: Optional[str]) -> bool:
        """工具粗集门（mode 决定档位；None = 全量放行）。

        与 ``tools_enabled`` 的关系：后者是"要不要工具"（外部通道 fail-closed），
        本方法是"要哪一档"（mode 渐进披露）。两者都与具体平台/来源无关。
        """
        if self.tool_allow is None:
            return True
        return str(name or "") in self.tool_allow

    def _sdk_tools(self) -> List[Any]:
        """本轮暴露给 SDK 的工具：池化的包装结果 + mode 粗集过滤。

        ``tools_enabled=False``（外部通道纯对话）→ 空列表（能力门优先于 mode 门）。
        """
        if not getattr(self, "tools_enabled", True):
            return []
        return [t for t in loop_pool.sdk_tools(self.registry)
                if self._tool_allowed(getattr(t, "name", ""))]

    def _base_prompt(self, runtime_context: Optional[dict] = None) -> str:
        """主链 system 基线：mode 决定「人格版」还是「平台工作台版」（plan §3.1）。

        两版共用能力块与运行时上下文注入（换模型 / 换环境同步生效），差别只在纪律
        模板：chat 版对话优先、要干活就派出去；task/auto 版是干活那套。
        """
        if self.mode == "chat":
            from omni_core.brain.prompt import build_voice_prompt

            return build_voice_prompt(self.brain_capabilities, runtime_context=runtime_context)
        return build_system_prompt(self.brain_capabilities, self.tool_schemas,
                                   self.reasoning_mode, runtime_context=runtime_context)

    def _log(self, *a):
        if self.verbose:
            print("[loop]", *a, flush=True)

    def _set_state(self, s: AgentState) -> None:
        # 纯追踪层：连续相同状态去重（如 task_done 早退已置 DONE，post-loop / 编排收尾 DONE
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

    def _human_channel(self) -> Any:
        """惰性创建并复用人类注入通道（避免模块加载期循环导入）。

        通道对象存活于 AgentCore 实例上，跨图回合 / 暂停恢复持续有效——
        人类消息在下一模型请求边界被取走，不依赖 LangGraph checkpoint。
        """
        if self._human_inj_channel is None:
            from omni_core.brain.sdk_loop import _HumanInjection
            self._human_inj_channel = _HumanInjection()
        return self._human_inj_channel

    def inject_message(self, text: str) -> bool:
        """M8 软注入（修复）：运行中插入人类纠偏，**不打断当前 turn**，下一请求即见。

        实现从「LangGraph checkpoint 双写」改为「SDK 请求级用户消息通道」：
        旧方案把消息写进 checkpoint，但单次 ``ainvoke`` 期间框架不回读新 checkpoint，
        导致长任务（单图回合内跑完）永远看不到注入——即「发送后模型不回应」。

        新方案：消息进入 ``_HumanInjection`` 通道，由运行中的 SDK 主循环在**下一次
        模型请求前**取走并作为 ``role=user`` 消息拼进输入；不打断当前工具链，与主流
        agent 的请求级注入范式一致。主 agent 路径（``_run_main``）透传该通道；子 agent
        路径不传，避免重复消费。

        Args:
            text: 人类指示原文。

        Returns:
            是否注入成功（图未运行 / 空消息 / 投递失败时返回 False）。
        """
        if not text or not text.strip():
            return False
        if self._graph is None or self._graph_config is None:
            return False
        note = text.strip()
        try:
            self._human_channel().push(note)
        except Exception as e:
            self._log(f"[inject] 投递失败: {type(e).__name__}: {e}")
            return False
        return True

    def drain_human_injections(self) -> List[Dict[str, Any]]:
        """取走通道里**未被消费**的人类注入（仅在 run 结束后调用；正常消费发生在请求边界）。

        收尾补投用：本轮最后一次模型请求之后注入的消息不会再有请求边界，
        留在通道里会随 loop 销毁而丢失（表现为「消息落库但模型不回应」）。
        """
        if self._human_inj_channel is None:
            return []
        return self._human_inj_channel.take_messages()

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
        # M8（修复）：丢弃上一任务滞留的人类注入，避免泄漏到下一任务
        self._human_inj_channel = None

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

