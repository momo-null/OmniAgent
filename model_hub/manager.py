"""模型生命周期管理器（兼容壳层）。

ModelManager 的实现已按职责拆分到 model_hub 的四个 Mixin 模块
（launcher / scanner / serving / processes）；本模块保留 ModelManager
聚合类与原始 ``__init__``，并 re-export 原模块的全部模块级名字，
保证既有 ``from model_hub.manager import ModelManager`` 引用零改动。
"""
import os
import json
import time
import signal
import socket
import subprocess
import urllib.request
import urllib.error
from typing import Dict, Any, List, Optional

import yaml
import psutil

from utils import get_logger
import config
from model_hub.meta import ModelMetaData

from model_hub.launcher import LauncherMixin
from model_hub.scanner import ScanMixin
from model_hub.serving import ServeMixin
from model_hub.health import HealthMixin
from model_hub.processes import ProcessMixin

logger = get_logger("model_manager")

# 默认模型目录（前端可覆盖传参）
DEFAULT_MODELS_DIR = r"D:\AI\Models"


class ModelManager(LauncherMixin, ScanMixin, ServeMixin, HealthMixin, ProcessMixin):
    """GGUF 模型生命周期管理（本地模型管理链路）"""

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "config.yaml"
            )
        self.config_path = config_path
        self.config = self._load_config()
        self.llama_root = self._resolve_path(
            self.config.get("llm_runtime", {}).get("llama_root", "./external/llama")
        )
        # 模型目录（默认 D:\AI\Models；扫描时调用方可覆盖）
        self.models_dir = DEFAULT_MODELS_DIR
        # 启动预设（profile）：从 config.yaml 的 model_presets 读取
        self.presets = self.config.get("model_presets", {}) or {}
        # 运行时状态: name -> {pid, port, process, started_at, out_log, err_log, params}
        self.processes: Dict[str, Dict[str, Any]] = {}


__all__ = ["ModelManager", "logger", "DEFAULT_MODELS_DIR"]
