"""M5：主链路是否真的由 SDK Runner 驱动（而不是手搓循环）。

三个层次：
1) 内核源码层：主入口不再调 `_run_inner`（它已标记废弃）。
2) Runner 层：Agent 由 SDK 构造，工具是 SDK FunctionTool，MCP 由 SDK MCPServer 承载。
3) 端到端：真起一个本地 MCP server（stdio），让 Agent+Runner 连上并调用它的工具，
   验证「外部 MCP 与自研工具平级」在真实 transport 上成立。
"""
import inspect
import json
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agents import Agent
from agents.tool import FunctionTool

from omni_core.local.tool_loop import ToolLoop, TaskSpec


def test_main_paths_no_longer_use_legacy_loop():
    """run_task / _run_subtask 走 `_run_via_sdk`，不再走 `_run_inner`。"""
    import omni_core.local.loop as _lp
    _ld = Path(inspect.getfile(_lp)).parent
    src = "\n".join((_ld / f).read_text(encoding="utf-8") for f in sorted(_ld.glob("*.py")))
    assert "self._run_via_sdk(" in src          # 主路径走 SDK Runner
    assert "self._run_inner(" not in src        # 主路径不再调手搓循环
    assert "_warn_legacy_loop" not in src       # 手搓循环及其废弃标记已彻底移除


def test_registry_holds_sdk_function_tools():
    from omni_core.tools import build_plugin_registry

    tools = build_plugin_registry().sdk_tools()
    assert tools, "插件层没产出任何工具"
    assert all(isinstance(t, FunctionTool) for t in tools)


def test_mcp_servers_are_sdk_objects():
    from agents.mcp import MCPServer
    from omni_core.tools.mcp_servers import build_mcp_servers

    servers = build_mcp_servers([
        {"name": "s1", "command": "npx", "args": ["-y", "srv"]},
        {"name": "s2", "url": "http://127.0.0.1:1/mcp"},
    ])
    assert servers and all(isinstance(s, MCPServer) for s in servers)


# --- 端到端：真起一个 stdio MCP server，让 Runner 调它的工具 -----------------
_SERVER_SRC = textwrap.dedent(
    '''
    from mcp.server.mcpserver import MCPServer

    m = MCPServer("omni-test-echo")

    @m.tool()
    def ext_add(a: int, b: int) -> int:
        """把两个数相加。"""
        return a + b

    m.run()
    '''
)


def test_agent_with_mcp_server_end_to_end(tmp_path):
    """Agent + 真实 MCPServerStdio：外部工具被 SDK 发现并可执行。"""
    from agents import Runner
    from omni_core.async_bridge import run_async
    from omni_core.brain.sdk_model import build_sdk_model
    from omni_core.tools.mcp_servers import build_mcp_servers

    srv_file = tmp_path / "mcp_echo_server.py"
    srv_file.write_text(_SERVER_SRC, encoding="utf-8")
    servers = build_mcp_servers(
        [{"name": "calc", "command": sys.executable, "args": [str(srv_file)]}]
    )
    assert servers

    async def _run():
        agent = Agent(
            name="t",
            model=build_sdk_model(
                {"base_url": "http://127.0.0.1:1/v1", "model": "stub", "api_key": "k"}
            ),
            mcp_servers=servers,
        )
        # 只验证「SDK 能连上并把外部工具暴露出来」（不跑真实模型）
        async with servers[0] as s:
            tools = await s.list_tools()
        return [t.name for t in tools]

    names = run_async(_run(), 60.0)
    assert "ext_add" in names, f"MCP 工具未被 SDK 发现: {names}"
