"""ModelManager 职责拆分 · 模型目录扫描、匹配与量化识别（逐字搬迁，零逻辑改动）。"""
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


class ScanMixin:
    """模型目录扫描、匹配与量化识别。"""

    def scan_and_build_models(
        self, models_dir: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """扫描模型目录并合并侧注信息

        递归遍历 ``models_dir``（默认 ``D:\\AI\\Models``），对每个 GGUF
        （排除 mmproj）读取同目录 ``.meta.json``，构建统一的模型信息。

        Args:
            models_dir: 扫描根目录；None 时用 ``self.models_dir``。

        Returns:
            List[Dict]，每个元素含 name / gguf_path / 参数 / 运行态等字段。
        """
        base = models_dir or self.models_dir
        result: List[Dict[str, Any]] = []
        if not os.path.isdir(base):
            logger.warning("模型目录不存在，跳过扫描: %s", base)
            return result

        for root, _dirs, files in os.walk(base):
            for f in files:
                if not f.lower().endswith(".gguf") or "mmproj" in f.lower():
                    continue
                gguf_path = os.path.join(root, f)
                try:
                    size_mb = round(os.path.getsize(gguf_path) / (1024 * 1024), 1)
                except OSError:
                    size_mb = 0.0

                meta = ModelMetaData.load_from(gguf_path)
                mmproj_path = (
                    meta.mmproj_path if (meta and meta.mmproj_path)
                    else self._auto_detect_mmproj(gguf_path)
                )
                mmproj_path = self._resolve_mmproj_path(mmproj_path, gguf_path)

                name = meta.name if (meta and meta.name) else os.path.splitext(f)[0]
                is_running = (
                    name in self.processes
                    and self._is_process_alive(self.processes[name].get("pid"))
                )
                running_port = (
                    self.processes[name]["port"] if is_running else None
                )

                result.append({
                    "name": name,
                    "gguf_path": gguf_path,
                    "description": (meta.description if meta else "") or "",
                    "size_mb": size_mb,
                    "quant": self._guess_quant(f),
                    "gpu_layers": meta.gpu_layers if meta else 99,
                    "ctx_size": meta.ctx_size if meta else 8192,
                    "threads": meta.threads if meta else 8,
                    "port": running_port if running_port else (meta.port if meta else None),
                    "reasoning_budget": meta.reasoning_budget if meta else 0,
                    "tags": (meta.tags if meta else []) or [],
                    "extra_args": list(meta.extra_args) if (meta and meta.extra_args) else [],
                    "mmproj_path": mmproj_path,
                    "has_mmproj": bool(mmproj_path),
                    "has_meta": meta is not None,
                    "status": "running" if is_running else "stopped",
                    "pid": self.processes[name].get("pid") if is_running else None,
                })
        return result

    def list_models(
        self, models_dir: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """列出所有模型（扫描 + 侧注合并结果）"""
        return self.scan_and_build_models(models_dir)

    def scan_models_dir(
        self, models_dir: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """扫描目录（兼容旧接口，返回统一模型信息）"""
        return self.scan_and_build_models(models_dir)

    def _find_model_by_name(
        self, name: str, models_dir: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """在扫描结果中按 name 查找（精确 → 模糊）"""
        models = self.scan_and_build_models(models_dir)
        for m in models:
            if m["name"] == name:
                return m
        low = name.lower()
        for m in models:
            if low in m["name"].lower():
                return m
        return None

    @staticmethod
    def _guess_quant(filename: str) -> str:
        """从文件名猜测量化级别"""
        upper = filename.upper()
        for q in ["Q4_K_M", "Q4_K_S", "Q3_K_M", "Q3_K_S", "Q5_K_M",
                   "Q5_K_S", "Q2_K", "Q8_0", "F16", "BF16", "Q4_0", "Q5_0"]:
            if q in upper:
                return q
        return "unknown"
