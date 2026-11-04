"""语音服务配置（**引擎侧**）：``~/.omniagent/voice_service.yaml``。

为什么要和插件配置分开：语音引擎已经是一个独立常驻服务（``engine_service.py``），
它的模型路径、音色、空闲回收、监听端口属于「服务」；插件只剩调用端点的职责
（``~/.omniagent/plugins/voice.yaml``：enabled + base_url + api_key + 模型名）。
混在一份文件里会让「换在线引擎」和「改本机权重路径」变成同一件事，也让服务进程
必须去读插件目录。

读取方：``engine_service``（监听/回收参数）、``plugin`` 的本机引擎段（STT/TTS/SVC）、
``sing_runtime`` / ``song_source``（唱歌引擎与在线曲库闸门）。
写入方：设置页经 ``PUT /api/runtime/tools/plugins/<name>/config?scope=service``
（schema 由 ``plugin.json`` 的 ``service_config`` 自报，路径也由它声明）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml

from utils import get_logger

logger = get_logger("plugins.voice")

_DEFAULT_PATH = "~/.omniagent/voice_service.yaml"


def service_config_path() -> Path:
    """服务配置文件路径（``OMNIAGENT_VOICE_SERVICE_CONFIG`` 可覆盖，测试/多实例用）。"""
    env = os.environ.get("OMNIAGENT_VOICE_SERVICE_CONFIG", "").strip()
    return Path(os.path.expanduser(env or _DEFAULT_PATH))


def service_cfg() -> Dict[str, Any]:
    """读服务配置；文件缺失/非法一律返回空 dict（引擎自己有默认值兜底）。"""
    path = service_config_path()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 —— 配置坏了不该让进程起不来
        logger.warning("语音服务配置读取失败（忽略）: %s", e)
        return {}
    return data if isinstance(data, dict) else {}
