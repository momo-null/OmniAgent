"""ModelManager 职责拆分 · 运行态查询、健康检测与日志读取（逐字搬迁，零逻辑改动）。"""
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


class HealthMixin:
    """运行态查询、健康检测与日志读取。"""

    def get_active_models(self) -> List[Dict[str, Any]]:
        """返回当前运行中的模型"""
        active = []
        for name, info in self.processes.items():
            alive = self._is_process_alive(info.get("pid"))
            healthy = self._health_check(info["port"])
            active.append({
                "name": name,
                "port": info["port"],
                "pid": info["pid"],
                "started_at": info.get("started_at"),
                "uptime_s": round(time.time() - info.get("started_at", time.time()), 1),
                "process_alive": alive,
                "healthy": healthy,
            })
        return active

    def health_check(self, name: str) -> bool:
        """检查指定模型的 llama-server 健康状态"""
        if name not in self.processes:
            return False
        return self._health_check(self.processes[name]["port"])

    def get_model_logs(self, name: str, lines: int = 50) -> Dict[str, str]:
        """获取模型启动日志"""
        if name not in self.processes:
            return {"out": "", "err": ""}
        info = self.processes[name]
        result = {}
        for key in ("out_log", "err_log"):
            path = info.get(key, "")
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                    result[key.replace("_log", "")] = "".join(all_lines[-lines:])
            else:
                result[key.replace("_log", "")] = ""
        return result

    def tail_logs(self, name: str, prev_seen: int = 0) -> Dict[str, Any]:
        """返回自 prev_seen 行以来的新增日志（供 SSE 增量推送）"""
        logs = self.get_model_logs(name, lines=2000)
        text = (logs.get("out", "") + "\n" + logs.get("err", ""))
        all_lines = text.splitlines()
        new_lines = all_lines[prev_seen:]
        return {"lines": new_lines, "seen": len(all_lines)}

    def get_default_model(
        self, models_dir: Optional[str] = None
    ) -> Optional[str]:
        """获取默认模型名（tags 含 default 的第一个）"""
        models = self.scan_and_build_models(models_dir)
        for m in models:
            if "default" in (m.get("tags") or []):
                return m["name"]
        if models:
            return models[0]["name"]
        return None

    @staticmethod
    def _health_check(port: int) -> bool:
        """检查 llama-server /health 端点"""
        try:
            url = f"http://127.0.0.1:{port}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("status") == "ok"
        except Exception:
            return False

    def _wait_health_check(
        self, port: int, timeout: int = 180, proc: Any = None
    ) -> bool:
        """轮询等待 llama-server 就绪

        9B Q4 + 大 ctx(32768) warmup 易超 90s（旧默认致健康检查超时→进程被
        validate_model 的 finally:stop_model 杀掉，err 日志停在 warming up），
        放宽到 180s。若仍不够可经 start_model/validate_model 的 timeout 参数继续调大。

        ``proc``：传入子进程句柄后，进程**已退出**则立即返回 False——不再空等
        （由 ``serving._wait_ready_or_fail`` 判定 failed 并透出 stderr）。
        """
        start = time.time()
        while time.time() - start < timeout:
            if self._health_check(port):
                return True
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(1)
        return False
