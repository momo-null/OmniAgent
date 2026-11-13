"""本地服务的统一加载器（进程层）。

此前同一套「起进程 / 端口占用即视为已运行 / 健康探测 / 日志落盘」被写了三份：
desktop TS 管语音、``omni_core.local.embedder`` 管向量、``model_hub`` 管 GGUF。
本模块把前两者收成一份；GGUF 的 llama-server 仍走 ``manager/launcher``
（它是 llama.cpp argv，症状相同但参数语义不同）。

判定口径（与用户确认的两层语义一致）：
- 本文件只管「起不起」；core 用不用是 ``config.yaml`` 的事。
- 端口已占用 = 已有实例在跑（手动起的 / 上次没退干净），直接跳过而不是抢端口。
- 探活失败**不报错**，只把状态交回给调用方与 Web 页。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from utils import get_logger

from model_hub import settings

logger = get_logger("model_hub.services")

#: 健康探测超时（秒）
HEALTH_TIMEOUT = 2.0


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _models_root() -> str:
    try:
        import omni_core.local.runtime_paths as rp

        return str(rp.models_root())
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".omniagent", "models")


class ServiceManager:
    """服务清单的执行侧：start / status / stop / ensure_started。"""

    def __init__(self, doc: Optional[Dict[str, Any]] = None):
        self.doc = doc or settings.load()
        #: 本进程拉起的子进程（重启后为空，靠端口探测判定「已在运行」）
        self._procs: Dict[str, subprocess.Popen] = {}

    # ── 解析 ──────────────────────────────────────────────
    def names(self) -> List[str]:
        return sorted((self.doc.get("services") or {}).keys())

    def spec(self, name: str) -> Dict[str, Any]:
        s = (self.doc.get("services") or {}).get(name)
        return dict(s) if isinstance(s, dict) else {}

    @staticmethod
    def _sub(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return (value.replace("{repo}", _repo_root())
                     .replace("{models}", _models_root()))

    def _command(self, spec: Dict[str, Any]) -> List[str]:
        py = self._sub(str(spec.get("python") or "")) or sys.executable
        args = spec.get("args") or []
        if isinstance(args, str):
            args = args.split()
        return [py] + [str(self._sub(a)) for a in args]

    def _cwd(self, spec: Dict[str, Any]) -> str:
        cwd = str(self._sub(spec.get("cwd") or "{repo}"))
        return cwd if os.path.isdir(cwd) else _repo_root()

    def _port(self, spec: Dict[str, Any]) -> int:
        try:
            return int(spec.get("port") or 0)
        except Exception:
            return 0

    def _url(self, spec: Dict[str, Any]) -> str:
        return f"http://127.0.0.1:{self._port(spec)}"

    def _log_path(self, name: str) -> str:
        d = os.path.join(_models_root(), name)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "svc.log")

    # ── 探测 ──────────────────────────────────────────────
    def port_open(self, port: int, timeout: float = 0.6) -> bool:
        if port <= 0:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout):
                return True
        except OSError:
            return False

    def healthy(self, name: str, timeout: float = HEALTH_TIMEOUT) -> bool:
        """HTTP 健康检查；未配 health 时退化为端口探测。"""
        from urllib.request import urlopen

        spec = self.spec(name)
        path = str(spec.get("health") or "").strip()
        if not path:
            return self.port_open(self._port(spec))
        try:
            with urlopen(self._url(spec) + path, timeout=timeout) as r:
                return int(getattr(r, "status", 200)) < 500
        except Exception:
            return False

    def status(self, name: str) -> Dict[str, Any]:
        spec = self.spec(name)
        port = self._port(spec)
        proc = self._procs.get(name)
        alive = bool(proc and proc.poll() is None)
        running = alive or self.port_open(port)
        return {
            "name": name,
            "enabled": bool(spec.get("enabled", True)),
            "autostart": bool(spec.get("autostart", False)),
            "running": running,
            "ready": self.healthy(name) if running else False,
            "port": port,
            "pid": (proc.pid if alive else None),
            "kind": str(spec.get("kind") or "python_service"),
        }

    # ── 生命周期 ──────────────────────────────────────────
    def start(self, name: str) -> Dict[str, Any]:
        """按清单启动；端口已占用视为已有实例，不抢端口。"""
        spec = self.spec(name)
        if not spec:
            return {"name": name, "result": "unknown"}
        if not bool(spec.get("enabled", True)):
            return {"name": name, "result": "disabled"}
        if str(spec.get("kind") or "") == "gguf":
            # GGUF 走 ModelManager（llama-server argv 语义不同），不在此重复实现
            return {"name": name, "result": "delegated"}
        port = self._port(spec)
        if self.port_open(port):
            return {"name": name, "result": "already"}
        cmd = self._command(spec)
        if not os.path.isfile(cmd[0]) and cmd[0] != sys.executable:
            logger.warning("%s 解释器不存在: %s", name, cmd[0])
            return {"name": name, "result": "missing_python"}
        flags = 0
        if os.name == "nt":
            # CREATE_NO_WINDOW 强制子进程不分配控制台；DETACHED_PROCESS 恰好相反
            # （脱离父控制台后 console 应用会自己弹一个），两者互斥不能同时给。
            # CREATE_NEW_PROCESS_GROUP 保留，便于整组终止。
            flags = 0x08000000 | 0x00000200
        try:
            log = open(self._log_path(name), "ab")
            proc = subprocess.Popen(
                cmd, cwd=self._cwd(spec), stdout=log, stderr=log,
                close_fds=True, creationflags=flags)
            self._procs[name] = proc
            logger.info("%s 已拉起（pid=%s, port=%s）", name, proc.pid, port)
            return {"name": name, "result": "started", "pid": proc.pid}
        except Exception as e:  # noqa: BLE001
            logger.warning("%s 拉起失败: %s: %s", name, type(e).__name__, e)
            return {"name": name, "result": "error", "error": str(e)}

    def stop(self, name: str) -> Dict[str, Any]:
        """终止本进程拉起的实例；拿不到 pid 时按端口找（best effort）。"""
        proc = self._procs.pop(name, None)
        pid = proc.pid if proc else None
        if pid is None:
            pid = self._pid_on_port(self._port(self.spec(name)))
        if not pid:
            return {"name": name, "result": "not_running"}
        try:
            import psutil

            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
            return {"name": name, "result": "stopped", "pid": pid}
        except Exception as e:  # noqa: BLE001
            return {"name": name, "result": "error", "error": str(e)}

    @staticmethod
    def _pid_on_port(port: int) -> Optional[int]:
        if port <= 0:
            return None
        try:
            import psutil

            for c in psutil.net_connections(kind="inet"):
                if (c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN
                        and c.pid):
                    return int(c.pid)
        except Exception:
            return None
        return None

    def wait_ready(self, name: str, timeout: float = 120.0) -> bool:
        """等探活通过（模型加载可能要几秒到几十秒）。"""
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline:
            if self.healthy(name):
                return True
            time.sleep(1.0)
        return False

    def ensure_started(self, wait: bool = False, timeout: float = 120.0) -> List[Dict[str, Any]]:
        """启动所有 ``autostart`` 且 ``enabled`` 的服务；幂等。"""
        out: List[Dict[str, Any]] = []
        for name in self.names():
            spec = self.spec(name)
            if not (bool(spec.get("enabled", True)) and bool(spec.get("autostart"))):
                continue
            r = self.start(name)
            if wait and r.get("result") in ("started", "already"):
                r["ready"] = self.wait_ready(name, timeout)
            out.append(r)
        return out


__all__ = ["ServiceManager"]
