"""观测工具（MVP 轻量版）：OCR 用 RapidOCR（ONNXRuntime，CPU），纯 CPU、不加载 YOLO。

为什么不复用 PerceptionModule：原 PerceptionModule.__init__ 会同时加载
EasyOCR + YOLO + torch，且 YOLO 模型路径/设备配置可能缺失导致整条链路起不来。
MVP 计算器场景只需要文字识别，故这里独立一个轻量 observer：
- 截屏用 mss（与原模块一致）
- OCR 用 RapidOCR（utils/ocr.py）。**曾用 EasyOCR：它依赖 torch(cu128)，
  首次 OCR 会把整套 CUDA 运行时拖进后端进程，实测 RSS 130MB → 4.5GB 且不释放**；
  RapidOCR 走 ONNXRuntime，常驻约 200MB，中英文识别良好。
- 活动窗口标题用 pyautogui.getActiveWindow()

模型读取器懒加载（首次 observe 触发），避免 import 期拖慢。
后续 richer 场景（vision=True）可在此叠加本地 VLM 摘要，与现有 mmproj 通道对接。
"""
import os
import threading
import time
import mss
import numpy as np
import pyautogui

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # OmniAgent/
_OCR_READER = None
# mss 把设备上下文（srcdc/memdc）存在线程局部里，只在其被初始化的那条线程上有效。
# 后端工具在 worker 线程执行，若全局复用同一个 mss 实例，子线程访问会触发
# AttributeError: '_thread._local' object has no attribute 'srcdc'。
# 故按线程各自缓存一个 mss 实例：每条线程用自己的实例（同线程内初始化+使用）。
_LOCAL = threading.local()


def _get_reader():
    """OCR 读取器（RapidOCR / ONNXRuntime，见 utils/ocr.py）。"""
    global _OCR_READER
    if _OCR_READER is None:
        from utils.ocr import get_reader
        _OCR_READER = get_reader()
    return _OCR_READER


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


def read_screen_text() -> list:
    """OCR 当前全屏，返回文本字符串列表（阈值过滤）。"""
    img = capture_fullscreen()
    reader = _get_reader()
    out = reader.readtext(img[:, :, ::-1])  # BGR -> RGB
    return [line[1] for line in out if line[2] >= 0.7]


def observe() -> dict:
    """返回屏幕摘要：{active_window, ocr_text}。"""
    t0 = time.time()
    ocr = read_screen_text()
    title = active_window_title()
    return {
        "active_window": title,
        "ocr_text": ocr,
        "cost_ms": int((time.time() - t0) * 1000),
    }
