"""U2 工具级禁用（per-tool disable）单测 + 接口集成测试。

验证：
1. 单元测试：禁用名单可精准过滤单工具，空名单正常加载全部工具；
2. 兼容性：旧签名（无 excluded 参数）查询方法仍返回全部工具，存量无回归；
3. 接口集成：PATCH /api/runtime/tools/disabled 禁用后 GET /tools 标记该工具为 disabled（仍列出，供界面重新开启）且
   disabled 字段同步；清空后恢复；非法工具名返回 400。

不依赖 GPU / 模型 / 模拟器；临时工具注册后 fixture 自动销毁，避免污染。
"""
import pytest

from omni_core.tools.base import (
    PluginRegistry,
    TOOL_REGISTRY,
    build_plugin_registry,
    function_tool,
    schemas,
    unregister_tool,
)


@pytest.fixture
def temp_tool():
    """注册一个临时工具（group=python，确保出现在 GET /tools 的启用分组内），
    测试结束后自动销毁。"""
    @function_tool(name="u2_temp_tool", group="python", description="u2 测试工具")
    def _u2_tmp(args: str = "") -> dict:
        """u2 临时工具（测试用）。"""
        return {"ok": True}

    yield "u2_temp_tool"
    unregister_tool("u2_temp_tool")


# --- 1. 单元测试：禁用名单精准过滤 -------------------------------------------
def test_excluded_filters_single_tool(temp_tool):
    reg = build_plugin_registry(None, excluded=[temp_tool])
    names = {p.name for p in reg.plugins}
    assert temp_tool not in names
    # 其它工具不受影响
    assert "run_python" in names


def test_empty_excluded_loads_all(temp_tool):
    reg = build_plugin_registry(None, excluded=[])
    assert temp_tool in {p.name for p in reg.plugins}
    # 无 excluded 参数（旧调用方）同样加载全部
    reg2 = build_plugin_registry(None)
    assert temp_tool in {p.name for p in reg2.plugins}


# --- 2. 兼容性：旧签名查询方法无回归 -----------------------------------------
def test_legacy_query_without_excluded(temp_tool):
    # PluginRegistry.__init__ 无 excluded 参数仍可构造并返回全部
    pr = PluginRegistry(None)
    assert temp_tool in set(pr.names())
    assert temp_tool in {p.name for p in pr.plugins}
    # 模块级 schemas(groups) 旧调用方式仍工作
    all_schemas = schemas(None)
    assert any(s["function"]["name"] == temp_tool for s in all_schemas)


# --- 3. 接口集成：PATCH 禁用 -> GET 隐藏 -> 清空恢复 -------------------------
def test_patch_disables_tool_and_get_hides_it(client, temp_tool):
    # PATCH 禁用
    r = client.patch("/api/runtime/tools/disabled", json={"disabled": [temp_tool]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["disabled"] == [temp_tool]

    # GET /tools：工具仍列出（否则界面无法再打开），但带 disabled 标记
    g = client.get("/api/runtime/tools").json()
    entry = next(t for t in g["tools"] if t["name"] == temp_tool)
    assert entry["disabled"] is True
    assert temp_tool in g["disabled"]

    # 清空后恢复
    r2 = client.patch("/api/runtime/tools/disabled", json={"disabled": []})
    assert r2.status_code == 200, r2.text
    g2 = client.get("/api/runtime/tools").json()
    entry2 = next(t for t in g2["tools"] if t["name"] == temp_tool)
    assert entry2["disabled"] is False
    assert g2["disabled"] == []


def test_patch_rejects_illegal_tool_name(client):
    r = client.patch(
        "/api/runtime/tools/disabled", json={"disabled": ["not_a_real_tool_xyz"]}
    )
    assert r.status_code == 400
    assert "非法工具名" in r.json()["error"]


def test_patch_rejects_non_list(client):
    r = client.patch("/api/runtime/tools/disabled", json={"disabled": "run_python"})
    assert r.status_code == 400
