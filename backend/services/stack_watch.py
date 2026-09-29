"""栈看门狗：定期把「进程 RSS + 全部线程栈」写盘。

为什么需要：后端出过一次「单任务把内存顶到 4GB 后整进程卡死」——
此时 HTTP 全挂、诊断端点也访问不了，无法判断卡在哪个调用。
本模块用一条**独立守护线程**周期性落盘快照，即使事件循环被阻塞
（GIL 被工作线程占住）也大概率能留下最后一刻的栈。

设计约束：
- 纯观测：不修改任何业务状态，不持有业务对象引用（只临时取 frame）。
- 极低开销：默认 20s 一次，format_stack 只遍历当前调用链。
- 文件轮换：只保留最近 KEEP 份快照，绝不无限增长。
- 可关闭：环境变量 OMNI_STACK_WATCH=0 或间隔设为 0。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

_KEEP = 5           # 保留的快照份数（轮换）
_DEFAULT_INTERVAL = 20.0
_LOG_DIR = os.path.expanduser(os.path.join("~", ".omniagent", "logs"))

_watch_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _rss_mb() -> Optional[float]:
    try:
        import psutil
        return round(psutil.Process(os.getpid()).memory_info().rss / 1048576, 1)
    except Exception:
        return None


def _thread_names() -> Dict[int, str]:
    try:
        return {t.ident: t.name for t in threading.enumerate() if t.ident is not None}
    except Exception:
        return {}


def _snapshot() -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines: List[str] = [f"=== {ts} rss={_rss_mb()}MB threads={threading.active_count()} ==="]
    names = _thread_names()
    frames = sys._current_frames()
    for ident, frame in frames.items():
        lines.append(f"--- thread {ident} ({names.get(ident, '?')}) ---")
        try:
            lines.extend(x.rstrip() for x in traceback.format_stack(frame))
        except Exception as e:  # 单个线程的栈格式化失败不应中断整体快照
            lines.append(f"<format_stack failed: {e}>")
    return "\n".join(lines) + "\n"


def _rotate_write(text: str) -> None:
    """写入最新快照并轮换，只保留最近 _KEEP 份。"""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        cur = os.path.join(_LOG_DIR, "stackwatch.log")
        # 旧快照依次后移：.log -> .1.log -> .2.log ...
        for i in range(_KEEP - 1, 0, -1):
            src = cur if i == 1 else os.path.join(_LOG_DIR, f"stackwatch.{i - 1}.log")
            dst = os.path.join(_LOG_DIR, f"stackwatch.{i}.log")
            if os.path.exists(src):
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(src, dst)
                except Exception:
                    pass
        with open(cur, "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except Exception:
        pass


def _loop(interval: float) -> None:
    while not _stop.wait(interval):
        try:
            _rotate_write(_snapshot())
        except Exception:
            pass


def start_stack_watch(interval: Optional[float] = None) -> bool:
    """启动看门狗线程（幂等，重复调用只启动一次）。返回是否在运行。"""
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return True
    env = os.environ.get("OMNI_STACK_WATCH", "").strip()
    if interval is None:
        if env in ("0", "off", "false"):
            return False
        interval = float(env) if env.replace(".", "", 1).isdigit() else _DEFAULT_INTERVAL
    if interval <= 0:
        return False
    _stop.clear()
    _watch_thread = threading.Thread(
        target=_loop, args=(interval,), name="omni-stack-watch", daemon=True
    )
    _watch_thread.start()
    return True


def stop_stack_watch() -> None:
    _stop.set()
