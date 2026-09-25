"""ModelManager 职责拆分 · 模型配置解析、路径解析与服务启动参数构建（逐字搬迁，零逻辑改动）。"""
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

# 透传参数（``extra_args``）中**禁止**出现的开关：这些由内核掌管，
# 交给用户会破坏系统不变量 —— ``--port`` 会让健康检查与 ``processes[].port``
# 记录错端口；``-m`` / ``-mm`` 会绕开路径定位与投影探测；``--host`` 固定本机。
_RESERVED_ARGS = frozenset({"-m", "--model", "--host", "--port", "-mm", "--mmproj"})


class LauncherMixin:
    """模型配置解析、路径解析与服务启动参数构建。"""

    def _load_config(self) -> Dict[str, Any]:
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.error("配置文件未找到: %s", self.config_path)
            return {}

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.normpath(
            os.path.join(os.path.dirname(self.config_path), path)
        )

    def _get_server_exe(self) -> str:
        exe = os.path.join(self.llama_root, "llama-server.exe")
        if not os.path.exists(exe):
            raise FileNotFoundError(f"llama-server.exe 不存在: {exe}")
        return exe

    def _build_env(self) -> Dict[str, str]:
        """构建子进程环境变量，注入 CUDA 路径（从配置读取）"""
        env = os.environ.copy()
        cuda_path = config.get_cuda_path()
        if cuda_path:
            cuda_bin = os.path.join(cuda_path, "bin")
            cuda_libnvvp = os.path.join(cuda_path, "libnvvp")
            extra = []
            if os.path.isdir(cuda_bin):
                extra.append(cuda_bin)
            if os.path.isdir(cuda_libnvvp):
                extra.append(cuda_libnvvp)
            if extra:
                env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
        return env

    def _resolve_launch_params(
        self,
        gguf_path: str,
        name: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        profile: Optional[str] = None,
    ) -> Dict[str, Any]:
        """解析一次启动所需的完整参数

        合并优先级（后者覆盖前者）：
        1. 代码默认值（threads=8, ctx=8192, gpu=99, port=0, reasoning=0）
        2. .meta.json 侧注（如有）
        3. profile 预设（config.yaml ``model_presets``）
        4. 本次启动传入的 overrides（UI 逐参数覆盖）
        """
        params: Dict[str, Any] = {
            "gguf_path": gguf_path,
            "threads": 8,
            "ctx_size": 8192,
            "gpu_layers": 99,
            "port": 0,  # 0 -> 自动分配
            "reasoning_budget": 0,
            "mmproj_path": None,
            "extra_args": [],    # 透传参数（llama.cpp argv token），见 meta.py
        }
        # 2. 侧注
        meta = ModelMetaData.load_from(gguf_path)
        if meta:
            if meta.threads is not None:
                params["threads"] = meta.threads
            if meta.ctx_size is not None:
                params["ctx_size"] = meta.ctx_size
            if meta.gpu_layers is not None:
                params["gpu_layers"] = meta.gpu_layers
            if meta.port is not None:
                params["port"] = meta.port
            if meta.reasoning_budget is not None:
                params["reasoning_budget"] = meta.reasoning_budget
            if meta.mmproj_path is not None:
                params["mmproj_path"] = meta.mmproj_path
            if meta.extra_args:
                params["extra_args"] = list(meta.extra_args)
        # 投影兜底：侧注未声明时，自动探测 GGUF 同目录的 mmproj（与
        # scan_and_build_models 用同一规则）。此前**只有「列表展示」路径**做了探测，
        # 两条「启动」路径都漏了 ⇒ 前端显示 has_mmproj=true、启动却不带 --mmproj，
        # 模型静默退化成纯文本（视觉工具随之不可用）。
        if not params.get("mmproj_path"):
            _detected = self._auto_detect_mmproj(gguf_path)
            if _detected:
                params["mmproj_path"] = _detected
                logger.info("自动探测到多模态投影: %s", _detected)
        # 3. 预设
        if profile and profile in self.presets:
            for k, v in self.presets[profile].items():
                if k in params and v is not None:
                    params[k] = v
        # 4. overrides
        if overrides:
            for k, v in overrides.items():
                if k in params and v is not None:
                    params[k] = v
        if name:
            params["name"] = name
        return params

    def _build_server_args_from_params(self, params: Dict[str, Any]) -> List[str]:
        """从参数 dict 构建 llama-server 启动参数"""
        exe = self._get_server_exe()
        gguf = params.get("gguf_path", "")
        if not gguf or not os.path.exists(gguf):
            raise FileNotFoundError(f"GGUF 文件不存在: {gguf}")
        args = [
            exe,
            "-m", gguf,
            "-t", str(params.get("threads", 8)),
            "-c", str(params.get("ctx_size", 8192)),
            "--n-gpu-layers", str(params.get("gpu_layers", 99)),
            "--host", "127.0.0.1",
            "--port", str(params.get("port", 8085)),
            "--reasoning-budget", str(params.get("reasoning_budget", 0)),
        ]
        # 投影只有两条路：自动探测（存在则加，见 _resolve_launch_params）或侧注
        # 显式 mmproj_path。**关闭投影** = 侧注把 mmproj_path 写成一个不存在的
        # 路径（自动探测被跳过，日志提示"文件不存在"）。不设 use_mmproj 三态：
        # 它无法"无中生有"、与自动语义冗余，且从未持久化（用户 2026-09-25 定）。
        mmproj = self._resolve_mmproj_path(params.get("mmproj_path"), gguf)
        if mmproj:
            args.extend(["--mmproj", mmproj])
        args.extend(self._extra_args_from_params(params))
        logger.info("llama-server argv: %s", " ".join(args))
        return args

    def _extra_args_from_params(self, params: Dict[str, Any]) -> List[str]:
        """取透传参数并校验（黑名单 + 规模上限）。

        透传是**用户自由**通道：llama.cpp 参数随版本演进，故**不做全量白名单**，
        写错由启动失败 + stderr 透出兜底（见 ``serving._wait_ready_or_fail``）。
        但内核掌管的少数开关不得出现，否则破坏系统不变量（见 ``_RESERVED_ARGS``）。
        """
        raw = params.get("extra_args") or []
        if isinstance(raw, str):           # 宽容：手写侧注可能给空白分隔字符串
            raw = raw.split()
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        for tok in raw:
            tok = str(tok).strip()
            if not tok:
                continue
            flag = tok.split("=")[0]
            if flag in _RESERVED_ARGS:
                raise ValueError(
                    f"透传参数不得包含内核掌管的开关「{flag}」"
                    f"（模型路径 / host / 端口 / mmproj 由内核决定）"
                )
            if len(tok) > 512:
                raise ValueError(f"透传参数过长（>{512} 字符）: {tok[:40]}...")
            out.append(tok)
        if len(out) > 64:
            raise ValueError(f"透传参数过多（{len(out)} 项，上限 64）")
        if out:
            logger.info("透传参数: %s", " ".join(out))
        return out

    def _resolve_mmproj_path(
        self, mmproj_path: Optional[str], gguf_path: str
    ) -> Optional[str]:
        """解析 mmproj 路径：相对路径基于 GGUF 同目录；不存在返回 None"""
        if not mmproj_path:
            return None
        if not os.path.isabs(mmproj_path):
            mmproj_path = os.path.join(os.path.dirname(gguf_path), mmproj_path)
        mmproj_path = os.path.normpath(mmproj_path)
        if os.path.exists(mmproj_path):
            return mmproj_path
        logger.warning("mmproj 文件不存在，忽略: %s", mmproj_path)
        return None

    def _auto_detect_mmproj(self, gguf_path: str) -> Optional[str]:
        """在 GGUF 同目录查找 mmproj 投影文件（文件名含 mmproj，不区分大小写）"""
        d = os.path.dirname(gguf_path)
        if not os.path.isdir(d):
            return None
        for f in os.listdir(d):
            if "mmproj" in f.lower() and f.lower().endswith(".gguf"):
                return os.path.join(d, f)
        return None
