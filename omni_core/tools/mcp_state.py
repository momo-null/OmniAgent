"""MCP 运行态：三级探活 + 磁盘状态缓存（lazy activation 的地基）。

设计见 ``doc/plans/implemented/mcp-lazy-activation-2026-11-13.md`` §6.2。三条不变量：

1. **可用性不是布尔，是四态 + 原因**：``offline``（端口不通）/ ``degraded``（端口通但协议
   不应答）/ ``unready``（活着但没登录）/ ``ready``。每态都带 ``hint``（恢复动作），
   模型可以把 hint 原样转述给用户——这比抛异常有用。
2. **状态按 server 名落 ``~/.omniagent/mcp_state.json``**：跨 run / 跨进程可读
   （目录注入、设置页、网关 ``ready()`` 共用一份）。**不塞进 SDK 的 MCPServer 对象**：
   那对象是 per-run 池化实例，跨进程不可见。
3. **这里只记观测，不做放弃**：``sdk_loop._MCP_ABANDONED`` 是"进程级放弃"，对 lazy
   server 不适用——一次没登录不能导致整个会话都不再试。重试节奏由调用方按 TTL 决定。

探活预算纪律（工具调用内，用户可感知）：

========  ========  =========================================
级别      超时      失败语义
========  ========  =========================================
端口      0.3s      offline（服务没起）→ hint 指向启动脚本
协议      1.5s      degraded（半死 / 协议不兼容）
就绪      2.0s      unready（活着但未登录）
========  ========  =========================================

三级累计不超过 ``DEFAULT_BUDGET_S``（2.0s）：超过即返回已有结论，绝不让工具调用卡 8s
（``sdk_loop._MCP_CONNECT_BUDGET_SEC`` 是"首次大脑调用前"的预算，语义不同，不能复用）。
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# 状态取值（顺序即严重程度，用于取更差的结论）
UNKNOWN = "unknown"
OFFLINE = "offline"
DEGRADED = "degraded"
UNREADY = "unready"
READY = "ready"

# 单次激活的总预算（秒）。端口/协议/就绪三级共享这一个 deadline。
DEFAULT_BUDGET_S = 2.0
PORT_TIMEOUT_S = 0.3
DEFAULT_TTL_S = 600

_STATE_LOCK = threading.RLock()
_CACHE_FALLBACK: Dict[str, "ServerState"] = {}   # 磁盘不可用时的进程内兜底


@dataclass
class ServerState:
    """一个 server 的观测结果（可序列化，落盘后跨进程读）。"""

    name: str
    state: str = UNKNOWN
    ready: bool = False
    reason: str = ""
    hint: str = ""
    tools: List[str] = field(default_factory=list)
    ts: float = 0.0
    latency_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def fresh_sec(self) -> float:
        """距上次探活的秒数（目录里给模型看"这个状态有多旧"）。"""
        return max(0.0, time.time() - self.ts) if self.ts else -1.0


# ---------------------------------------------------------------- 路径与读写

def state_path() -> Path:
    """状态文件路径。

    优先级：``OMNI_MCP_STATE_PATH``（测试）> 与 ``config.MCP_CONFIG_PATH`` 同目录 >
    ``~/.omniagent``。跟随 config 是为了让测试的路径重定向自动生效——否则单测会写进
    真实用户目录（conftest 对 config 写接口有守卫，对自建文件没有）。
    """
    env = os.environ.get("OMNI_MCP_STATE_PATH")
    if env:
        return Path(env)
    try:
        import config as _config

        base = os.path.dirname(str(_config.MCP_CONFIG_PATH))
        if base:
            return Path(base) / "mcp_state.json"
    except Exception:
        pass
    return Path.home() / ".omniagent" / "mcp_state.json"


def _read_all() -> Dict[str, ServerState]:
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # 缺文件 / 坏 JSON / 半写（上次崩溃残留）一律当空——与各 load_state 同款宽容
        raw = None
    out: Dict[str, ServerState] = {}
    if isinstance(raw, dict):
        for name, item in raw.items():
            if not isinstance(item, dict) or not isinstance(name, str):
                continue
            try:
                out[name] = ServerState(
                    name=name,
                    state=str(item.get("state") or UNKNOWN),
                    ready=bool(item.get("ready")),
                    reason=str(item.get("reason") or ""),
                    hint=str(item.get("hint") or ""),
                    tools=[str(t) for t in (item.get("tools") or []) if str(t)],
                    ts=float(item.get("ts") or 0.0),
                    latency_ms=int(item.get("latency_ms") or 0),
                )
            except Exception:
                continue
    return out


def _write_all(states: Dict[str, ServerState]) -> None:
    """原子写：临时文件 + ``os.replace``（半写不可见）。失败只退进程内。"""
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({k: v.to_dict() for k, v in states.items()},
                             ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".mcp_state.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    except Exception:
        pass


def load_state(name: str) -> Optional[ServerState]:
    """读单个 server 的缓存观测（不探活）。"""
    with _STATE_LOCK:
        st = _read_all().get(name)
        return st or _CACHE_FALLBACK.get(name)


def load_all() -> Dict[str, ServerState]:
    """读全部缓存观测（目录渲染 / 设置页 / 网关 ready 共用入口）。"""
    with _STATE_LOCK:
        merged = dict(_CACHE_FALLBACK)
        merged.update(_read_all())
        return merged


def save_state(state: ServerState) -> ServerState:
    """写一个 server 的观测（activate / probe 的出口）。"""
    state.ts = state.ts or time.time()
    with _STATE_LOCK:
        _CACHE_FALLBACK[state.name] = state
        all_states = _read_all()
        all_states[state.name] = state
        _write_all(all_states)
    return state


def clear_state(name: str = "") -> None:
    """清缓存（测试 / 手工排障用）。``name=""`` 清全部。"""
    with _STATE_LOCK:
        if not name:
            _CACHE_FALLBACK.clear()
            _write_all({})
            return
        _CACHE_FALLBACK.pop(name, None)
        all_states = _read_all()
        all_states.pop(name, None)
        _write_all(all_states)


# ---------------------------------------------------------------- 端点与端口

def endpoint(cfg: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """从配置解析 ``(host, port)``；stdio 型（无 url）返回 None。"""
    url = str((cfg or {}).get("url") or "").strip()
    port_raw = (cfg or {}).get("port")
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "127.0.0.1"
        port = int(port_raw) if port_raw not in (None, "") else int(parsed.port or 0)
        if port <= 0:
            return None
        return host, port
    except Exception:
        return None


def port_open(host: str, port: int, timeout_s: float = PORT_TIMEOUT_S) -> bool:
    """端口探活：只证明"有东西在 listen"，不证明协议对。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


