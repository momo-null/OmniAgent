"""MCP 元工具（L1 激活层）：``mcp_catalog`` / ``mcp_activate`` / ``mcp_call``。

设计见 ``doc/plans/implemented/mcp-lazy-activation-2026-11-13.md`` §6.3–§6.4。三条纪律：

1. **不预连接、不预注入**：``McpSession`` 只持有 lazy server 的**对象**（构造不连接），
   模型调 ``mcp_activate`` 才 connect + 探活 + ``agent.mcp_servers.append``。
2. **注入靠 SDK 的每次 run 重新枚举**：``Agent.get_mcp_tools()`` 每次 ``Runner.run`` 都读
   ``agent.mcp_servers``（``agents/agent.py:250``），所以 append 后**下一个块**自动生效，
   不需要重建 Agent、不需要改 ``while`` 循环。同块内要连续调用走 ``mcp_call``。
3. **失败不抛、给状态和恢复动作**：``{ok:false, state, reason, hint}``。未登录 / 服务没起
   都是正常返回值，模型可以原样转述给用户——这比异常有用。

工具不在模块导入时注册进 ``TOOL_REGISTRY``（否则 mcp 关闭时也会污染全局工具面），
而是由 ``build_mcp_tools(session)`` 在子任务装配时按需构造（``_meta_tool`` 只走
``wrap_tool`` + SDK ``function_tool``，不调 ``register_tool``）。
"""
from __future__ import annotations

import importlib
import threading
from typing import Any, Dict, List, Optional

from omni_core.tools import mcp_state
from omni_core.tools.mcp_servers import build_mcp_servers, mode_of, server_name

# 工具描述里给出的下一步动作：弱模型容易"激活了不调"，靠明确指令补一刀
_NEXT_HINT = "工具已就绪，直接按名字调用（无需再次 activate）。"


def _meta_tool(name: str, description: str):
    """构造**不注册**的内核元工具（与 ``base.function_tool`` 同款包装链，少一步注册）。

    为什么不用 ``base.function_tool``：那个装饰器在 import 期就把工具写进全局
    ``TOOL_REGISTRY``，会让 mcp 关闭的运行也带 3 个用不上的 schema。元工具是 per-run
    装配物，必须能"不出现"。
    """
    def deco(func):
        from agents import function_tool as sdk_function_tool

        from omni_core.tools.policy import wrap_tool

        # 与 SDK 原生 MCP 派发同级，不声明 risk：审批收口是另一条线（方案 §2 非目标）
        guarded = wrap_tool(func, tool_name=name, unit="mcp")
        return sdk_function_tool(
            guarded,
            name_override=name,
            description_override=description or None,
            strict_mode=False,
            use_docstring_info=True,
        )
    return deco


class ActivationSlot:
    """一个 server 的"当前激活子集"槽（``None`` = 不限，用 include 白名单全量）。

    为什么单独一个可变对象：SDK 的 ``tool_filter`` 在 server **构造时**绑定，而激活发生在
    运行中。让 filter 闭包读这个槽，就能"构造一次、动态变化"——因为 SDK 每次
    ``list_tools`` 都重新应用 filter（``agents/mcp/server.py:1002``）。
    """

    __slots__ = ("allowed",)

    def __init__(self):
        self.allowed: Optional[set] = None


