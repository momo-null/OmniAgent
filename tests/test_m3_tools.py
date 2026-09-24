"""M3 设备能力 tool 化单测：键鼠路由 / 感知 / 通用件（template_match、wait_for、drag）。

设备能力是**环境的工具面**（`environments/<kind>/tools.py`）：激活哪个环境，
就注册哪个环境的工具。
不依赖 GPU / 真实模型 / 模拟器：用 fake 执行后端注入环境工具面。
"""
from PIL import Image, ImageDraw

import pytest

from omni_core.tools.base import TOOL_REGISTRY, call_tool, schemas
from tests._env import install_fake_env  # noqa: E402


@pytest.fixture(autouse=True)
def _load_host_env_tools():
    """设备工具＝host 环境工具面：**import 该模块即注册**。

    刻意不用 ``activate_environment``：那会构造并绑定**真实** HostBackend，
    用例若漏了 ``_bind`` 就会操作真实桌面（也慢）。这里只登记，由用例自绑 fake。
    """
    from environments.host import tools  # noqa: F401  (import 即注册工具)


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
    """把 fake 后端注入 host 环境工具面（工具经 `_BACKEND` 调用原语）。"""
    from environments.host import tools as host_tools

    host_tools.bind(exec_mod)
    return exec_mod


# --- 注册与环境归属 ---------------------------------------------------------
def test_host_env_tools_registered():
    """激活 host → 它的整套工具进注册表（unit=host、source=env）。

    Android 专属原语不属于 host 环境（换环境＝重装，见 §4）。
    """
    for name in (
        "press", "hotkey", "type", "wait", "click", "drag",
        "observe", "read_screen_text", "screenshot",
        "template_match", "wait_for",
    ):
        assert name in TOOL_REGISTRY, f"{name} 应为 host 环境工具"
        plugin = TOOL_REGISTRY[name]
        assert plugin.unit == "host", f"{name} 应属 host 环境"
        assert plugin.source == "env", f"{name} 来源应为 env"

    for name in (
        "ocr_screenshot", "get_ui_tree", "tap_by_id", "tap_text",
        "launch_app", "press_keycode", "collect_list",
    ):
        assert name not in TOOL_REGISTRY, f"{name} 是 Android 专属，不该出现在 host 环境"


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
    """当前环境没有的原语（如 Android 专属 tap_text 在 host 环境）→ 错误 dict，不中断循环。"""
    _bind(_FakeExec())
    res = call_tool("tap_text", {"text": "确定"})
    assert res["ok"] is False
    assert "error" in res


def test_kernel_has_no_handrolled_dispatch():
    """M3：内核不再有 Provider 聚合与手搓派发入口。"""
    import inspect
    from pathlib import Path

    import omni_core.brain.tools as brain_tools
    import omni_core.local.loop.core as tool_loop

    assert not hasattr(brain_tools, "build_registry")
    assert not hasattr(brain_tools, "dispatch_tool")

    import omni_core.local.loop as _lp
    _ld = Path(inspect.getfile(_lp)).parent
    src = "\n".join((_ld / f).read_text(encoding="utf-8") for f in sorted(_ld.glob("*.py")))
    assert "build_registry" not in src
    # 内核只注入运行时 + 取插件层 schema，不按工具名分支；
    # P3：设备/视觉已迁为插件，内核改经 loader 装载、不再点名绑定函数。
    assert "load_plugins" in src
    assert "build_plugin_registry" in src


def test_tool_loop_schemas_come_from_registry(monkeypatch):
    """内核工具清单由**注册表**聚合（环境工具 + 插件工具 + core），不由后端硬塞 schema。"""

    class _FakeBrain:
        def __init__(self, *a, **k):
            pass

        def chat(self, messages, tools=None, tool_choice="auto"):
            raise AssertionError("本用例不下发 LLM 调用")

        def close(self):
            pass

    class _FakeExecModule:
        def __init__(self, *a, **k):
            self.backend = _FakeBackendInner()
            self.kind = "host"

    install_fake_env(monkeypatch, _FakeExecModule)
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _FakeBrain)

    from omni_core.local.loop import ToolLoop

    loop = ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "capabilities": {}}, verbose=False)
    names = {s["function"]["name"] for s in loop.tool_schemas}
    # 环境工具（autouse 已激活 host）
    assert {"press", "click", "observe", "template_match"}.issubset(names)
    # 执行能力只剩内核 builtin shell（run_python 已删）
    assert "shell_exec" in names
    assert "run_python" not in names
    # 内核元工具不在插件层（由循环门控）
    assert "task_done" not in names
    # 后端不再提供 schema（能力暴露唯一来源是注册表）
    assert not hasattr(_FakeExecModule, "tool_schemas")


def test_registry_is_unfiltered_view():
    """注册表是只读视图：无分组/排除过滤参数（门禁＝环境单选 + 插件开关）。"""
    from omni_core.tools.base import sdk_tools

    names = {s["function"]["name"] for s in schemas()}
    assert names == {t.name for t in sdk_tools()}
    assert names, "注册表不应为空"
