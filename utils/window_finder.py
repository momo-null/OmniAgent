"""窗口查找工具

通过进程名与窗口标题关键字定位窗口句柄与坐标。
"""
import ctypes
from ctypes import wintypes
from typing import List, Dict, Any, Optional
import psutil


user32 = ctypes.windll.user32


def _get_window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _get_window_rect(hwnd: int) -> Optional[Dict[str, int]]:
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return {"left": rect.left, "top": rect.top, "width": width, "height": height}


def _get_window_pid(hwnd: int) -> Optional[int]:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value if pid.value else None


def _match_process_name(proc_name: str, target: str) -> bool:
    if not proc_name or not target:
        return False
    proc = proc_name.lower()
    tgt = target.lower()
    if not tgt.endswith(".exe"):
        return proc == tgt or proc == f"{tgt}.exe"
    return proc == tgt


def find_windows_by_process(process_name: str, title_keywords: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """根据进程名与标题关键字查找窗口

    Args:
        process_name: 进程名（可不带 .exe）
        title_keywords: 标题关键字列表，任意一个命中即视为匹配

    Returns:
        窗口信息列表（含 hwnd、title、rect）
    """
    title_keywords = title_keywords or []
    results: List[Dict[str, Any]] = []

    # 预构建 pid -> name 映射，降低枚举成本
    pid_to_name: Dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        pid_to_name[proc.info["pid"]] = proc.info.get("name", "")

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = _get_window_pid(hwnd)
        if pid is None:
            return True
        proc_name = pid_to_name.get(pid, "")
        if not _match_process_name(proc_name, process_name):
            return True
        title = _get_window_text(hwnd)
        if title_keywords:
            title_lower = title.lower()
            if not any(k.lower() in title_lower for k in title_keywords):
                return True
        rect = _get_window_rect(hwnd)
        if rect:
            results.append({
                "hwnd": int(hwnd),
                "title": title,
                "rect": rect
            })
        return True

    user32.EnumWindows(enum_proc, 0)
    return results


def find_windows_by_title(title_keywords: List[str]) -> List[Dict[str, Any]]:
    """根据标题关键字查找窗口

    Args:
        title_keywords: 标题关键字列表

    Returns:
        窗口信息列表（含 hwnd、title、rect）
    """
    results: List[Dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_text(hwnd)
        if not title:
            return True
        title_lower = title.lower()
        if not any(k.lower() in title_lower for k in title_keywords):
            return True
        rect = _get_window_rect(hwnd)
        if rect:
            results.append({
                "hwnd": int(hwnd),
                "title": title,
                "rect": rect
            })
        return True

    user32.EnumWindows(enum_proc, 0)
    return results


def get_primary_window_rect(process_name: str, title_keywords: Optional[List[str]] = None) -> Optional[Dict[str, int]]:
    """获取主窗口的矩形区域

    Args:
        process_name: 进程名（可不带 .exe）
        title_keywords: 标题关键字列表

    Returns:
        窗口矩形字典或 None
    """
    windows = find_windows_by_process(process_name, title_keywords)
    if not windows:
        return None
    # 选择面积最大的窗口作为主窗口
    windows.sort(key=lambda w: w["rect"]["width"] * w["rect"]["height"], reverse=True)
    return windows[0]["rect"]


def list_all_visible_windows() -> List[Dict[str, Any]]:
    """列出所有可见窗口（调试用）

    Returns:
        窗口信息列表，含 title、pid、rect、process_name
    """
    results: List[Dict[str, Any]] = []

    pid_to_name: Dict[int, str] = {}
    for proc in psutil.process_iter(["pid", "name"]):
        pid_to_name[proc.info["pid"]] = proc.info.get("name", "")

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_text(hwnd)
        rect = _get_window_rect(hwnd)
        pid = _get_window_pid(hwnd)
        if rect and rect["width"] > 100 and rect["height"] > 100:
            results.append({
                "hwnd": int(hwnd),
                "title": title,
                "pid": pid,
                "process_name": pid_to_name.get(pid or 0, ""),
                "rect": rect
            })
        return True

    user32.EnumWindows(enum_proc, 0)
    results.sort(key=lambda w: w["rect"]["width"] * w["rect"]["height"], reverse=True)
    return results


def activate_window(process_name: str, title_keywords: Optional[List[str]] = None) -> bool:
    """将匹配的窗口激活到前台

    Args:
        process_name: 进程名（可不带 .exe）
        title_keywords: 标题关键字列表

    Returns:
        是否激活成功
    """
    windows = find_windows_by_process(process_name, title_keywords)
    if not windows:
        # 尝试按标题查找
        if title_keywords:
            windows = find_windows_by_title(title_keywords)
    if not windows:
        return False
    # 选择面积最大的窗口
    windows.sort(key=lambda w: w["rect"]["width"] * w["rect"]["height"], reverse=True)
    hwnd = windows[0]["hwnd"]
    # 先最小化再恢复，确保能激活到前台
    user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True
