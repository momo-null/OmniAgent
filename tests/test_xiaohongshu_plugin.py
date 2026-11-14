"""小红书只读采集插件（阶段 5：工具源改成 MCP 声明）离线测试。

契约变化：
- 插件不再自带 3 个 ``@function_tool``：工具由外部 MCP server 提供；
- manifest 的 ``tools.include`` 白名单**只含只读工具名**（零写操作靠结构表达）；
- 宿主 loader 只登记声明，不 import plugin.py、不注册工具；
- 双轨收口判定（``replaced_units``）认 manifest，不再依赖 mcp.json 的过渡字段。
"""
import importlib.util
import json
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "plugins" / "xiaohongshu"


def _load_plugin_module():
    """按 loader 同款方式装载插件模块（不污染全局注册表）。"""
    from omni_core.tools.base import TOOL_REGISTRY

    spec = importlib.util.spec_from_file_location(
        "xiaohongshu_plugin_under_test", PLUGIN_DIR / "plugin.py")
    mod = importlib.util.module_from_spec(spec)
    before = set(TOOL_REGISTRY)
    spec.loader.exec_module(mod)
    after = set(TOOL_REGISTRY)
    for name in after - before:
        TOOL_REGISTRY.pop(name, None)
    return mod, after - before


def test_manifest_declares_mcp_source_with_readonly_whitelist():
    doc = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    tools = doc["tools"]
    assert tools["source"] == "mcp"
    assert tools["server"] == "xiaohongshu"
    assert tools["include"] == ["list_feeds", "search_feeds", "get_feed_detail"]
    # 零写操作红线：白名单里不允许出现发布/评论/点赞类动词
    banned = ("publish", "comment", "like", "post", "send", "create")
    assert not [t for t in tools["include"] if any(b in t.lower() for b in banned)]


def test_plugin_module_registers_no_tools():
    _mod, added = _load_plugin_module()
    assert added == set(), f"插件不应再注册工具: {sorted(added)}"


def test_host_declares_but_does_not_import_mcp_plugin(tmp_path, monkeypatch):
    """loader 对 tools.source=mcp：只登记 server/include，不 import plugin.py。"""
    import sys
    from omni_core.tools import loader

    d = tmp_path / "demo_mcp"
    d.mkdir()
    (d / "plugin.json").write_text(json.dumps({
        "name": "demo_mcp", "enabled": True,
        "tools": {"source": "mcp", "server": "demo-server", "include": ["read_a"]},
    }), encoding="utf-8")
    (d / "plugin.py").write_text("raise RuntimeError('不该被 import')\n", encoding="utf-8")

    monkeypatch.setattr(loader, "_MCP_DECLARATIONS", {})
    cfg = {"runtime": {"tools": {"plugin_dirs": [str(tmp_path)]}}}
    report = loader.load_plugins(cfg)
    assert "demo_mcp" in report.loaded
    assert loader.mcp_declarations()["demo_mcp"] == {
        "server": "demo-server", "include": ["read_a"]}
    assert "_omni_plugin_demo_mcp" not in sys.modules


def test_replaced_units_accepts_manifest_units():
    from omni_core.tools.mcp_meta import replaced_units

    assert replaced_units(None) == set()
    assert replaced_units([{"name": "u1"}]) == set()
    assert replaced_units(None, {"xiaohongshu"}) == {"xiaohongshu"}
    assert replaced_units([{"name": "legacy", "replace_plugin_tools": True}],
                          {"xiaohongshu"}) == {"legacy", "xiaohongshu"}


def test_mcp_tool_units_reads_repo_manifests():
    from omni_core.tools.loader import mcp_tool_units

    units = mcp_tool_units()
    assert "xiaohongshu" in units
    assert "bilibili" not in units      # 本插件工具仍由自己注册
