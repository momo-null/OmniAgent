"""设备接入层入口（ExecutionModule）—— M4 起位于顶层 `devices/` 包。

定位（设计 §5.6 / §7-M3、M4）：本文件属于 **L1 能力层**，已从内核 `omni_core/`
物理迁出；内核不再持有任何设备实现，只经 L1 tool 插件层按名调用。

ExecutionModule 本身不是具体控制器，而是**按配置选驱动 + 委派的薄壳**：
- config.runtime.backend = "host"     -> HostBackend（本机 pyautogui + mss + EasyOCR）
- config.runtime.backend = "emulator" -> EmulatorBackend（Android uiautomator2 / ADB）

M4 变更：后端不再声明 `tool_schemas` —— 能力暴露的唯一来源是 agent 外层
tool 插件层（omni_core/tools/device_tool.py），设备层只负责执行，不再反向
向内核注入工具清单。

保留 `execute_decision(decision_dict)` 旧接口以兼容 state_manager 工作流。
"""
import os
import time
import yaml

from utils import get_logger
from devices.base import ExecutionBackend
from devices.host import HostBackend
from devices.emulator import EmulatorBackend


def _load_config() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def create_backend(config: dict) -> ExecutionBackend:
    """按 config.runtime.backend 创建对应设备驱动（默认 host）。"""
    kind = (config.get("runtime") or {}).get("backend", "host")
    if kind == "emulator":
        return EmulatorBackend(config)
    return HostBackend(config)


class ExecutionModule:
    """执行模块：按配置选择的执行后端委托壳。"""

    def __init__(self, config: dict | None = None):
        self.config = config if config is not None else _load_config()
        self.logger = get_logger("execution")
        self.backend = create_backend(self.config)
        self.logger.info(f"执行模块初始化：后端 = {self.backend.name}")

    # --- 后端元信息 ---------------------------------------------------------
    @property
    def backend_kind(self) -> str:
        return self.backend.name

    # --- 委托：坐标 ---------------------------------------------------------
    def normalize_coordinate(self, x: float, y: float):
        return self.backend.normalize_coordinate(x, y)

    # --- 委托：执行原语 -----------------------------------------------------
    def execute_keyboard_action(self, action_type: str, params: dict) -> bool:
        return self.backend.execute_keyboard_action(action_type, params)

    def execute_mouse_action(self, action_type: str, params: dict) -> bool:
        return self.backend.execute_mouse_action(action_type, params)

    def observe(self):
        return self.backend.observe()

    def read_screen_text(self):
        return self.backend.read_screen_text()

    def ocr_screenshot(self):
        """图像 OCR（EasyOCR GPU）：转发到 backend（emulator 支持，host 暂无）。"""
        if hasattr(self.backend, "ocr_screenshot"):
            return self.backend.ocr_screenshot()
        return {"ocr_text": [], "error": "ocr_screenshot 仅 emulator 后端支持"}

    def tap_text(self, text: str):
        """OCR 定位文字并点击（emulator 支持，host 暂无）。"""
        if hasattr(self.backend, "tap_text"):
            return self.backend.tap_text(text)
        return {"ok": False, "error": "tap_text 仅 emulator 后端支持"}

    def tap_text_region(self, text: str, max_cy: float = 0.2, min_cy: float = 0.0):
        """区域限定 OCR 点击（emulator 支持，host 暂无）。用于避开同名底部导航。"""
        if hasattr(self.backend, "tap_text_region"):
            return self.backend.tap_text_region(text, max_cy=max_cy, min_cy=min_cy)
        return {"ok": False, "error": "tap_text_region 仅 emulator 后端支持"}

    def collect_list(self, **kwargs):
        """一次性采集当前列表全部条目（emulator 支持，host 暂无）。"""
        if hasattr(self.backend, "collect_list"):
            return self.backend.collect_list(**kwargs)
        return {"ok": False, "error": "collect_list 仅 emulator 后端支持", "entries": [], "total": 0}

    def screenshot(self, save_path: str | None = None):
        return self.backend.screenshot(save_path)

    def get_ui_tree(self):
        return self.backend.get_ui_tree()

    def tap_by_id(self, resource_id: str):
        return self.backend.tap_by_id(resource_id)

    def launch_app(self, package: str):
        return self.backend.launch_app(package)

    def press_keycode(self, code: int):
        return self.backend.press_keycode(code)

    def wait(self, ms: int):
        return self.backend.wait(ms)

    # --- 兼容旧接口：decision dict -> 后端委派 ------------------------------
    def execute_decision(self, decision: dict) -> dict:
        """执行认知模块输出的决策字典（保留以兼容 state_manager 工作流）。"""
        start_time = time.time()
        action_type = decision.get("action_type", "none")
        action_params = decision.get("action_params", {})
        reason = decision.get("reason", "")

        def _result(success, message, **extra):
            return {
                "timestamp": time.time(),
                "success": success,
                "action_type": action_type,
                "action_params": action_params,
                "reason": reason,
                "message": message,
                "cost_time": time.time() - start_time,
                **extra,
            }

        try:
            if action_type == "none":
                return _result(True, "无操作")
            if action_type in ["click", "double_click", "right_click", "move", "drag"]:
                ok = self.execute_mouse_action(action_type, action_params)
                return _result(ok, "鼠标操作执行成功" if ok else "鼠标操作执行失败")
            if action_type in ["type", "press", "hotkey"]:
                ok = self.execute_keyboard_action(action_type, action_params)
                return _result(ok, "键盘操作执行成功" if ok else "键盘操作执行失败")
            if action_type == "wait":
                self.wait(int(action_params.get("seconds", 1.0) * 1000))
                return _result(True, "等待完成")
            return _result(False, f"未知的动作类型: {action_type}")
        except Exception as e:
            return _result(False, f"执行失败: {str(e)}", error=str(e))
