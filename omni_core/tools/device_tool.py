"""设备/通用自研外层 tool 插件（平级，由 LLM 直接调用）。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.6 / §7-M3）：
- Host / Emulator 的设备能力（键盘/鼠标/感知/设备原语）作为 agent 外层 tool 插件，
  与视觉、Python 执行、外部 MCP 完全平级，由 LLM 直接调用。
- 工具定义用 SDK `function_tool`：schema 与参数说明由 SDK 从签名 + docstring 产出。
- 运行时注入：``bind_execution_module(em)`` 在 ToolLoop 构造期调用一次；
  插件函数体不含全局硬编码依赖。

去场景化（§9）：需要「把 percept 变文本」时统一走 backend.text_of(percept)，
不在插件里读具体感知字段。
"""
from typing import Any, Dict, Optional

from omni_core.tools.base import function_tool


# 注入的执行后端委托壳（由 tool_loop 构造期注入）
_EM: Optional[Any] = None


def bind_execution_module(em: Any) -> None:
    """注入执行后端（一次性，tool_loop 构造期调用）。"""
    global _EM
    _EM = em


def _require_em() -> Any:
    if _EM is None:
        raise RuntimeError("执行后端未注入（未绑定 ExecutionModule）")
    return _EM


def _percept_text(percept: Any) -> str:
    """把后端 percept 文本化：统一走 backend.text_of，不预设任何字段。"""
    em = _require_em()
    tof = getattr(getattr(em, "backend", None), "text_of", None)
    if callable(tof):
        try:
            return str(tof(percept or {}) or "")
        except Exception:
            pass
    return str(percept or "")


# ---------------------------------------------------------------------------
# 键盘 / 鼠标
# ---------------------------------------------------------------------------
@function_tool(description="按下并释放单个键，如 enter / space / esc / a / win", group="device")
def press(key: str) -> Dict[str, Any]:
    """按下并释放单个键。

    Args:
        key: 键名，例如 enter、space、win、a
    """
    ok = _require_em().execute_keyboard_action("press", {"key": key})
    return {"ok": bool(ok)}


@function_tool(description="按下组合键，keys 为逗号分隔字符串", group="device")
def hotkey(keys: str) -> Dict[str, Any]:
    """按下组合键。

    Args:
        keys: 逗号分隔的键，例如 'ctrl,c'
    """
    key_list = [k.strip() for k in (keys or "").split(",") if k.strip()] if isinstance(keys, str) else list(keys)
    ok = _require_em().execute_keyboard_action("hotkey", {"keys": key_list})
    return {"ok": bool(ok), "keys": key_list}


@function_tool(name="type", description="在当前焦点处输入一段文本", group="device")
def input_text(text: str) -> Dict[str, Any]:
    """输入文本。

    Args:
        text: 要输入的文本
    """
    ok = _require_em().execute_keyboard_action("type", {"text": text})
    return {"ok": bool(ok)}


@function_tool(description="等待若干毫秒，用于等待动画或程序加载", group="device")
def wait(ms: int = 500) -> Dict[str, Any]:
    """等待指定毫秒。

    Args:
        ms: 等待的毫秒数，例如 500
    """
    res = _require_em().wait(int(ms))
    return res if isinstance(res, dict) else {"ok": True, "waited_ms": int(ms)}


@function_tool(description="点击归一化坐标处", group="device")
def click(x: float, y: float) -> Dict[str, Any]:
    """点击归一化坐标。

    Args:
        x: 横坐标 0~1
        y: 纵坐标 0~1
    """
    ok = _require_em().execute_mouse_action("click", {"x": x, "y": y})
    return {"ok": bool(ok)}


@function_tool(description="从一点拖到另一点（归一化坐标），用于滑动列表、拖动元素等", group="device")
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
@function_tool(description="取得当前环境状态摘要（形状由后端决定：窗口/层级树/终端输出等）", group="device")
def observe() -> Dict[str, Any]:
    """取得当前环境状态摘要。"""
    return _require_em().observe()


@function_tool(description="重新识别当前界面文字（系统控件层级树提取，快；自绘界面可能为空）", group="device")
def read_screen_text() -> Dict[str, Any]:
    """主动重新识别界面文字。"""
    return _require_em().read_screen_text()


@function_tool(description="对当前截图做图像 OCR，识别自绘/自定义 UI 的文字（较慢但更全）", group="device_emulator")
def ocr_screenshot() -> Dict[str, Any]:
    """对当前截图做图像 OCR。"""
    return _require_em().ocr_screenshot()


@function_tool(description="截取当前画面并返回图像路径，用于需要视觉判断时", group="device")
def screenshot(save_path: str = "") -> Dict[str, Any]:
    """截取当前画面。

    Args:
        save_path: 可选，截图保存路径；留空则用默认临时路径
    """
    return _require_em().screenshot(save_path or None)


@function_tool(description="获取当前界面的结构化 UI 层级（控件文本/坐标/resource-id）", group="device_emulator")
def get_ui_tree() -> Dict[str, Any]:
    """获取当前界面的结构化 UI 层级。"""
    return _require_em().get_ui_tree()


# ---------------------------------------------------------------------------
# 设备原语（不支持的后端返回明确错误 dict，不抛异常）
# ---------------------------------------------------------------------------
@function_tool(description="按控件 resource-id 点击（比坐标更可靠）", group="device_emulator")
def tap_by_id(resource_id: str) -> Dict[str, Any]:
    """按控件 id 点击。

    Args:
        resource_id: 控件的 resource-id，如 com.x:id/btn
    """
    return _require_em().tap_by_id(resource_id)


@function_tool(description="定位界面上的指定文字并点击其中心；找不到时返回当前可见文字列表", group="device_emulator")
def tap_text(text: str) -> Dict[str, Any]:
    """定位文字并点击。

    Args:
        text: 要点击的文字，如“确定”（精确匹配优先，支持子串）
    """
    return _require_em().tap_text(text)


@function_tool(description="启动指定包名的应用", group="device_emulator")
def launch_app(package: str) -> Dict[str, Any]:
    """启动应用。

    Args:
        package: 应用包名，如 com.android.calculator2
    """
    return _require_em().launch_app(package)


@function_tool(description="按下设备按键码（Android keycode：3=HOME, 4=BACK, 66=ENTER）", group="device_emulator")
def press_keycode(code: int) -> Dict[str, Any]:
    """按下设备按键码。

    Args:
        code: 按键码整数
    """
    return _require_em().press_keycode(int(code))


@function_tool(description="一次性采集当前列表/页面上所有条目的文本（内部自动翻页去重）", group="device_emulator")
def collect_list(max_pages: int = 40) -> Dict[str, Any]:
    """批量采集当前列表条目。

    Args:
        max_pages: 最多翻页次数，默认 40
    """
    try:
        return _require_em().collect_list(max_pages=int(max_pages))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "entries": [], "total": 0}


# ---------------------------------------------------------------------------
# 通用能力（与设备无关，任意界面可用）
# ---------------------------------------------------------------------------
@function_tool(description="在画面上用模板图像匹配定位元素：给定小图路径，返回最相似位置的归一化中心坐标与匹配分数", group="device")
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


@function_tool(description="轮询等待界面上出现指定文字（等加载/弹窗/按钮），出现或超时后返回", group="device")
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
