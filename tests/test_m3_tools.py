"""M3 设备能力 tool 化单测：键鼠路由 / 感知 / 通用件（template_match、wait_for、drag）。

设备能力已从 `brain/providers/*` 的手搓派发平移为 agent 外层 tool 插件
（omni_core.tools.device_tool），与视觉/Python/外部 MCP 平级。
不依赖 GPU / 真实模型 / 模拟器：用 fake 执行后端注入插件层。
"""
from PIL import Image, ImageDraw

from omni_core.tools.base import TOOL_REGISTRY, call_tool, schemas
from omni_core.tools.device_tool import bind_execution_module


class _FakeBackendInner:
    """模拟真实后端：提供 text_of（去场景化，插件不读感知字段）。"""

    def text_of(self, percept: dict) -> str:
        return " ".join(str(x) for x in ((percept or {}).get("ocr_text") or []))

    def verify_done(self, cond: str, percept: dict):
        return (cond or "") in self.text_of(percept), "ok"


class _FakeExec:
    def __init__(self, shot_path=None, percept=None):
        self.calls = []
        self._shot = shot_path
        self._percept = percept or {}
        self.backend = _FakeBackendInner()

    # --- 感知 ---
    def observe(self):
        self.calls.append(("observe",))
        return {"ok": True, "percept": "fake"}

    def read_screen_text(self):
        self.calls.append(("read_screen_text",))
        return dict(self._percept)

    def ocr_screenshot(self):
        self.calls.append(("ocr_screenshot",))
        return {"ok": True, "percept": "fake"}

    def screenshot(self, save_path=None):
        self.calls.append(("screenshot", save_path))
        return {"ok": True, "path": self._shot}

    # --- 动作 ---
    def execute_mouse_action(self, action, params):
        self.calls.append((action, dict(params)))
        return True

    def execute_keyboard_action(self, action, params):
        self.calls.append((action, dict(params)))
        return True

    def wait(self, ms):
        self.calls.append(("wait", ms))
        return {"ok": True, "waited_ms": ms}


def _bind(exec_mod):
    bind_execution_module(exec_mod)
    return exec_mod


# --- 注册与分组 -------------------------------------------------------------
def test_device_tools_registered_as_plugins():
    """设备工具按后端可移植性分两组：

    - ``device``：通用键鼠/感知（host 与 emulator 都可用）
    - ``device_emulator``：Android 专属原语（UI 层级 / resource-id 点击 / keycode / 包名启动…），
      host 后端下由 tool_loop 的设备组收窄逻辑剔除，不暴露给模型。
    """
    for name in (
        "press", "hotkey", "type", "wait", "click", "drag",
        "observe", "read_screen_text", "screenshot",
        "template_match", "wait_for",
    ):
        assert name in TOOL_REGISTRY, f"{name} 应为平级插件"
        assert TOOL_REGISTRY[name].group == "device", f"{name} 应属通用 device 组"

    for name in (
        "ocr_screenshot", "get_ui_tree", "tap_by_id", "tap_text",
        "launch_app", "press_keycode", "collect_list",
    ):
        assert name in TOOL_REGISTRY, f"{name} 应为平级插件"
        assert TOOL_REGISTRY[name].group == "device_emulator", f"{name} 应属 emulator 专属子组"


def test_device_schema_has_param_description():
    fn = TOOL_REGISTRY["template_match"].schema["function"]
    assert fn["name"] == "template_match"
    assert "template_path" in fn["parameters"]["required"]
    # 参数说明来自 params 映射，而非裸参数名（LLM 可读）
    assert fn["parameters"]["properties"]["threshold"]["description"] != "threshold"


def test_type_tool_uses_type_name_not_builtin():
    """键盘输入工具的对外名必须是 'type'（不暴露 Python 内置名 input_text）。"""
    assert "type" in TOOL_REGISTRY
    assert "input_text" not in TOOL_REGISTRY


# --- 键鼠路由 ---------------------------------------------------------------
def test_keyboard_and_mouse_route_to_backend():
    fake = _bind(_FakeExec())
    assert call_tool("press", {"key": "enter"})["ok"] is True
    assert call_tool("hotkey", {"keys": "ctrl,c"})["keys"] == ["ctrl", "c"]
    assert call_tool("type", {"text": "123+456="})["ok"] is True
    assert call_tool("click", {"x": 0.5, "y": 0.4})["ok"] is True
    assert call_tool("wait", {"ms": 30})["waited_ms"] == 30
    assert ("press", {"key": "enter"}) in fake.calls
    assert ("hotkey", {"keys": ["ctrl", "c"]}) in fake.calls
    assert ("type", {"text": "123+456="}) in fake.calls
    assert ("click", {"x": 0.5, "y": 0.4}) in fake.calls


def test_drag_routes_to_backend():
    fake = _bind(_FakeExec())
    res = call_tool("drag", {"from_x": 0.1, "from_y": 0.2, "to_x": 0.8, "to_y": 0.9})
    assert res["ok"] is True
    assert ("drag", {"from_x": 0.1, "from_y": 0.2, "to_x": 0.8, "to_y": 0.9}) in fake.calls


def test_perception_routes_to_backend():
    fake = _bind(_FakeExec(percept={"ocr_text": ["登录"]}))
    assert call_tool("observe", {})["ok"] is True
    assert call_tool("read_screen_text", {}) == {"ocr_text": ["登录"]}
    assert ("read_screen_text",) in fake.calls


