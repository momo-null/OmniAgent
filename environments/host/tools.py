"""host 环境自带的工具面（本机 Windows：pyautogui 键鼠 + mss 截图 + EasyOCR 观测）。

- 属 ``environments/host/``（自包含）；环境装载器激活该环境时 import 本模块并 ``bind(backend)``。
- 名字 / 参数 / 语义按**本机**自然定义（如 ``press("enter")`` / ``click(x, y)``）。
- 去场景化：需要「把 percept 变文本」时统一走 backend.text_of(percept)。
"""
from typing import Any, Dict, Optional

from omni_core.tools.base import function_tool
from utils import get_logger

logger = get_logger("environments.host")

# 本环境后端实例（由环境装载器在激活时注入）
_BACKEND: Optional[Any] = None


def bind(backend: Any) -> None:
    """注入本环境后端实例（激活该环境时调用一次）。"""
    global _BACKEND
    _BACKEND = backend


def _require_em() -> Any:
    if _BACKEND is None:
        raise RuntimeError("host 环境未绑定后端（未激活？）")
    return _BACKEND


def _percept_text(percept: Any) -> str:
    """把后端 percept 文本化：统一走 backend.text_of，不预设任何字段。"""
    em = _require_em()
    tof = getattr(em, "text_of", None)
    if callable(tof):
        try:
            return str(tof(percept or {}) or "")
        except Exception:
            pass
    return str(percept or "")


# ---------------------------------------------------------------------------
# 键盘 / 鼠标
# ---------------------------------------------------------------------------
@function_tool(description="按下并释放单个键（本机键名，如 enter / space / a / ctrl）",
               unit="host", source="env")
def press(key: str) -> Dict[str, Any]:
    """按下并释放单个键。

    Args:
        key: 键名，例如 enter、space、a
    """
    ok = _require_em().execute_keyboard_action("press", {"key": key})
    return {"ok": bool(ok)}


@function_tool(description="按下组合键，keys 为逗号分隔字符串（如 'ctrl,c'）",
               unit="host", source="env")
def hotkey(keys: str) -> Dict[str, Any]:
    """按下组合键。

    Args:
        keys: 逗号分隔的键，例如 'ctrl,c'
    """
    key_list = [k.strip() for k in (keys or "").split(",") if k.strip()] if isinstance(keys, str) else list(keys)
    ok = _require_em().execute_keyboard_action("hotkey", {"keys": key_list})
    return {"ok": bool(ok), "keys": key_list}


@function_tool(name="type", description="在当前焦点处输入一段文本", unit="host", source="env")
def input_text(text: str) -> Dict[str, Any]:
    """输入文本。

    Args:
        text: 要输入的文本
    """
    ok = _require_em().execute_keyboard_action("type", {"text": text})
    return {"ok": bool(ok)}


@function_tool(description="等待若干毫秒，用于等待动画或程序加载", unit="host", source="env")
def wait(ms: int = 500) -> Dict[str, Any]:
    """等待指定毫秒。

    Args:
        ms: 等待的毫秒数，例如 500
    """
    res = _require_em().wait(int(ms))
    return res if isinstance(res, dict) else {"ok": True, "waited_ms": int(ms)}


@function_tool(description="点击归一化坐标处（0~1）", unit="host", source="env")
def click(x: float, y: float) -> Dict[str, Any]:
    """点击归一化坐标。

    Args:
        x: 横坐标 0~1
        y: 纵坐标 0~1
    """
    ok = _require_em().execute_mouse_action("click", {"x": x, "y": y})
    return {"ok": bool(ok)}


@function_tool(description="从一点拖到另一点（归一化坐标）", unit="host", source="env")
def drag(from_x: float, from_y: float, to_x: float, to_y: float) -> Dict[str, Any]:
    """归一化坐标拖拽。

    Args:
        from_x: 起点横坐标 0~1
        from_y: 起点纵坐标 0~1
        to_x: 终点横坐标 0~1
        to_y: 终点纵坐标 0~1
    """
    ok = _require_em().execute_mouse_action(
        "drag", {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y}
    )
    return {"ok": bool(ok)}


