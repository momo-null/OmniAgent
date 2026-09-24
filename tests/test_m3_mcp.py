"""外部 MCP 接入单测（**SDK 原生**）。

MCP 的连接/发现/派发全部交给 Agents SDK：本层只把 config 翻译成
`agents.mcp.MCPServer`（stdio / streamable-http），交给 Agent 即可。
因此这里只验证「配置 -> MCPServer」这一跳的正确性与容错；
真正的端到端（真起 server 并让 Runner 调它的工具）见 tests/test_m5_sdk_runner.py。
"""
import pytest

from agents.mcp import MCPServerStdio, MCPServerStreamableHttp

from omni_core.tools.mcp_servers import build_mcp_servers


def test_build_stdio_server_from_config():
    servers = build_mcp_servers([
        {"name": "fs", "command": "npx", "args": ["-y", "srv", "."], "env": {"K": "v"}, "cwd": "/tmp"},
    ])
    assert len(servers) == 1
    s = servers[0]
    assert isinstance(s, MCPServerStdio)
    assert s.name == "fs"


def test_build_http_server_from_config():
    servers = build_mcp_servers([
        {"name": "remote", "url": "http://127.0.0.1:8010/mcp", "headers": {"A": "b"}},
    ])
    assert len(servers) == 1
    assert isinstance(servers[0], MCPServerStreamableHttp)


def test_disabled_or_invalid_skipped():
    """disabled / 非 dict / 既无 command 也无 url（占位段）全部跳过，不抛异常。"""
    servers = build_mcp_servers([
        {"name": "off", "enabled": False, "command": "x"},
        "not-a-dict",
        {"name": "placeholder", "command": ""},
        {"name": ""},
    ])
    assert servers == []


def test_no_connection_at_build_time():
    """构建期不连接：连不上的 server 也只返回对象，错误留给 Runner 阶段处理。"""
    servers = build_mcp_servers([{"name": "bad", "command": "definitely-not-a-binary"}])
    assert len(servers) == 1  # 未连接，因此不会失败


def test_empty_config_returns_empty():
    assert build_mcp_servers(None) == []
    assert build_mcp_servers([]) == []


def test_log_callback_used_on_bad_config(monkeypatch):
    """单个 server 配置畸形时记录日志，不影响其它 server。"""
    import omni_core.tools.mcp_servers as m

    def _boom(params, name):  # 模拟 SDK 构造失败
        raise ValueError("bad params")

    monkeypatch.setattr(m, "MCPServerStdio", _boom)
    logs = []
    servers = build_mcp_servers(
        [{"name": "bad", "command": "x"}, {"name": "ok", "url": "http://127.0.0.1:9/mcp"}],
        log=logs.append,
    )
    assert any("bad" in line for line in logs)
    assert len(servers) == 1  # 另一个 http server 正常构造


def test_sdk_mcp_package_available():
    """MCP 走的是 SDK 自带实现，不依赖我们自研桥（mcp_bridge 已删）。"""
    import importlib
    import pathlib

    assert not (pathlib.Path(__file__).resolve().parent.parent / "omni_core" / "tools" / "mcp_bridge.py").exists()
    assert importlib.util.find_spec("agents.mcp") is not None