# --- 通用件 -----------------------------------------------------------------
def test_template_match_finds_patch(tmp_path):
    # 纯色模板在 TM_CCOEFF_NORMED 下方差为零会数值退化；用带内部纹理的模板
    base = Image.new("RGB", (200, 200), (0, 128, 0))
    templ = Image.new("RGB", (30, 30), (255, 0, 0))
    ImageDraw.Draw(templ).rectangle([10, 10, 20, 20], fill=(0, 0, 255))  # 蓝心制造方差
    base.paste(templ, (100, 100))
    base_path = str(tmp_path / "base.png")
    templ_path = str(tmp_path / "templ.png")
    base.save(base_path)
    templ.save(templ_path)

    _bind(_FakeExec(shot_path=base_path))
    res = call_tool("template_match", {"template_path": templ_path})
    assert res["ok"] is True
    assert res["found"] is True
    assert res["score"] >= 0.9
    cx, cy = res["center"]
    # 中心像素 (115,115) / 200 = 0.575
    assert abs(cx - 0.575) < 0.02
    assert abs(cy - 0.575) < 0.02


def test_template_match_missing_template(tmp_path):
    _bind(_FakeExec(shot_path="x.png"))
    res = call_tool("template_match", {"template_path": str(tmp_path / "nope.png")})
    assert res["ok"] is False


def test_template_match_miss_marks_no_confidence(tmp_path):
    """未匹配到 -> no_confidence（内核据此升级）。

    两张互不相关的随机噪声图：归一化相关系数接近 0，必然低于阈值。
    """
    import numpy as np

    rng = np.random.default_rng(7)
    base = Image.fromarray(rng.integers(0, 255, (100, 100, 3), dtype=np.uint8))
    templ = Image.fromarray(rng.integers(0, 255, (20, 20, 3), dtype=np.uint8))
    base_path = str(tmp_path / "b.png")
    templ_path = str(tmp_path / "t.png")
    base.save(base_path)
    templ.save(templ_path)

    _bind(_FakeExec(shot_path=base_path))
    res = call_tool("template_match", {"template_path": templ_path, "threshold": 0.8})
    assert res["ok"] is True
    assert res["found"] is False
    assert res["score"] < 0.8
    assert res["no_confidence"] is True


def test_wait_for_finds_text():
    class _Seq(_FakeExec):
        def __init__(self):
            super().__init__()
            self.n = 0

        def read_screen_text(self):
            self.n += 1
            self.calls.append(("read_screen_text",))
            return {"ocr_text": ["加载中"] if self.n < 2 else ["登录", "开始"]}

    fake = _bind(_Seq())
    res = call_tool("wait_for", {"text": "登录", "timeout_ms": 2000, "interval_ms": 50})
    assert res["ok"] is True and res["found"] is True
    assert fake.n >= 2


def test_wait_for_timeout():
    _bind(_FakeExec(percept={"ocr_text": ["加载中"]}))
    res = call_tool("wait_for", {"text": "登录", "timeout_ms": 250, "interval_ms": 50})
    assert res["ok"] is True and res["found"] is False and res.get("timeout") is True


# --- 兜底 -------------------------------------------------------------------
def test_unsupported_capability_returns_error_dict_not_raise():
    """后端不支持的原语（fake 无 tap_text）→ 错误 dict 回填，不中断循环。"""
    _bind(_FakeExec())
    res = call_tool("tap_text", {"text": "确定"})
    assert res["ok"] is False
    assert "error" in res


def test_kernel_has_no_handrolled_dispatch():
    """M3：内核不再有 Provider 聚合与手搓派发入口。"""
    import inspect
    from pathlib import Path

    import omni_core.brain.tools as brain_tools
    import omni_core.local.tool_loop as tool_loop

    assert not hasattr(brain_tools, "build_registry")
    assert not hasattr(brain_tools, "dispatch_tool")

    import omni_core.local.loop as _lp
    _ld = Path(inspect.getfile(_lp)).parent
    src = "\n".join((_ld / f).read_text(encoding="utf-8") for f in sorted(_ld.glob("*.py")))
    assert "build_registry" not in src
    # 内核只注入运行时 + 取插件层 schema，不按工具名分支
    assert "bind_execution_module" in src
    assert "build_plugin_registry" in src


def test_tool_loop_schemas_come_from_plugin_layer(monkeypatch):
    """内核工具清单由插件层聚合（含 device / python 分组），不由后端硬塞。"""

    class _FakeBrain:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, tools=None, tool_choice="auto"):
            raise AssertionError("本用例不下发 LLM 调用")

        def close(self):
            pass

    class _FakeExecModule:
        tool_schemas = []

        def __init__(self, *a, **k):
            self.backend = _FakeBackendInner()
            self.backend_kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

    monkeypatch.setattr("omni_core.local.loop.core.ExecutionModule", _FakeExecModule)
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _FakeBrain)

    from omni_core.local.tool_loop import ToolLoop

    loop = ToolLoop({"model": "m", "base_url": "http://x", "capabilities": {}}, verbose=False)
    names = {s["function"]["name"] for s in loop.tool_schemas}
    # 设备能力已 tool 化（内核零持有）
    assert {"press", "click", "observe", "template_match"}.issubset(names)
    # Python 执行能力（§5.5）
    assert "run_python" in names
    # 内核元工具不在插件层（由 tool_loop 门控）
    assert "task_done" not in names


def test_device_group_filter():
    names = {s["function"]["name"] for s in schemas(["device"])}
    assert {"press", "click", "template_match"}.issubset(names)
    # 视觉/Python 分组不在 device 视图内
    assert "vision_describe" not in names
    assert "run_python" not in names
