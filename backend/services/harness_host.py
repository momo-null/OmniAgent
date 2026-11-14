"""网关进程托管（后端所有权，2026-11-14）。

为什么由后端拉起而不是桌面壳：桌面壳退出/重启只影响窗口层，后端才是常驻宿主；
Web 单独跑后端时也应拿到事件源与定时任务，而不是各启动路径各写一份 spawn 逻辑。
网关仍是**独立进程**（它有自己的调度器、通道长连接与相对隔离的崩溃面），
后端只做拉起 / 发现已有实例 / 退出时收树，不做线程内嵌。

幂等：
- 已有 ``harness.channel_gateway`` 进程（无论谁拉起）→ 直接复用，不重复启动；
- 网关自身另有单实例锁（见 ``harness.channel_gateway._acquire_singleton``），
  两处并发拉起时第二个会自行退出，不会出现双调度器 / 双通道。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_PROC: Optional[subprocess.Popen] = None
_LOG_HANDLE = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _log_path() -> Path:
    p = Path.home() / ".omniagent" / "logs" / "harness.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _rotate(path: Path, max_bytes: int = 5 * 1024 * 1024) -> None:
    try:
        if path.is_file() and path.stat().st_size > max_bytes:
            path.replace(path.with_suffix(".old"))
    except Exception:  # 日志轮转失败不影响启动
        pass


def find_running_gateway() -> Optional[int]:
    """已有网关进程的 pid（按命令行识别，不依赖 pid 文件；pid 文件会被并发写坏）。"""
    try:
        import psutil
    except Exception:
        return None
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            parts = p.info.get("cmdline") or []
            # 必须是「python -m harness.channel_gateway」本尊，不能只匹配到脚本里出现的字符串
            if ("-m" in parts and "harness.channel_gateway" in parts
                    and p.is_running() and p.status() != psutil.STATUS_ZOMBIE):
                return int(p.info["pid"])
        except Exception:
            continue
    return None


def start_gateway(backend_port: int = 8000) -> Optional[subprocess.Popen]:
    """启动网关（或复用已有实例）。测试/显式关闭时返回 None。"""
    global _PROC, _LOG_HANDLE
    if os.environ.get("OMNIAGENT_DISABLE_GATEWAY") == "1":
        return None
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    if _PROC is not None and _PROC.poll() is None:
        return _PROC
    existing = find_running_gateway()
    if existing:
        return None

    root = _repo_root()
    log_path = _log_path()
    _rotate(log_path)
    _LOG_HANDLE = open(log_path, "ab", buffering=0)
    env = dict(os.environ)
    env["OMNIAGENT_BACKEND_URL"] = f"http://127.0.0.1:{int(backend_port)}"
    env.setdefault("PYTHONUNBUFFERED", "1")
    import shlex

    extra = shlex.split(os.environ.get("OMNIAGENT_GATEWAY_ARGS") or "")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    _PROC = subprocess.Popen(
        [sys.executable, "-m", "harness.channel_gateway", *extra],
        cwd=str(root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=_LOG_HANDLE,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )
    return _PROC


def stop_gateway() -> None:
    """只停本进程拉起的网关；复用别人的实例时不动它。"""
    global _PROC, _LOG_HANDLE
    proc, _PROC = _PROC, None
    if proc is not None and proc.poll() is None:
        try:
            import psutil

            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                try:
                    child.terminate()
                except Exception:
                    pass
            parent.terminate()
            psutil.wait_procs([parent], timeout=5)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
    if _LOG_HANDLE is not None:
        try:
            _LOG_HANDLE.close()
        except Exception:
            pass
        _LOG_HANDLE = None
