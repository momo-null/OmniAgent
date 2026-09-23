"""M1/M4：设备驱动层（devices/）单测。

M4：设备实现已从内核 omni_core/ 迁出到顶层 devices/ 包（L1 能力层），
且后端不再声明 tool_schemas——能力暴露的唯一来源是 tool 插件层。

覆盖：
- create_backend 按 config.runtime.backend 选 host / emulator
- EmulatorBackend 构造不要求设备在线（u2 懒导入）
- HostBackend 坐标归一化
- 设备工具经插件层 call_tool 按名派发（内核零分支）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devices import create_backend, ExecutionModule, HostBackend, EmulatorBackend
from omni_core.tools.base import call_tool, TOOL_REGISTRY
from omni_core.tools.device_tool import bind_execution_module


def test_create_backend_default_host():
    b = create_backend({})
    assert isinstance(b, HostBackend)
    assert b.name == "host"


def test_create_backend_emulator():
    b = create_backend({"runtime": {"backend": "emulator"}})
    assert isinstance(b, EmulatorBackend)
    assert b.name == "emulator"
    # 构造不应连接设备，adb_serial 来自配置
    assert b.adb_serial == "emulator-5554"


def test_device_driver_exposes_no_tool_schemas():
    """M4：后端不再声明 tool_schemas——能力暴露唯一来源是 tool 插件层。

    反向依赖被切断：设备层不 import 内核的 brain/tools，也不向内核塞 schema。
    """
    emu = EmulatorBackend({"runtime": {"emulator": {"adb_serial": "emulator-5554"}}})
    assert not hasattr(emu, "tool_schemas")
    host = HostBackend({})
    assert not hasattr(host, "tool_schemas")
    # 等价能力由插件层声明：通用设备工具 group=device；
    # Android 专属原语为 device_emulator 子组（host 后端下不暴露）
    for t in ("screenshot", "type", "click"):
        assert t in TOOL_REGISTRY
        assert TOOL_REGISTRY[t].group == "device"
    for t in ("get_ui_tree", "tap_by_id", "launch_app", "press_keycode"):
        assert t in TOOL_REGISTRY
        assert TOOL_REGISTRY[t].group == "device_emulator"


def test_execution_module_reads_config_backend():
    # ExecutionModule() 无参时读取 config.yaml 的 runtime.backend，
    # 不再假定固定默认；与当前活动配置（emulator/host）解耦。
    import yaml
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    expected = (cfg.get("runtime") or {}).get("backend", "host")
    em = ExecutionModule()
    assert em.backend_kind == expected
    # 去场景化契约（§9）：内核只经这两个抽象方法用后端
    assert callable(em.backend.text_of)
    assert callable(em.backend.verify_done)


def test_host_normalize_coordinate():
    # HostBackend 用真实屏幕分辨率覆盖配置；这里直接给定尺寸验证纯数学
    h = HostBackend({})
    h.screen_width, h.screen_height = 1000, 500
    h.coordinate_normalization = True
    assert h.normalize_coordinate(0.5, 0.5) == (500, 250)
    # 绝对坐标原样
    assert h.normalize_coordinate(10, 20) == (10, 20)
    # 关闭归一化：相对坐标也按绝对处理
    h.coordinate_normalization = False
    assert h.normalize_coordinate(10, 20) == (10, 20)


class _FakeExec:
    """记录被调用的方法，用于验证 dispatch 路由。"""
    def __init__(self):
        self.calls = []

    def observe(self):
        self.calls.append(("observe",))
        return {"active_window": "x", "ocr_text": ["1"]}

    def read_screen_text(self):
        self.calls.append(("read_screen_text",))
        return {"ocr_text": ["1"]}

    def get_ui_tree(self):
        self.calls.append(("get_ui_tree",))
        return {"ok": True, "ui_tree": "<node/>"}

    def tap_by_id(self, resource_id):
        self.calls.append(("tap_by_id", resource_id))
        return {"ok": True}

    def launch_app(self, package):
        self.calls.append(("launch_app", package))
        return {"ok": True}

    def press_keycode(self, code):
        self.calls.append(("press_keycode", code))
        return {"ok": True}

    def screenshot(self, save_path=None):
        self.calls.append(("screenshot", save_path))
        return {"ok": True, "path": "p.png"}


def test_dispatch_routes_emulator_tools():
    """M3：设备工具已外置为平级插件，派发走插件层 call_tool（内核零分支）。"""
    fake = _FakeExec()
    bind_execution_module(fake)
    r1 = call_tool("get_ui_tree", {})
    r2 = call_tool("tap_by_id", {"resource_id": "com.x:id/y"})
    r3 = call_tool("launch_app", {"package": "com.calc"})
    r4 = call_tool("press_keycode", {"code": 4})
    r5 = call_tool("screenshot", {})
    assert r1 == {"ok": True, "ui_tree": "<node/>"}
    assert r2 == {"ok": True}
    assert r3 == {"ok": True}
    assert r4 == {"ok": True}
    assert r5 == {"ok": True, "path": "p.png"}
    assert fake.calls == [
        ("get_ui_tree",),
        ("tap_by_id", "com.x:id/y"),
        ("launch_app", "com.calc"),
        ("press_keycode", 4),
        ("screenshot", None),
    ]


def test_dispatch_observe_via_exec():
    fake = _FakeExec()
    bind_execution_module(fake)
    res = call_tool("observe", {})
    assert res == {"active_window": "x", "ocr_text": ["1"]}
