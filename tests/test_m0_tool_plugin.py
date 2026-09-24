"""M0 验证：agent 外层 tool 插件层（工具=平级插件，内核零派发）。

不依赖 GPU / 真实模型 / 模拟器：用 fake VisionRuntime 注入插件层，
验证 function_tool 装饰器隔离的「定义/注册/派发/回填」四跳，以及 vision
工具已从 providers 手搓派发迁为平级插件。
"""
import pytest

from omni_core.tools.base import TOOL_REGISTRY, call_tool, schemas
from omni_core.tools.loader import load_plugins, plugin_module


@pytest.fixture(autouse=True)
def _load_official_plugins():
    """P3：vision 已迁为官方插件（plugins/vision），按名访问前需先装载。"""
    load_plugins({})


def _vision():
    """取已装载的 vision 插件模块句柄。"""
    module = plugin_module("vision")
    assert module is not None, "vision 插件未装载"
    return module


class _FakeVision:
    """最小 VisionRuntime stub：仅实现插件层用到的 4 个方法。"""
    def __init__(self):
        self.calls = []

    def vision_describe(self, prompt):
        self.calls.append(("vision_describe", prompt))
        return {"ok": True, "description": f"fake:{prompt}"}

    def som_ground(self):
        self.calls.append(("som_ground",))
        return {"ok": True, "marks": [{"id": 1, "resource_id": "x", "label": "btn"}]}

    def visual_so_m(self, ask=""):
        self.calls.append(("visual_so_m", ask))
        return {"ok": True, "marks": [{"id": 2, "center": [0.5, 0.6], "label": "icon"}]}

    def tap_mark(self, mark):
        """α 决策：marks 状态由插件自持，运行时只负责按 mark 落点。"""
        self.calls.append(("tap_mark", mark.get("id")))
        return {"ok": True, "tapped": mark.get("id")}


def test_function_tool_auto_schema():
    # [1] 定义：[2] 注册：装饰器自动产出 schema 并登记
    assert "vision_describe" in TOOL_REGISTRY
    plugin = TOOL_REGISTRY["vision_describe"]
    fn = plugin.schema["function"]
    assert fn["name"] == "vision_describe"
    assert fn["parameters"]["properties"]["prompt"]["type"] == "string"
    assert "prompt" in fn["parameters"]["required"]


def test_all_vision_tools_registered_as_plugins():
    for name in ("vision_describe", "som_ground", "som_marks", "tap_by_mark"):
        assert name in TOOL_REGISTRY, f"{name} 应为平级插件"
        assert TOOL_REGISTRY[name].group == "vision"


def test_dispatch_routes_to_plugin_no_core_branch():
    # [3] 派发：[4] 回填：call_tool 按名路由，无 if name== 分支
    fake = _FakeVision()
    _vision().bind_vision_runtime(fake)

    res = call_tool("vision_describe", {"prompt": "这是什么界面？"})
    assert res == {"ok": True, "description": "fake:这是什么界面？"}

    res = call_tool("som_marks", {"ask": "列出按钮"})
    assert res["ok"] is True
    assert res["marks"][0]["id"] == 2

    res = call_tool("tap_by_mark", {"mark_id": 2})
    assert res["tapped"] == 2

    assert fake.calls == [
        ("vision_describe", "这是什么界面？"),
        ("visual_so_m", "列出按钮"),
        ("tap_mark", 2),
    ]


def test_unknown_tool_returns_error_dict():
    res = call_tool("does_not_exist", {})
    assert res["ok"] is False
    assert "unknown tool" in res["error"]


def test_schemas_exportable_for_llm():
    names = {s["function"]["name"] for s in schemas()}
    assert {"vision_describe", "som_ground", "som_marks", "tap_by_mark"}.issubset(names)