#: 拉起本机服务后给协议层留的独立预算（冷启动早就把 L0 的 2s 耗光了）
SPAWN_CONNECT_GRACE_S = 5.0
_SPAWN_LOCK = threading.Lock()


def ensure_local_service(name: str, cfg: Dict[str, Any],
                         ep: Tuple[str, int]) -> Tuple[bool, str]:
    """端口不通时按 ``spawn`` 配置拉起**本机 HTTP 服务**，并等端口就绪。

    为什么需要：命中 ``url`` 的 MCP 服务（如某本地采集服务的 ``<svc>.exe``，HTTP @<port>）
    是**独立进程**，不是 SDK 能接管 stdio 的那类 server。没人起它，模型就只能一直看到
    ``offline``。``spawn`` 把"需要时才连"延伸到"需要时才起"，且不牺牲开机后的零常驻。

    契约（``cfg["spawn"]``）::

        {"command": "…/<svc>.exe",   # 必填，支持 %VAR% 与 ~
         "args": [], "cwd": "…",                # 可选，cwd 缺省 = exe 所在目录
         "ready_timeout_s": 20,                 # 等端口上限（首启可能要下 Chromium）
         "env": {"K": "V"}, "hint": "…"}        # 可选

    三条纪律：
    - **幂等**：进锁后先复查端口，通了就当 ``already_up``，绝不重复拉第二份；
    - **不阻塞他人**：等端口在锁外做，否则一个 run 的冷启动会卡住别的 run；
    - **只起不杀**：这里不做 stop/restart——误杀一个共享服务，比拉不起来更糟。
      要"用完就停"请走独立守护（见文档 §12.2 的可选项）。
    """
    sp = (cfg or {}).get("spawn")
    if not isinstance(sp, dict):
        return False, "no_spawn_config"
    cmd = str(sp.get("command") or "").strip()
    if not cmd:
        return False, "no_spawn_command"
    exe = os.path.expandvars(os.path.expanduser(cmd))
    if not os.path.exists(exe):
        return False, "spawn_command_missing"

    import subprocess

    with _SPAWN_LOCK:
        # 双检：另一个 run 可能已经拉起来了（或用户自己手动起了）
        if port_open(ep[0], ep[1]):
            return True, "already_up"
        argv = [exe] + [str(a) for a in (sp.get("args") or [])]
        cwd = os.path.expandvars(os.path.expanduser(str(sp.get("cwd") or "")))
        if not cwd:
            cwd = os.path.dirname(exe) or ""
        env = dict(os.environ)
        extra = sp.get("env")
        if isinstance(extra, dict):
            env.update({str(k): str(v) for k, v in extra.items()})
        flags = 0
        if os.name == "nt":
            # 不弹黑窗、独立进程组：后端重启不影响它
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            subprocess.Popen(
                argv, cwd=cwd or None, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
            )
        except Exception as e:  # noqa: BLE001 —— 起不来只能降级成 offline
            return False, f"spawn_failed:{type(e).__name__}"

    # 锁外等端口：冷启动可能十几秒（部分服务首启还要下载运行时依赖）
    deadline = time.monotonic() + max(0.5, float(sp.get("ready_timeout_s") or 20.0))
    while time.monotonic() < deadline:
        if port_open(ep[0], ep[1], timeout_s=0.2):
            return True, "spawned"
        time.sleep(0.2)
    return False, "spawn_timeout"


