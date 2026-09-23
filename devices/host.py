"""HostBackend：本机执行后端（pyautogui + mss + EasyOCR observer）。

把本机控制逻辑按 ExecutionBackend 抽象实现。属 L1 设备能力层，
observe / read_screen_text 复用 devices.observer（轻量 EasyOCR，纯 CPU）。
"""
import os
import time
from typing import Any, Dict, Optional

import pyautogui

from utils import get_logger
from devices.base import ExecutionBackend


class HostBackend(ExecutionBackend):
    name = "host"

    def __init__(self, config: Optional[dict] = None):
        self.logger = get_logger("execution.host")
        self.config = config or {}
        exec_cfg = self.config.get("execution", {})
        self.screen_config = exec_cfg.get("screen_resolution", {"width": 1920, "height": 1080})
        self.screen_width = self.screen_config.get("width", 1920)
        self.screen_height = self.screen_config.get("height", 1080)
        self.coordinate_normalization = exec_cfg.get("coordinate_normalization", True)

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = exec_cfg.get("click_delay", 0.1)
        self.mouse_move_duration = exec_cfg.get("mouse_move_duration", 0.2)
        self.type_interval = exec_cfg.get("type_interval", 0.05)

        # 以实际屏幕分辨率覆盖配置（若不一致仅告警）
        try:
            aw, ah = pyautogui.size()
            if self.screen_width != aw or self.screen_height != ah:
                self.logger.warning(
                    f"配置分辨率({self.screen_width}x{self.screen_height})与实际({aw}x{ah})不一致，已采用实际"
                )
                self.screen_width, self.screen_height = aw, ah
        except Exception as e:
            self.logger.warning(f"读取实际分辨率失败: {e}")
        self.logger.info(f"HostBackend 初始化完成，屏幕: {self.screen_width}x{self.screen_height}")

    # --- 坐标 ---------------------------------------------------------------
    def normalize_coordinate(self, x: float, y: float):
        if self.coordinate_normalization and 0 <= x <= 1 and 0 <= y <= 1:
            return int(x * self.screen_width), int(y * self.screen_height)
        return int(round(x)), int(round(y))

    # --- 键盘 ---------------------------------------------------------------
    def execute_keyboard_action(self, action_type: str, params: Dict[str, Any]) -> bool:
        try:
            if action_type == "type":
                pyautogui.typewrite(params.get("text", ""), interval=self.type_interval)
            elif action_type == "press":
                pyautogui.press(params.get("key", ""))
            elif action_type == "hotkey":
                keys = params.get("keys", [])
                if not isinstance(keys, list):
                    keys = [keys]
                pyautogui.hotkey(*keys)
            else:
                self.logger.warning(f"HostBackend 未知键盘动作: {action_type}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"HostBackend 键盘操作失败: {e}", exc_info=True)
            return False

    # --- 鼠标 ---------------------------------------------------------------
    def execute_mouse_action(self, action_type: str, params: Dict[str, Any]) -> bool:
        try:
            x = params.get("x", 0)
            y = params.get("y", 0)
            abs_x, abs_y = self.normalize_coordinate(x, y)
            button = params.get("button", "left")
            if action_type == "click":
                pyautogui.click(abs_x, abs_y, button=button)
            elif action_type == "double_click":
                pyautogui.doubleClick(abs_x, abs_y, button=button)
            elif action_type == "right_click":
                pyautogui.rightClick(abs_x, abs_y)
            elif action_type == "move":
                pyautogui.moveTo(abs_x, abs_y, duration=self.mouse_move_duration)
            elif action_type == "drag":
                from_x, from_y = params.get("from_x"), params.get("from_y")
                to_x = params.get("to_x", params.get("x_end", 0))
                to_y = params.get("to_y", params.get("y_end", 0))
                if from_x is not None and from_y is not None:
                    fx, fy = self.normalize_coordinate(from_x, from_y)
                    pyautogui.moveTo(fx, fy, duration=self.mouse_move_duration)
                    tx, ty = self.normalize_coordinate(to_x, to_y)
                    pyautogui.dragTo(tx, ty, duration=self.mouse_move_duration, button=button)
                else:
                    tx, ty = self.normalize_coordinate(to_x, to_y)
                    pyautogui.dragTo(tx, ty, duration=self.mouse_move_duration, button=button)
            else:
                self.logger.warning(f"HostBackend 未知鼠标动作: {action_type}")
                return False
            return True
        except Exception as e:
            self.logger.error(f"HostBackend 鼠标操作失败: {e}", exc_info=True)
            return False

    # --- 感知（委托 observer）------------------------------------------------
    def observe(self) -> Dict[str, Any]:
        from devices import observer
        obs = observer.observe()
        return {
            "active_window": obs.get("active_window", ""),
            "ocr_text": obs.get("ocr_text", []),
        }

    def read_screen_text(self) -> Dict[str, Any]:
        from devices import observer
        return {"ocr_text": observer.read_screen_text()}

    # --- 感知文本化 / 完成判定（覆盖基类抽象，屏幕形状） ---------------------
    def text_of(self, percept: Dict[str, Any]) -> str:
        """把 HostBackend 的屏幕 percept 转纯文本（active_window + ocr_text + ui_tree）。"""
        if not isinstance(percept, dict):
            return str(percept)
        parts = []
        if percept.get("active_window"):
            parts.append(f"[窗口] {percept['active_window']}")
        ocr = percept.get("ocr_text")
        if ocr:
            parts.append("[文字] " + " / ".join(ocr if isinstance(ocr, list) else [str(ocr)]))
        if percept.get("ui_tree"):
            parts.append("[UI树] " + str(percept["ui_tree"]))
        return "\n".join(parts)

    def verify_done(self, condition: str, percept: Dict[str, Any]) -> tuple:
        """屏幕完成判定：condition 按 '|' 拆多候选，在 text_of(percept) 中做子串命中。"""
        if not condition:
            return False, "无可校验条件"
        text = self.text_of(percept)
        for c in str(condition).split("|"):
            c = c.strip()
            if c and c in text:
                return True, f"命中条件: {c}"
        return False, f"未找到完成条件: {condition}"

    # --- 截屏（mss）---------------------------------------------------------
    def screenshot(self, save_path: Optional[str] = None) -> Dict[str, Any]:
        import mss
        try:
            save_path = save_path or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "temp", "host_screenshot.png"
            )
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with mss.mss() as sct:
                sct.shot(output=save_path)
            return {"ok": True, "path": save_path}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 注：M4 起本后端不再声明 tool_schemas —— 能力暴露的唯一来源是
    # agent 外层 tool 插件层（omni_core/tools/device_tool.py），
    # 设备层只负责「执行」，不参与「告诉 LLM 有什么能力」（L1↛内核反向依赖）。