class McpSession:
    """一个子任务内的 lazy MCP 激活状态（per-run，不进池、不跨 run 共享）。

    持有 ``agent`` 的**只读引用**（构造后回填），激活时 append server；同时写
    ``ActivationSlot``，让 server 的动态 filter 在下次 ``list_tools`` 时生效。

    Args:
        cfgs: lazy 配置列表（mcp.json 的 servers 段子集）。
        servers: 已构造的 SDK server 对象（ToolLoop 级复用；None 则自建）。
        slots: ``{name: ActivationSlot}``（与 server 构造方共享同一批槽）。
        log: 可选日志回调。
    """

    def __init__(self, cfgs: Optional[List[dict]] = None, servers: Optional[Dict[str, Any]] = None,
                 slots: Optional[Dict[str, ActivationSlot]] = None, log=None):
        self.cfgs: Dict[str, dict] = {}
        for cfg in (cfgs or []):
            if isinstance(cfg, dict):
                self.cfgs[server_name(cfg)] = cfg
        self.slots: Dict[str, ActivationSlot] = slots if slots is not None else {
            name: ActivationSlot() for name in self.cfgs}
        self.servers: Dict[str, Any] = {}
        for srv in (servers.values() if isinstance(servers, dict) else (servers or [])):
            key = str(getattr(srv, "name", "") or "")
            if key:
                self.servers[key] = srv
        self.agent: Any = None
        self.armed: set = set()
        self._lock = threading.RLock()
        self._log = log or (lambda _m: None)
        self._closed = False

    # ------------------------------------------------------------ 装配
    def build_servers(self) -> List[Any]:
        """按 lazy 配置构造 server（不连接），并把 dynamic filter 绑到本 session。

        仅在没有现成 server 对象时用（测试 / 独立调用）；生产路径由 ToolLoop 构造一次、
        跨子任务复用，避免每个子任务重复建对象。
        """
        built = build_mcp_servers(list(self.cfgs.values()), activation=self.filter_for)
        for srv in built:
            key = str(getattr(srv, "name", "") or "")
            if key:
                self.servers[key] = srv
        return built

    def filter_for(self, name: str) -> Optional[set]:
        """当前已激活的工具子集；None = 不限（用 include 白名单全量）。"""
        slot = self.slots.get(str(name))
        return slot.allowed if slot is not None else None

    def bind(self, agent: Any) -> None:
        """回填 Agent 引用（激活时要往它的 ``mcp_servers`` 里 append）。"""
        self.agent = agent

    # ------------------------------------------------------------ 激活
    def activate(self, server: str, tools: Optional[List[str]] = None) -> Dict[str, Any]:
        """连接 + 探活 + 注入（下一块生效）。**未 ready 不注入**——避免模型白调。"""
        name = str(server or "").strip()
        if not self.cfgs:
            return {"ok": False, "state": mcp_state.UNKNOWN,
                    "error": "mcp.json 里没有 lazy server（或 mcp 未启用）"}
        if name not in self.cfgs:
            return {"ok": False, "state": mcp_state.UNKNOWN,
                    "error": f"未知或非 lazy 的 server: {name}",
                    "available": sorted(self.cfgs.keys())}
        srv = self.servers.get(name)
        if srv is None:
            return {"ok": False, "state": mcp_state.UNKNOWN,
                    "error": f"server 对象未构造: {name}"}

        with self._lock:
            if name in self.armed and not self._closed:
                return {"ok": True, "state": mcp_state.READY, "already": True,
                        "tools": self._describe(name), "next": _NEXT_HINT}

        cfg = self.cfgs[name]
        budget = float(cfg.get("probe_budget_s") or mcp_state.DEFAULT_BUDGET_S)
        # 就绪探针默认**不跑**：实测有的探针要 ~11s（要驱动浏览器），
        # 放进激活同步路径会让工具调用卡死。要严格前置就显式开
        # probe_readiness_on_activate（并把 probe_budget_s 放大到 ≥12）。
        st = mcp_state.probe(
            name, cfg, srv, force=True, budget_s=budget,
            with_readiness=bool(cfg.get("probe_readiness_on_activate")))
        if not st.ready:
            self._log(f"[mcp] activate {name} 失败：{st.state}/{st.reason}")
            return {
                "ok": False, "state": st.state, "reason": st.reason, "hint": st.hint,
                # error 给模型一句话结论（reason 是机器字段，模型未必读得懂）
                "error": f"{st.state}/{st.reason}" if st.reason else st.state,
                "latency_ms": st.latency_ms,
                # 给模型一个"别急着重试"的节奏：offline 冷启动通常几秒
                "retry_after_sec": 15 if st.state == mcp_state.OFFLINE else 30,
                "note": "服务不可用时不注入工具；按 hint 处理后重新 activate 即可",
            }

        allow: Optional[set] = None
        if tools:
            allow = {str(t) for t in tools if str(t)}
        with self._lock:
            slot = self.slots.get(name)
            if slot is None:
                slot = self.slots[name] = ActivationSlot()
            slot.allowed = allow
            self.armed.add(name)
            if self.agent is not None:
                try:
                    current = list(getattr(self.agent, "mcp_servers", []) or [])
                    if srv not in current:
                        current.append(srv)
                        self.agent.mcp_servers = current
                except Exception as e:  # noqa: BLE001 —— 注入失败不该让激活整体失败
                    self._log(f"[mcp] {name} 注入 agent 失败：{type(e).__name__}: {e}")
        try:
            srv.invalidate_tools_cache()      # 子集专用，避免拿到激活前的全量
        except Exception:
            pass
        self._log(f"[mcp] activate {name} ok（{st.latency_ms}ms，"
                  f"{len(self._describe(name))} 个工具）")
        return {"ok": True, "state": mcp_state.READY, "latency_ms": st.latency_ms,
                "tools": self._describe(name), "next": _NEXT_HINT}

    def _describe(self, name: str) -> List[Dict[str, Any]]:
        """已激活工具的精简说明（名字 / 一句描述 / 必填参数名+类型）。

        不回传完整 JSON Schema：激活后 SDK 会自己注入，重复回传纯浪费 token。
        """
        srv = self.servers.get(name)
        if srv is None:
            return []
        try:
            # 统一走 mcp_state.list_tools：动态 filter 在上下文外裸调会抛 UserError
            listed = mcp_state.list_tools(srv, timeout_s=3.0)
        except Exception:
            listed = []
        allow = self.filter_for(name)
        out: List[Dict[str, Any]] = []
        for t in listed or []:
            tname = str(getattr(t, "name", "") or "")
            if not tname:
                continue
            if allow is not None and tname not in allow:
                continue
            schema = getattr(t, "inputSchema", None) or {}
            props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
            required = list(schema.get("required") or []) if isinstance(schema, dict) else []
            desc = str(getattr(t, "description", "") or "").strip().replace("\n", " ")
            out.append({
                "name": tname,
                "desc": desc[:140],
                "required": [{"name": str(k),
                              "type": str((props.get(k) or {}).get("type", "any"))}
                             for k in required],
            })
        return out

    # ------------------------------------------------------------ 派发
    def call(self, server: str, tool: str, arguments: Optional[dict] = None) -> Dict[str, Any]:
        """通用派发（块内立即可用；未激活则先激活一次）。"""
        name = str(server or "").strip()
        tool = str(tool or "").strip()
        if not tool:
            return {"ok": False, "error": "tool 不能为空"}
        if name not in self.armed:
            act = self.activate(name)
            if not act.get("ok"):
                return act
        srv = self.servers.get(name)
        if srv is None:
            return {"ok": False, "state": mcp_state.UNKNOWN, "error": f"server 未构造: {name}"}
        args = arguments if isinstance(arguments, dict) else {}
        cfg = self.cfgs.get(name) or {}
        timeout_s = max(1.0, float(cfg.get("timeout_ms", 20000) or 20000) / 1000.0)
        try:
            from omni_core.async_bridge import run_async

            res = run_async(srv.call_tool(tool, args), timeout=min(timeout_s, 30.0))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "state": mcp_state.DEGRADED, "reason": type(e).__name__,
                    "error": f"MCP 工具调用失败: {str(e)[:240]}",
                    "hint": "连接可能已断；mcp_catalog(refresh=true) 可重探"}
        data = mcp_state.result_data(res)
        if getattr(res, "isError", False):
            text = str(data) if data is not None else "工具返回错误"
            # 把"未登录"这类业务失败回写成观测（目录/下次 activate 快判），
            # 但不改变本次返回语义——模型仍拿到原始失败原因。
            try:
                mcp_state.record_call_failure(name, cfg, text)
            except Exception:
                pass
            return {"ok": False, "state": mcp_state.READY, "reason": "tool_error",
                    "error": text[:300]}
        return {"ok": True, "content": _apply_transform(cfg, tool, data)}

    # ------------------------------------------------------------ 目录 / 收尾
    def catalog(self, refresh: bool = False) -> Dict[str, Any]:
        """列目录：配置 + 缓存观测；``refresh`` 只重探本 run 已激活的 server。"""
        cfg_all = {"servers": list(self.cfgs.values())}
        if refresh:
            for name in list(self.armed):
                srv = self.servers.get(name)
                if srv is None:
                    continue
                try:
                    mcp_state.probe(name, self.cfgs[name], srv, force=True)
                except Exception:
                    pass
        entries = mcp_state.catalog_entries(cfg_all)
        for e in entries:
            e["activated"] = e["name"] in self.armed
        return {"ok": True, "servers": entries}

    def close(self) -> None:
        """子任务结束：断开已激活的 lazy 连接（``lifetime: run`` 语义）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            targets = [self.servers[n] for n in self.armed if n in self.servers]
            self.armed.clear()
            for slot in self.slots.values():
                slot.allowed = None
        for srv in targets:
            cfg = self.cfgs.get(str(getattr(srv, "name", "") or "")) or {}
            if str(cfg.get("lifetime") or "run").strip().lower() != "run":
                continue
            try:
                from omni_core.async_bridge import run_async

                run_async(srv.cleanup(), timeout=5.0)
            except Exception:
                pass


def _apply_transform(cfg: dict, tool: str, data: Any) -> Any:
    """结果归一钩子：``transform.<tool> = "函数名"`` → ``plugins/<server>/normalize.py``。

    归一逻辑留在插件里（如插件自己的 ``normalize.py``），MCP 层只负责"按配置调一下"：
    这样既不用把归一代码复制进内核，也不要求所有 server 都归一。
    """
    key = ((cfg or {}).get("transform") or {}).get(tool)
    if not key:
        return data
    name = str(cfg.get("name") or "").replace("-", "_")
    try:
        mod = importlib.import_module(f"plugins.{name}.normalize")
        fn = getattr(mod, str(key), None)
        return fn(data) if callable(fn) else data
    except Exception:
        return data


def replaced_units(lazy_cfgs: Optional[List[dict]]) -> set:
    """被 lazy server 接管了工具集的 unit 名集合（工具双轨收口的唯一判定入口）。

    只有显式声明 ``replace_plugin_tools`` 的 server 才接管；缺省空集 = 现有行为
    零变化。集中在此，是为了工具来源改用 manifest 声明时只换本函数，装配侧不动。
    """
    return {
        str(cfg.get("name") or "")
        for cfg in (lazy_cfgs or [])
        if isinstance(cfg, dict) and cfg.get("replace_plugin_tools")
    }


def build_lazy_bundle(servers_cfg: Optional[List[dict]], log=None) -> tuple:
    """构造 lazy 激活包 ``(cfgs, servers, slots)``；没有 lazy server 时返回三个 None。

    时序上这是 **ToolLoop 构造期**（每个 run 一次，不是每个子任务）：server 对象只构造、
    不连接（成本 ~0ms），三者在同一 run 的子任务间共享——避免每个子任务重复建连接。

    ``slots`` 与 server 的动态 filter 绑定（闭包读同一个 dict），所以 ``McpSession.activate``
    写槽后，SDK 下一次 ``list_tools`` 立即按子集过滤，不需要重连、不需要重建 Agent。
    """
    lazy: List[dict] = []
    for cfg in (servers_cfg or []):
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        if not (cfg.get("command") or cfg.get("url")):
            continue
        if mode_of(cfg) != "lazy":
            continue
        lazy.append(cfg)
    if not lazy:
        return (None, None, None)

    slots = {server_name(c): ActivationSlot() for c in lazy}

    def _activation(name: str) -> Optional[set]:
        slot = slots.get(str(name))
        return slot.allowed if slot is not None else None

    servers = build_mcp_servers(lazy, log=log, activation=_activation)
    by_name = {str(getattr(s, "name", "") or ""): s for s in servers}
    return (lazy, by_name, slots)


def build_mcp_tools(session: McpSession) -> List[Any]:
    """构造三个元工具（per-run；工具集里只会出现这 3 个，不会出现 server 的 schema）。"""

    @_meta_tool(
        "mcp_catalog",
        "列出可用的外部 MCP 服务及其状态（未启动/未登录/可用），不连接。"
        "需要判断某个服务能不能用时，再用 mcp_activate 连接；目录状态是上次观测，"
        "不代表本次可用。",
    )
    def mcp_catalog(refresh: bool = False) -> Dict[str, Any]:
        """列出 MCP 服务目录（配置 + 上次探活观测）。

        Args:
            refresh: 是否重探本 run 已激活的 server（默认 False，只读缓存）
        """
        try:
            return session.catalog(refresh=bool(refresh))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @_meta_tool(
        "mcp_activate",
        "连接并按需加载一个外部 MCP 服务：探活（服务是否启动、是否登录）后把它的工具"
        "挂到本次会话，下一轮即可直接调用。未就绪时返回 state/reason/hint，不加载工具。",
    )
    def mcp_activate(server: str, tools: Optional[List[str]] = None) -> Dict[str, Any]:
        """连接 + 探活 + 激活一个 MCP 服务。

        Args:
            server: 服务名（取 mcp_catalog 返回的 name）
            tools: 可选，只加载这些工具（缺省 = 该服务配置的白名单全量）
        """
        try:
            return session.activate(server, tools=tools)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @_meta_tool(
        "mcp_call",
        "直接调用某个 MCP 服务的工具（无需先 activate；未激活会自动激活一次）。"
        "适合在激活的同一轮里立即调用，或工具未挂载时的兜底派发。",
    )
    def mcp_call(server: str, tool: str, arguments: Optional[dict] = None) -> Dict[str, Any]:
        """通用派发：调 MCP 工具（未激活则先激活）。

        Args:
            server: 服务名
            tool: 该服务暴露的工具名
            arguments: 参数对象（与工具 schema 对应）
        """
        try:
            return session.call(server, tool, arguments)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return [mcp_catalog, mcp_activate, mcp_call]