# ---------------------------------------------------------------- 结果解析

def _text_of(result: Any) -> str:
    """``CallToolResult`` → 纯文本（拼接所有 text 内容块）。"""
    try:
        parts: List[str] = []
        for block in (getattr(result, "content", None) or []):
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        if parts:
            return "\n".join(parts).strip()
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return json.dumps(structured, ensure_ascii=False)
    except Exception:
        pass
    return ""


def result_data(result: Any) -> Any:
    """工具结果归一：优先 JSON，其次纯文本，最后原样 repr。"""
    text = _text_of(result)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def tool_names(tools: Any) -> List[str]:
    """``list[MCPTool]`` → 名字列表（过滤后仍按原序）。"""
    out: List[str] = []
    for t in (tools or []):
        name = str(getattr(t, "name", "") or "")
        if name:
            out.append(name)
    return out


#: 探活/枚举用的占位 agent。SDK 的动态 filter 只检查 ``agent is not None``
#: （``agents/mcp/server.py:1019``），不读它的字段——我们的过滤器也只读闭包里的 server 名。
_PLACEHOLDER_AGENT = SimpleNamespace(name="omni-mcp-probe")


def list_tools(server: Any, timeout_s: float = 3.0) -> List[Any]:
    """``server.list_tools()`` 的统一安全入口。

    **不能裸调** ``server.list_tools()``：只要该 server 绑定了 callable 过滤器（我们的
    include/子集过滤都是 callable），SDK 在缺 ``run_context``/``agent`` 时直接抛
    ``UserError("run_context and agent are required for dynamic tool filtering")``。
    Runner 里调用时它自带上下文，所以平时不会暴露；探活/枚举这类"上下文外调用"必须补。
    """
    try:
        from agents.run_context import RunContextWrapper

        ctx: Any = RunContextWrapper(context=None)
    except Exception:
        ctx = SimpleNamespace(context=None)
    from omni_core.async_bridge import run_async

    return run_async(server.list_tools(ctx, _PLACEHOLDER_AGENT), timeout=timeout_s) or []


# ---------------------------------------------------------------- 工具调用（上下文外）

_RAW_SERVERS: Dict[str, Any] = {}
_CALL_LOCKS: Dict[str, threading.Lock] = {}
_RAW_LOCK = threading.Lock()


def _cache_key(cfg: Dict[str, Any]) -> str:
    return "%s|%s" % (cfg.get("name") or "", cfg.get("url") or cfg.get("command") or "")


