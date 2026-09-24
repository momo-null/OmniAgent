"""观测工具（MVP 轻量版）：复用 EasyOCR 引擎做 OCR，纯 CPU、不加载 YOLO。

为什么不复用 PerceptionModule：原 PerceptionModule.__init__ 会同时加载
EasyOCR + YOLO + torch，且 YOLO 模型路径/设备配置可能缺失导致整条链路起不来。
MVP 计算器场景只需要文字识别，故这里独立一个轻量 observer：
- 截屏用 mss（与原模块一致）
- OCR 用 EasyOCR，CPU 模式。注意：本机缓存的识别权重只有 zh_sim_g2.pth
  （英文 english_g2 缺失且下载源被墙），故用 lang=['ch_sim']——中文模型含
  数字/字母识别，计算器数字可正常读出，且零下载。模型目录指向真实缓存 ~/.EasyOCR。
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
import yaml

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # OmniAgent/
_OCR_READER = None
# mss 把设备上下文（srcdc/memdc）存在线程局部里，只在其被初始化的那条线程上有效。
# 后端工具在 worker 线程执行，若全局复用同一个 mss 实例，子线程访问会触发
# AttributeError: '_thread._local' object has no attribute 'srcdc'。
# 故按线程各自缓存一个 mss 实例：每条线程用自己的实例（同线程内初始化+使用）。
_LOCAL = threading.local()


def _model_path() -> str:
    try:
        with open(os.path.join(_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg.get("models", {}).get("easyocr", {}).get("model_path", "./models/easyocr")
    except Exception:
        return "./models/easyocr"


def _get_reader():
    global _OCR_READER
    if _OCR_READER is None:
        import easyocr

        # 本机缓存识别权重只有 zh_sim_g2.pth（english_g2 缺失），故用
        # ['ch_sim']：中文模型含数字/字母识别，且 ~/.EasyOCR/model 下
        # craft+zh_sim_g2 均已缓存、md5 校验通过，零下载。
        # 注意：easyocr 查 model_storage_directory/<filename>，而缓存实际在
        # ~/.EasyOCR/model/ 子目录，故此处必须带 '/model' 一级。
        _OCR_READER = easyocr.Reader(
            ["ch_sim"],
            gpu=False,
            model_storage_directory=os.path.expanduser("~/.EasyOCR/model"),
            verbose=False,
        )
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
