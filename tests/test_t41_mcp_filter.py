"""T4.1（O1 配套）— MCP 工具分组 & 按需注入。

覆盖：
1. include / exclude glob 规则精准生效，工具按需展示。
2. 无过滤配置时默认全量放行（tool_filter=None），行为无回归。
3. 命名形态符合探明的 SDK 规范；非法配置静默跳过不报错。

**命名规范探测结论（留存备查）**：
    本版 agents SDK（``agents/mcp/util.py::_build_prefixed_tool_base_name``）
    原生即为 MCP 工具加前缀 ``mcp_<server>__<tool>``，同名冲突时 SDK 自动追加
    短哈希兜底。即 **原生命名已自带命名空间、不存在冲突**，本项目因此保留原生
    名称，不自造 ``mcp__<server>__<raw>`` 适配层。过滤配置的 glob 同时兼容
    「原始工具名」与「带前缀暴露名」两种写法。
"""
import asyncio

import pytest
from mcp.types import Tool as MCPTool

from omni_core.tools.mcp_servers import build_mcp_servers


def _tool(name):
    # 必须是真实 MCPTool（pydantic 模型）：SDK 过滤前会 model_copy 快照
    return MCPTool(name=name, description="", inputSchema={"type": "object", "properties": {}})


def _filter_of(cfg):
    servers = build_mcp_servers([cfg])
    assert len(servers) == 1
    return servers[0]


def _apply(server, names, server_name="fs"):
    """走 SDK 原生过滤管线（_apply_dynamic_tool_filter）得到最终保留的工具。"""
    tools = [_tool(n) for n in names]
    kept = asyncio.run(server._apply_dynamic_tool_filter(tools, None, None))
    return [t.name for t in kept]


# --- 1. include / exclude 生效 -----------------------------------------------
def test_include_exclude_globs_filter_tools():
    server = _filter_of({
        "name": "fs", "enabled": True, "command": "npx",
        "tool_filter": {"include": ["read_*", "list_*"], "exclude": ["read_secret"]},
    })
    assert callable(server.tool_filter), "应注入 SDK 原生动态过滤器"

    kept = _apply(server, ["read_file", "list_dir", "read_secret", "write_file"])
    assert kept == ["read_file", "list_dir"]


def test_exclude_only_blocks_matching_tools():
    server = _filter_of({
        "name": "fs", "enabled": True, "command": "npx",
        "tool_filter": {"exclude": ["delete_*"]},
    })
    kept = _apply(server, ["read_file", "delete_all", "write_file"])
    assert kept == ["read_file", "write_file"]


def test_http_server_supports_tool_filter():
    server = _filter_of({
        "name": "remote", "enabled": True, "url": "http://127.0.0.1:8010/mcp",
        "tool_filter": {"include": ["search"]},
    })
    assert callable(server.tool_filter)
    assert _apply(server, ["search", "other"], server_name="remote") == ["search"]


# --- 2. 无过滤配置 -> 全量放行 -------------------------------------------------
def test_no_filter_config_passes_all_tools():
    server = _filter_of({"name": "fs", "enabled": True, "command": "npx"})
    assert server.tool_filter is None, "无过滤配置应全量放行（默认行为不变）"
    # 直接走原生管线验证：无过滤器时原样返回
    tools = [_tool("read_file"), _tool("write_file")]
    assert asyncio.run(server._apply_tool_filter(tools)) == tools


def test_empty_filter_lists_pass_all_tools():
    server = _filter_of({
        "name": "fs", "enabled": True, "command": "npx",
        "tool_filter": {"include": [], "exclude": []},
    })
    assert server.tool_filter is None


# --- 3. 命名规范 & 非法配置静默跳过 -------------------------------------------
def test_prefixed_exposed_name_also_matches():
    """配置里写 SDK 暴露名（mcp_<server>__<tool>）同样命中。"""
    server = _filter_of({
        "name": "fs", "enabled": True, "command": "npx",
        "tool_filter": {"include": ["mcp_fs__read_*"]},
    })
    kept = _apply(server, ["read_file", "write_file"])
    assert kept == ["read_file"]
    # 命名规范：SDK 原生前缀形态为 mcp_<server>__<tool>
    assert f"mcp_fs__read_file".startswith("mcp_fs__")


def test_invalid_filter_config_skipped_silently():
    """非法配置（非 dict / include 非列表）静默跳过，不抛异常、不过滤。"""
    logs = []
    server = _filter_of({
        "name": "fs", "enabled": True, "command": "npx",
        "tool_filter": "not-a-dict",
    })
    assert server.tool_filter is None

    server2 = build_mcp_servers([{
        "name": "fs2", "enabled": True, "command": "npx",
        "tool_filter": {"include": "read_*"},
    }], log=logs.append)[0]
    assert server2.tool_filter is None
    assert any("非法" in m for m in logs), "非法配置应留日志痕迹"


def test_disabled_and_invalid_servers_still_skipped():
    servers = build_mcp_servers([
        {"name": "off", "enabled": False, "command": "npx"},
        {"name": "empty"},                       # 无 command 也无 url -> 静默跳过
        {"name": "ok", "enabled": True, "command": "npx",
         "tool_filter": {"exclude": ["x"]}},
    ])
    assert len(servers) == 1
    assert callable(servers[0].tool_filter)