def raw_server(cfg: Dict[str, Any]) -> Any:
    """取一个**不带 tool_filter** 的 SDK server（进程内缓存，按 ``name|url`` 复用）。

    元工具与 SDK 运行期的 filter 是"给模型看的只读白名单"；写路径与诊断型调用
    （通道发布/评论、登录探针）不在白名单里，走这里绕过过滤。
    同一 key 复用同一个对象——SDK server 内部持有 session，不能并发复用，
    串行化由 :func:`call_tool` 负责。
    """
    key = _cache_key(cfg)
    with _RAW_LOCK:
        srv = _RAW_SERVERS.get(key)
        if srv is None:
            from omni_core.tools.mcp_servers import build_mcp_servers

            clone = dict(cfg)
            clone["tool_filter"] = None
            built = build_mcp_servers([clone], mode_filter="all")
            if not built:
                raise RuntimeError("MCP server 配置无效: %r" % (cfg.get("name") or key,))
            srv = built[0]
            _RAW_SERVERS[key] = srv
            _CALL_LOCKS[key] = threading.Lock()
        return srv


def call_tool(cfg: Dict[str, Any], tool: str,
              arguments: Optional[Dict[str, Any]] = None,
              timeout_s: float = 15.0) -> Dict[str, Any]:
    """上下文外的工具调用统一入口：``connect → call_tool → cleanup`` 一次跑完。

    返回 ``{"ok": True, "data": <JSON/文本>}`` 或 ``{"ok": False, "error": "..."}``。
    通道适配器（写路径/登录探针）与任何非 run 内的调用都走这里，**不再各自手写
    JSON-RPC**——MCP 客户端全进程只有这一份实现。
    """
    try:
        server = raw_server(cfg)
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    async def _once() -> Any:
        await server.connect()
        try:
            return await server.call_tool(tool, dict(arguments or {}))
        finally:
            try:
                await server.cleanup()
            except Exception:
                pass

    from omni_core.async_bridge import run_async

    lock = _CALL_LOCKS.get(_cache_key(cfg))
    try:
        if lock is not None:
            with lock:
                result = run_async(_once(), timeout=timeout_s)
        else:
            result = run_async(_once(), timeout=timeout_s)
    except Exception as e:
        return {"ok": False,
                "error": "MCP 调用失败（%s）: %s: %s" % (tool, type(e).__name__, e)}

    is_err = bool(getattr(result, "isError", None) or getattr(result, "is_error", None))
    if is_err:
        return {"ok": False, "error": _text_of(result) or ("%s 执行失败" % tool)}
    return {"ok": True, "data": result_data(result)}


def _dig(data: Any, path: str) -> Any:
    """按 ``a.b.c`` 取嵌套字段；任一层缺失返回 None。"""
    cur = data
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


#: 通用技术状态词：dict 里 status/state 字段的就绪取值（不含任何业务/平台文案）
READY_WORDS = ("ok", "ready", "online", "healthy", "available", "up", "true")