# ---------------------------------------------------------------------------
# 感知
# ---------------------------------------------------------------------------
@function_tool(description="取得当前环境状态摘要（活动窗口标题 + 屏幕 OCR 文字）",
               unit="host", source="env", percept="state")
def observe() -> Dict[str, Any]:
    """取得当前环境状态摘要。"""
    return _require_em().observe()


@function_tool(description="重新识别当前屏幕文字（OCR，纯 CPU）",
               unit="host", source="env", percept="state")
def read_screen_text() -> Dict[str, Any]:
    """主动重新识别屏幕文字。"""
    return _require_em().read_screen_text()


@function_tool(description="截取当前画面并返回图像路径，用于需要视觉判断时",
               unit="host", source="env")
def screenshot(save_path: str = "") -> Dict[str, Any]:
    """截取当前画面。

    Args:
        save_path: 可选，截图保存路径；留空则落当前任务临时目录
    """
    return _require_em().screenshot(save_path or None)


# ---------------------------------------------------------------------------
# 通用件（与后端无关）
# ---------------------------------------------------------------------------
@function_tool(description="在画面上用模板图像匹配定位元素：给定小图路径，返回最相似位置的归一化中心坐标与匹配分数",
               unit="host", source="env")
def template_match(template_path: str, screenshot_path: str = "", threshold: float = 0.8) -> Dict[str, Any]:
    """模板图像匹配定位。

    Args:
        template_path: 模板图像路径（要找的小图）
        screenshot_path: 可选，指定截图路径；不填则用当前画面截图
        threshold: 匹配分数阈值 0~1，默认 0.8；低于则判定未找到
    """
    import os

    if not template_path or not os.path.isfile(template_path):
        return {"ok": False, "error": f"template 不存在: {template_path}"}

    shot_path = screenshot_path
    if not shot_path:
        shot = _require_em().screenshot()
        if not isinstance(shot, dict) or not shot.get("ok"):
            return {"ok": False, "error": f"截图失败: {(shot or {}).get('error')}"}
        shot_path = shot.get("path")

    try:
        import cv2
    except Exception as e:
        return {"ok": False, "error": f"cv2 不可用: {type(e).__name__}: {e}"}

    img = cv2.imread(shot_path)
    templ = cv2.imread(template_path)
    if img is None or templ is None:
        return {"ok": False, "error": "截图或模板读取失败"}
    if templ.shape[0] > img.shape[0] or templ.shape[1] > img.shape[1]:
        return {"ok": False, "error": "模板比截图还大"}

    res = cv2.matchTemplate(img, templ, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    h, w = templ.shape[:2]
    cx = (max_loc[0] + w / 2) / img.shape[1]
    cy = (max_loc[1] + h / 2) / img.shape[0]
    found = bool(max_val >= float(threshold))
    return {
        "ok": True,
        "found": found,
        "score": round(float(max_val), 4),
        "center": [round(float(cx), 4), round(float(cy), 4)],
        "threshold": float(threshold),
        # 升级硬触发：未匹配到（低于阈）即视为「无置信」，内核据此升级
        "no_confidence": (not found),
    }


@function_tool(description="轮询等待屏幕上出现指定文字（等加载/弹窗/按钮），出现或超时后返回",
               unit="host", source="env")
def wait_for(text: str, timeout_ms: int = 5000, interval_ms: int = 300) -> Dict[str, Any]:
    """轮询等待指定文字出现。

    Args:
        text: 等待出现的文字
        timeout_ms: 最长等待毫秒，默认 5000
        interval_ms: 轮询间隔毫秒，默认 300
    """
    import time

    timeout_ms = int(timeout_ms)
    interval_ms = max(50, int(interval_ms))
    deadline = time.time() + timeout_ms / 1000.0
    waited = 0
    while True:
        try:
            percept = _require_em().read_screen_text() or {}
            found = text in _percept_text(percept)
        except Exception:
            found = False
        if found:
            return {"ok": True, "found": True, "waited_ms": waited}
        if time.time() >= deadline:
            return {"ok": True, "found": False, "waited_ms": waited, "timeout": True}
        time.sleep(interval_ms / 1000.0)
        waited += interval_ms
