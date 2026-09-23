"""外部 MCP 接入（**SDK 原生**）：config -> `agents.mcp.MCPServer` 列表。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.0 / §7-M3）：
- MCP 专用于**外部工具接入**（开源 MCP server / 第三方系统）。
- 连接、工具发现、schema 转换、派发全部交给 SDK：把 MCPServer 交给 Agent；
  **注意：本版 agents SDK 的 Runner 不会自动 `connect()`（见 agents/agent.py
  注释），须由 sdk_loop 在运行期先 `connect()` 再交给 Agent**。连接后的工具发现
  与派发由 SDK 接管，把外部工具与自研工具**平级**暴露给 LLM 并按名派发。
- 纯配置增量：新增一个外部能力 = 在 MCP 配置加一段 server，零代码改动。
- MCP 配置已抽离到 **``~/.omniagent/mcp.json``**（不再进 ``config.yaml``）；
  由 ``config.load_mcp_config()`` 读取，``config.save_mcp_config()`` 写回。

配置示例（~/.omniagent/mcp.json）::

    enabled: true
    servers:
      - name: filesystem
        enabled: true
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
        # T4.1：工具按需注入（默认全量放行）。glob 匹配（标准库 fnmatch），
        # 既匹配 MCP 原始工具名，也匹配 SDK 暴露名 `mcp_<server>__<tool>`。
        # tool_filter:
        #   include: ["read_*", "list_*"]     # 白名单（空 = 不限制）
        #   exclude: ["write_*", "delete_*"]  # 黑名单（优先级高于 include）
        # T4.2：连接 / 探活 / 工具调用超时（毫秒，默认 60000）。
        # 透传 SDK 原生 client_session_timeout_seconds，并在连接包装层同步生效。
        timeout_ms: 60000
      - name: remote
        enabled: false
        url: http://127.0.0.1:8010/mcp

工具命名（T4.1 探测结论，留存备查）：
    本版 agents SDK 原生即为 MCP 工具加前缀：``mcp_<server>__<tool>``
    （见 agents/mcp/util.py ``_build_prefixed_tool_base_name``），同名冲突时
    SDK 还会自动加短哈希兜底。因此**不存在命名冲突**，本项目保留原生名称，
    不再另造 ``mcp__<server>__<raw>`` 适配层；过滤配置的 glob 同时兼容
    原始名与带前缀的暴露名，用户两种写法都能命中。
"""
import fnmatch
from typing import Any, Callable, List, Optional

from agents.mcp import MCPServer, MCPServerStdio, MCPServerStreamableHttp


DEFAULT_TIMEOUT_MS = 60000


def _timeout_seconds(cfg: dict, log: Optional[Callable[[str], None]] = None) -> float:
    """T4.2：解析 ``timeout_ms``（默认 60000ms），返回秒；非法值回退默认。"""
    raw = cfg.get("timeout_ms")
    if raw is None:
        return DEFAULT_TIMEOUT_MS / 1000.0
    try:
        ms = float(raw)
    except (TypeError, ValueError):
        if log:
            log("MCP timeout_ms 非法（应为数字），已回退默认 60000")
        return DEFAULT_TIMEOUT_MS / 1000.0
    if ms <= 0:
        return DEFAULT_TIMEOUT_MS / 1000.0
    return ms / 1000.0


def _tool_filter_patterns(cfg: dict, log: Optional[Callable[[str], None]] = None):
    """从服务配置解析 ``tool_filter.include/exclude`` glob 列表。

    非法配置（非 dict / 非字符串列表）一律静默跳过（不报错、不过滤）。
    返回 (include, exclude)；两者都空表示无需过滤。
    """
    tf = cfg.get("tool_filter")
    if not isinstance(tf, dict):
        return [], []

    def _patterns(key):
        raw = tf.get(key)
        if not isinstance(raw, (list, tuple)):
            if raw is not None and log:
                log(f"MCP tool_filter.{key} 非法（应为字符串列表），已忽略")
            return []
        return [str(p) for p in raw if isinstance(p, str)]

    return _patterns("include"), _patterns("exclude")


def _build_tool_filter(include: List[str], exclude: List[str], server_name: str):
    """构造 SDK 原生动态工具过滤器（ToolFilterCallable）。

    用动态过滤器而非静态 allowlist：配置是 **glob**，需对实际工具名做通配匹配，
    而静态过滤器只支持精确名列表。
    """
    def _matches(name: str, patterns: List[str]) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in patterns)

    def _filter(context: Any, tool: Any) -> bool:
        raw = str(getattr(tool, "name", "") or "")
        srv = str(getattr(context, "server_name", "") or server_name)
        prefixed = f"mcp_{srv}__{raw}"
        if include and not (_matches(raw, include) or _matches(prefixed, include)):
            return False
        if exclude and (_matches(raw, exclude) or _matches(prefixed, exclude)):
            return False
        return True

    return _filter


def build_mcp_servers(
    servers_cfg: Optional[List[dict]],
    log: Optional[Callable[[str], None]] = None,
) -> List[MCPServer]:
    """按配置构造 SDK MCPServer（不在此处连接；连接生命周期由 sdk_loop 在运行期负责）。

    Args:
        servers_cfg: ``runtime.mcp.servers`` 列表。
        log: 可选日志回调（如 ``ToolLoop._log``）；None 则静默。

    Returns:
        SDK MCPServer 列表，可直接传给 ``Agent(mcp_servers=[...])``。
    """
    servers: List[MCPServer] = []
    for cfg in servers_cfg or []:
        if not isinstance(cfg, dict) or not cfg.get("enabled", True):
            continue
        # 既无 command 也无 url = 非法/占位配置，静默跳过
        if not (cfg.get("command") or cfg.get("url")):
            continue

        name = str(cfg.get("name") or cfg.get("command") or cfg.get("url") or "mcp")
        # T4.1：按需注入——默认全量放行（无 include/exclude 时不过滤）
        _inc, _exc = _tool_filter_patterns(cfg, log)
        _filter = _build_tool_filter(_inc, _exc, name) if (_inc or _exc) else None
        # T4.2：超时透传（SDK 原生 client_session_timeout_seconds，单位秒）
        _timeout_s = _timeout_seconds(cfg, log)
        try:
            if cfg.get("url"):
                params: dict = {"url": cfg["url"]}
                if cfg.get("headers"):
                    params["headers"] = cfg["headers"]
                servers.append(MCPServerStreamableHttp(
                    params=params, name=name, tool_filter=_filter,
                    client_session_timeout_seconds=_timeout_s))
            else:
                stdio_params: dict = {
                    "command": cfg["command"],
                    "args": list(cfg.get("args") or []),
                }
                if cfg.get("env"):
                    stdio_params["env"] = cfg["env"]
                if cfg.get("cwd"):
                    stdio_params["cwd"] = cfg["cwd"]
                servers.append(MCPServerStdio(
                    params=stdio_params, name=name, tool_filter=_filter,
                    client_session_timeout_seconds=_timeout_s))
        except Exception as e:  # 单个 server 配错不得拖垮整体
            if log:
                log(f"MCP server {name} 配置无效: {type(e).__name__}: {e}")
    return servers
