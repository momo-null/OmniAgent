"""ModelManager 职责拆分 · 端口管理、进程启停与孤儿进程回收（逐字搬迁，零逻辑改动）。"""
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import psutil
import yaml

import config
from utils import get_logger
from model_hub.meta import ModelMetaData

logger = get_logger("model_manager")


class ProcessMixin:
    """端口管理、进程启停与孤儿进程回收。"""
    LLAMA_SERVER_EXE = "llama-server.exe"

    @staticmethod
    def _port_free(port: int) -> bool:
        """检测端口是否空闲"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

    def _alloc_port(self, start: int = 8085, end: int = 8185) -> int:
        """分配一个空闲端口（避开已占用与已运行进程端口）"""
        used = {info.get("port") for info in self.processes.values()}
        for p in range(start, end + 1):
            if p in used:
                continue
            if self._port_free(p):
                return p
        raise RuntimeError("没有可用端口（8085-8185 均被占用）")

    def stop_model(self, name: str, timeout: int = 10) -> Dict[str, Any]:
        """停止指定模型（兼容 subprocess.Popen 与已接管 psutil.Process）"""
        if name not in self.processes:
            return {"name": name, "status": "not_running"}

        info = self.processes[name]
        proc = info.get("process")
        pid = info.get("pid")

        if pid and self._is_process_alive(pid):
            logger.info("停止模型 %s (pid=%s)", name, pid)
            self._terminate_process(proc, pid, timeout)

        del self.processes[name]
        logger.info("模型 %s 已停止", name)
        return {"name": name, "status": "stopped"}

    @staticmethod
    def _terminate_process(proc: Any, pid: Optional[int], timeout: int = 10) -> None:
        """统一终止进程：Popen 走 CTRL_BREAK 优雅退出；psutil.Process/仅 pid 走 terminate/kill

        proc 可能为：subprocess.Popen（本后端启动）、psutil.Process（启动接管孤儿）、
        None（仅有 pid）。分别适配 Windows 下的优雅退出与强制终止。
        """
        # 1) 本后端启动的 Popen 子进程：CTRL_BREAK_EVENT 优雅退出
        if isinstance(proc, subprocess.Popen):
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, AttributeError):
                pass
            try:
                proc.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                logger.warning("模型(pid=%s) 优雅停止超时，强制终止", pid)
            except Exception:
                pass
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            return

        # 2) 已接管孤儿（psutil.Process）或仅 pid
        target = proc if isinstance(proc, psutil.Process) else None
        if target is None and pid:
            try:
                target = psutil.Process(pid)
            except Exception:
                target = None
        if target is None:
            logger.warning("无法定位进程(pid=%s) 以停止，跳过", pid)
            return
        try:
            target.terminate()
            target.wait(timeout=timeout)
        except Exception:
            try:
                target.kill()
                target.wait(timeout=5)
            except Exception:
                pass

    def stop_all(self) -> List[Dict[str, Any]]:
        """停止所有运行中的模型"""
        results = []
        for name in list(self.processes.keys()):
            results.append(self.stop_model(name))
        return results

    def _iter_llama_server_procs(self):
        """枚举系统中所有 llama-server.exe 进程（生成器）"""
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if (p.info.get("name") or "").lower() == self.LLAMA_SERVER_EXE:
                    yield p
            except Exception:
                continue

    @staticmethod
    def _proc_listen_port(proc: "psutil.Process") -> Optional[int]:
        """取进程监听的 TCP 端口，失败/无监听返回 None"""
        try:
            for c in proc.net_connections(kind="inet"):
                if c.status == "LISTEN" and c.laddr:
                    return int(c.laddr.port)
        except Exception:
            return None
        return None

    def detect_and_adopt_orphans(self) -> List[Dict[str, Any]]:
        """B：后端启动时探测遗留 llama-server 孤儿并接管

        将后端未跟踪、但真实在跑的 llama-server 接管进 self.processes，
        使其可被正常停止与一键释放管理。不自动杀掉（停止以用户操作为准），
        仅把「后端异常退出后仍在跑」的实例重新纳入管理视野。
        """
        tracked_pids = {info.get("pid") for info in self.processes.values()}
        adopted: List[Dict[str, Any]] = []
        for proc in self._iter_llama_server_procs():
            pid = proc.info.get("pid")
            if pid in tracked_pids:
                continue
            port = self._proc_listen_port(proc)
            if not port or not self._health_check(port):
                logger.warning(
                    "发现未跟踪 llama-server(pid=%s) 但端口不可用，跳过接管", pid
                )
                continue
            base = self._fetch_model_id(port) or f"orphan-{pid}"
            name = base
            n = 1
            while name in self.processes:
                name = f"{base}-{n}"
                n += 1
            self.processes[name] = {
                "pid": pid,
                "port": port,
                "process": proc,
                "started_at": time.time(),
                "out_log": "",
                "err_log": "",
                "params": {"gguf_path": "", "adopted": True},
                "adopted": True,
            }
            adopted.append({"name": name, "pid": pid, "port": port})
            logger.info("接管孤儿 llama-server: %s (pid=%s, port=%d)", name, pid, port)
        return adopted

    def kill_orphans(self) -> List[Dict[str, Any]]:
        """C：强制释放所有 llama-server 进程（已跟踪 + 系统级兜底）

        1) 先停掉本后端跟踪的全部实例（含启动接管的孤儿）；
        2) 再系统级扫一遍 llama-server.exe 兜底强杀，覆盖「后端崩溃遗留、
           且本次启动因无端口/无健康而未被接管」的孤儿。
        返回被杀清单。注意：会杀掉机器上所有 llama-server.exe（含其他会话）。
        """
        killed: List[Dict[str, Any]] = []
        for name in list(self.processes.keys()):
            try:
                self.stop_model(name)
                killed.append({"name": name, "source": "tracked"})
            except Exception as e:
                logger.warning("停止跟踪实例 %s 失败: %s", name, e)
        for proc in self._iter_llama_server_procs():
            pid = proc.info.get("pid")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                killed.append({"pid": pid, "source": "process_scan"})
            except Exception as e:
                logger.warning("终止 llama-server(pid=%s) 失败: %s", pid, e)
        return killed

    @staticmethod
    def _is_process_alive(pid: Optional[int]) -> bool:
        """检查进程是否还在运行"""
        if pid is None:
            return False
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x100000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
