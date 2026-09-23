"""工具注册表单测（工具定义走 SDK `function_tool`）。

覆盖：按名派发、schema 聚合与分组过滤、异常兜底、外部工具平级注册。
插件一律通过 SDK 的 function_tool 登记，注册表只做「枚举 + 同步 invoke」的视图。
"""
from omni_core.tools.base import (
    TOOL_REGISTRY,
    PluginRegistry,
    ToolPlugin,
    build_plugin_registry,
    call_tool,
    function_tool,
    register_external,
    sdk_tools,
    unregister_tool,
)


def _fake_plugin(name, group, ret, boom=False):
    """用 SDK function_tool 登记一个假插件（与真实插件同路径）。"""

    @function_tool(name=name, description=name, group=group)
    def _fake(value: str = "") -> dict:
        if boom:
            raise RuntimeError("kaboom")
        return dict(ret)

    return TOOL_REGISTRY[name]


def test_registry_routes_by_name():
    register_external(_fake_plugin("_t_x", "test", {"ok": True, "src": "x"}))
    register_external(_fake_plugin("_t_y", "test", {"ok": True, "src": "y"}))
    try:
        reg = build_plugin_registry()
        assert reg.dispatch("_t_x", {"value": "1"})["src"] == "x"
        assert reg.dispatch("_t_y")["src"] == "y"
    finally:
        unregister_tool("_t_x")
        unregister_tool("_t_y")


def test_registry_unknown_tool_error():
    res = call_tool("__definitely_not_a_tool__", {})
    assert res["ok"] is False
    assert "unknown tool" in res["error"]


def test_registry_catches_plugin_exception():
    """failure_error_function=None：异常不外泄成「成功字符串」，而是被回填成错误 dict。"""
    register_external(_fake_plugin("_t_boom", "test", {}, boom=True))
    try:
        res = call_tool("_t_boom")
        assert res["ok"] is False
        assert "RuntimeError" in res["error"]
    finally:
        unregister_tool("_t_boom")


def test_group_filter_and_schema_aggregation():
    register_external(_fake_plugin("_t_a", "grpA", {}))
    register_external(_fake_plugin("_t_b", "grpB", {}))
    try:
        reg = PluginRegistry(["grpA"])
        assert reg.names().count("_t_a") == 1
        assert "_t_b" not in reg.names()
        assert {s["function"]["name"] for s in reg.schemas} == {"_t_a"}
        # 交给 SDK Agent 的工具清单同样受分组过滤
        assert [t.name for t in sdk_tools(["grpA"])] == ["_t_a"]
        # 未指定分组 = 全部启用
        assert "_t_b" in build_plugin_registry().names()
    finally:
        unregister_tool("_t_a")
        unregister_tool("_t_b")


def test_external_mcp_tool_is_peer_with_builtin():
    """外部 MCP 工具与自研工具同表、同派发路径（平级，无专属通道）。"""
    register_external(_fake_plugin("_t_mcp", "mcp", {"ok": True, "src": "mcp"}))
    try:
        p = TOOL_REGISTRY["_t_mcp"]
        p.source = "mcp"
        # 与自研工具一样：注册表里有、schema 可取、按名可派发
        assert p.schema["function"]["name"] == "_t_mcp"
        assert call_tool("_t_mcp")["src"] == "mcp"
        assert build_plugin_registry().has("_t_mcp") is True
    finally:
        unregister_tool("_t_mcp")


def test_plugin_holds_sdk_function_tool():
    """注册表持有的是 SDK FunctionTool（不是自研 schema/函数对）。"""
    from agents.tool import FunctionTool

    register_external(_fake_plugin("_t_sdk", "test", {"ok": True}))
    try:
        plugin = TOOL_REGISTRY["_t_sdk"]
        assert isinstance(plugin, ToolPlugin)
        assert isinstance(plugin.tool, FunctionTool)
        assert plugin.tool.name == "_t_sdk"
    finally:
        unregister_tool("_t_sdk")