def _readiness_ok(data: Any, probe_cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """按配置判定探针结果——**只做机械匹配，不内置任何业务文案**。

    判定顺序（按优先级）：
    1. ``extract`` + ``expect``：按字段路径取值比较（最严格，推荐）；
    2. ``data`` 为 bool 直接用；为 dict 时按通用键（ok/success/valid/ready/status/state）；
    3. 文本：``fail_words`` 命中即不就绪（否定优先），``ok_words`` 命中即就绪。
       两张词表都由调用方通过配置提供；都没配时返回 ``probe_unknown``——本模块不知道
       某个服务的"就绪"在业务上怎么说，不做猜测。
    """
    extract = str(probe_cfg.get("extract") or "").strip()
    expect = probe_cfg.get("expect", True)
    if extract:
        got = _dig(data, extract)
        if got is not None:
            if isinstance(expect, bool):
                ok = bool(got) == expect
            else:
                ok = got == expect or str(got).strip().lower() == str(expect).strip().lower()
            return (True, "") if ok else (False, "probe_mismatch")
        # 字段缺失不立刻判死：有的实现返回纯文本，继续走文本口径

    if isinstance(data, bool):
        return (True, "") if data == bool(expect) else (False, "probe_mismatch")
    if isinstance(data, dict):
        for key in ("ok", "success", "valid", "ready", "status", "state"):
            if key not in data:
                continue
            got = data.get(key)
            if isinstance(got, bool):
                return (True, "") if got == bool(expect) else (False, "probe_mismatch")
            return ((True, "") if str(got).strip().lower() in READY_WORDS
                    else (False, "probe_unknown"))

    low = str(data).lower() if data is not None else ""
    fail_words = [str(w).lower() for w in (probe_cfg.get("fail_words") or []) if str(w)]
    ok_words = [str(w).lower() for w in (probe_cfg.get("ok_words") or []) if str(w)]
    if fail_words and any(w in low for w in fail_words):
        return False, "probe_mismatch"
    if ok_words and any(w in low for w in ok_words):
        return True, ""
    return False, "probe_unknown"


def readiness_ok(data: Any, probe_cfg: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """公开的机械判定入口：按 ``probe_cfg`` 判"是否就绪"。

    词表（``ok_words`` / ``fail_words``）由调用方提供——server 配置或通道自己——
    本模块不内置任何服务/平台的文案。
    """
    return _readiness_ok(data, dict(probe_cfg or {}))


def record_call_failure(name: str, cfg: Dict[str, Any], text: Any) -> Optional[ServerState]:
    """真实调用失败时回写观测态（**只记观测，不改调用结果**）。

    是否记 ``unready`` 完全由配置的 ``readiness.fail_words`` 决定——本模块不猜业务
    文案；没配词表就什么都不记，失败照原样返回给调用方。
    """
    rcfg = (cfg or {}).get("readiness") or {}
    fail_words = [str(w) for w in (rcfg.get("fail_words") or []) if str(w)]
    if not fail_words:
        return None
    low = str(text or "").lower()
    if not any(w.lower() in low for w in fail_words):
        return None
    hint = str(rcfg.get("hint") or "")
    prev = load_state(name)
    st = ServerState(
        name=name, state=UNREADY, ready=False, reason="probe_mismatch",
        hint=hint or "服务报告未就绪", ts=time.time(),
        tools=list(prev.tools) if prev else [],
    )
    return save_state(st)


# ---------------------------------------------------------------- 探活主流程

def probe(
    name: str,
    cfg: Dict[str, Any],
    server: Any,
    *,
    force: bool = False,
    budget_s: float = DEFAULT_BUDGET_S,
    with_readiness: bool = True,
) -> ServerState:
    """三级探活并写缓存。**不抛异常**：任何一级失败都转成带 hint 的状态。

    Args:
        name: server 名（缓存键）。
        cfg: mcp.json 的单 server 段（url/port/readiness/timeout_ms）。
        server: SDK ``MCPServer`` 对象（未连接也可；连接复用 ``session``）。
        force: True 跳过 TTL 缓存（激活时永远 true——"这一秒到底行不行"当场裁定）。
        budget_s: 总预算（端口 + 协议 + 就绪共享）。
        with_readiness: 是否执行 L2 就绪探针。**默认 True 只对"探针便宜"的 server 合适**；
            有的探针要驱动外部进程，单次可达十余秒，放进激活同步路径会把工具调用卡死
            ——那类 server 应显式关掉，把就绪态交给"真实调用结果回写"
            （``record_call_failure``）。

    Returns:
        ``ServerState``；``ready=True`` 才代表可注入 agent。注意 ``ready`` 只表示
        "端口 + 协议可用"（未开就绪探针时），**不**代表已登录。
    """
    ttl = float((cfg or {}).get("readiness", {}).get("ttl_sec", DEFAULT_TTL_S) or DEFAULT_TTL_S)
    cached = load_state(name)
    if cached and not force and cached.ts and (time.time() - cached.ts) < ttl:
        return cached

    started = time.monotonic()
    deadline = started + max(0.1, float(budget_s))
    # hint 按**状态**选，不能混用：服务没起时提示"扫码"毫无意义（readiness.hint 只描述
    # "活着但没登录"这一种失败）。三类 hint 各自独立，配置可覆盖。
    _readiness = (cfg or {}).get("readiness")
    ready_hint = str(_readiness.get("hint") or "") if isinstance(_readiness, dict) else ""
    offline_hint = (str((cfg or {}).get("offline_hint") or "")
                    or "MCP 服务未启动（先运行对应的启动脚本）")
    degraded_hint = (str((cfg or {}).get("degraded_hint") or "")
                     or "MCP 服务已监听但协议不应答（可能刚启动或已卡死）")

    def _build(state: str, reason: str = "", hint: str = "",
               tools: Optional[List[str]] = None) -> ServerState:
        st = ServerState(
            name=name, state=state, ready=(state == READY), reason=reason,
            hint=hint, tools=list(tools or (cached.tools if cached else [])),
            ts=time.time(), latency_ms=int((time.monotonic() - started) * 1000),
        )
        return save_state(st)

    # L0：端口（仅 url 型；stdio 无端口，直接进协议级）
    ep = endpoint(cfg)
    if ep is not None and not port_open(ep[0], ep[1]):
        spawn_cfg = (cfg or {}).get("spawn")
        if isinstance(spawn_cfg, dict) and str(spawn_cfg.get("command") or "").strip():
            ok, why = ensure_local_service(name, cfg, ep)
            if not ok:
                return _build(OFFLINE, why,
                              str(spawn_cfg.get("hint") or "") or offline_hint)
            # 冷启动把 L0 的 2s 预算早耗光了：给协议层一段独立预算
            deadline = max(deadline, time.monotonic() + SPAWN_CONNECT_GRACE_S)
        else:
            return _build(OFFLINE, "port_closed", offline_hint)

    # L1：连接 + list_tools（复用已连接 session；失败退避重试一次）
    timeouts_s = max(0.3, float((cfg or {}).get("timeout_ms", 60000) or 60000) / 1000.0)
    last_err: Optional[BaseException] = None
    tools: List[str] = []
    for attempt in (1, 2):
        left = deadline - time.monotonic()
        if left <= 0:
            last_err = last_err or TimeoutError("probe budget exhausted")
            break
        per_timeout = max(0.1, min(timeouts_s, left))
        try:
            from omni_core.async_bridge import run_async

            if getattr(server, "session", None) is None:
                run_async(server.connect(), timeout=per_timeout)
            # 注意：不能裸调 list_tools()——动态 filter 需要 run_context/agent（见下方封装）
            listed = list_tools(server, timeout_s=max(0.1, deadline - time.monotonic()))
            tools = tool_names(listed)
            last_err = None
            break
        except Exception as e:  # noqa: BLE001 —— 任何异常都只是"这一级没过"
            last_err = e
            if attempt == 1 and (deadline - time.monotonic()) > 0.4:
                time.sleep(0.3)
    if last_err is not None:
        return _build(DEGRADED, f"{type(last_err).__name__}", degraded_hint, tools)

    # L2：就绪探针（登录态等业务前置，仅配置了才做）
    readiness = (cfg or {}).get("readiness")
    if with_readiness and isinstance(readiness, dict) and readiness.get("probe"):
        probe_tool = str(readiness.get("probe") or "")
        left = deadline - time.monotonic()
        if left <= 0.1:
            return _build(DEGRADED, "probe_budget_exhausted", degraded_hint, tools)
        try:
            from omni_core.async_bridge import run_async

            res = run_async(server.call_tool(probe_tool, {}), timeout=min(2.5, left))
            if getattr(res, "isError", False):
                return _build(UNREADY, "probe_error",
                              ready_hint or f"就绪探针 {probe_tool} 返回错误", tools)
            ok, reason = _readiness_ok(result_data(res), readiness)
            if not ok:
                return _build(UNREADY, reason, ready_hint or "服务可用但未就绪",
                              tools)
        except Exception as e:  # noqa: BLE001 —— 探针**抛异常**是"半死"，不是"不就绪"
            return _build(DEGRADED, f"{type(e).__name__}", degraded_hint, tools)

    return _build(READY, "", "", tools)


# ---------------------------------------------------------------- 目录渲染

def catalog_entries(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按配置生成目录条目（只读配置 + 缓存观测，**零连接**）。

    排序：lazy 的排前面（它们才需要模型决策），eager 的排后面；同组按配置原序。
    """
    servers = [s for s in ((cfg or {}).get("servers") or []) if isinstance(s, dict)]
    states = load_all()
    lazy_first = [s for s in servers if is_lazy_cfg(s)] + [s for s in servers if not is_lazy_cfg(s)]
    out: List[Dict[str, Any]] = []
    for item in lazy_first:
        if not item.get("enabled", True):
            continue
        if not (item.get("command") or item.get("url")):
            continue
        name = str(item.get("name") or item.get("command") or item.get("url") or "mcp")
        st = states.get(name)
        out.append({
            "name": name,
            "desc": str(item.get("desc") or item.get("description") or "").strip(),
            "lazy": is_lazy_cfg(item),
            "state": st.state if st else UNKNOWN,
            "ready": bool(st.ready) if st else False,
            "reason": st.reason if st else "",
            # 目录里优先给服务自己的恢复提示；没有才用 readiness.hint
            "hint": (st.hint if st and st.hint else
                     str(((item.get("readiness") or {}) or {}).get("hint") or "")),
            "fresh_sec": int(st.fresh_sec) if st and st.ts else -1,
            "tools": list(st.tools) if st else [],
        })
    return out


def format_catalog_message(cfg: Dict[str, Any], *,
                           desc_limit: int = 60, max_items: int = 12) -> str:
    """渲染成 ``<available_mcp_servers>`` 文本（一条 user 消息，紧随技能目录之后）。

    刻意**不承诺可用**：只列"存在什么、干什么、上次探到什么状态"。要让上下文保证
    "能连上"就必须先连一遍——那正是启动即连接的老毛病。可用性由 activate 当场裁定。
    """
    catalog_cfg = (cfg or {}).get("catalog") or {}
    if not isinstance(catalog_cfg, dict) or catalog_cfg.get("inject") is False:
        return ""
    entries = catalog_entries(cfg)
    if not entries:
        return ""
    limit = int(catalog_cfg.get("desc_limit", desc_limit) or desc_limit)
    cap = int(catalog_cfg.get("max_items", max_items) or max_items)
    lines = ["<available_mcp_servers>"]
    for e in entries[:max(1, cap)]:
        desc = e["desc"] or "外部 MCP 服务"
        if len(desc) > limit:
            desc = desc[:limit] + "…"
        if e["lazy"]:
            # 状态文案：ready 也要带新鲜度，避免模型把 10 分钟前的观测当现状
            if e["state"] == READY:
                status = f"上次探活可用（{e['fresh_sec']}s 前）"
            elif e["state"] == UNKNOWN:
                status = "未探活"
            else:
                status = f"{e['state']}{('：' + e['reason']) if e['reason'] else ''}"
                if e.get("hint"):
                    status += f"（{e['hint']}）"
            lines.append(f"- `{e['name']}`: {desc}｜{status}")
        else:
            lines.append(f"- `{e['name']}`: {desc}｜常驻（工具已加载）")
    lines.append("</available_mcp_servers>")
    lines.append(
        "以上为外部 MCP 服务目录（仅摘要，工具尚未加载）。目录里的状态是上一次探活的观测，"
        "**不代表本次可用**；需要时调用 mcp_activate(name) 连接并按需加载工具，以它的返回为准。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 配置判定

def is_lazy_cfg(cfg: Dict[str, Any]) -> bool:
    """是否显式声明了懒加载（``mode: lazy``）。

    缺省 **eager**：升级不得静默改变既有配置的语义（现网条目大多没写 ``mode``）。
    要懒加载必须显式声明——这是与"缺省 lazy"方案的实质偏离，理由见
    ``mcp_servers.mode_of``。
    """
    mode = str((cfg or {}).get("mode") or "eager").strip().lower()
    return mode in ("lazy", "on_demand", "ondemand")


def has_lazy_server(cfg: Dict[str, Any], name: str) -> bool:
    """配置里是否存在同名 lazy server（供插件让位，避免双轨重复注册）。"""
    for item in ((cfg or {}).get("servers") or []):
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("name") or item.get("command") or item.get("url") or "")
        if item_name == name and item.get("enabled", True) and is_lazy_cfg(item):
            return True
    return False
