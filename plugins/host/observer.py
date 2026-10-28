"""观测原语（host 插件内）：全屏抓帧 + 活动窗口标题。

- 截屏用 mss（线程局部实例：mss 把设备上下文存在线程局部里，跨线程复用会
  触发 AttributeError，故每条线程各自缓存一个实例）。
- 活动窗口标题用 pyautogui.getActiveWindow()。

OCR **不在这里**：文字识别是可选增强，由 OCR 插件（plugins/ocr）经
capability registry 提供（见 tools.py 的 ``_ocr_screen_lines``）——OCR 插件未
启用时本模块零 ORT 依赖、observe 只返回窗口标题。曾用 EasyOCR 的教训保留在
utils/ocr.py 文档里（torch cu128 会把 RSS 顶到 4.5GB）。
"""
import threading

import mss
import numpy as np
import pyautogui

# mss 把设备上下文（srcdc/memdc）存在线程局部里，只在其被初始化的那条线程上有效。
# 后端工具在 worker 线程执行，若全局复用同一个 mss 实例，子线程访问会触发
# AttributeError: '_thread._local' object has no attribute 'srcdc'。
# 故按线程各自缓存一个 mss 实例：每条线程用自己的实例（同线程内初始化+使用）。
_LOCAL = threading.local()


def _sct():
    sct = getattr(_LOCAL, "sct", None)
    if sct is None:
        sct = mss.mss()
        _LOCAL.sct = sct
    return sct


def capture_fullscreen() -> np.ndarray:
    s = _sct()
    img = np.array(s.grab(s.monitors[0]))[:, :, :3]  # BGRA -> BGR
    return img


def active_window_title() -> str:
    try:
        w = pyautogui.getActiveWindow()
        return w.title if w else ""
    except Exception:
        return ""
