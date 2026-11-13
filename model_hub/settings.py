"""``~/.omniagent/model_hub.yaml`` —— 本地服务的开关与启动描述（资源层）。

与 core 配置的分工是**正交的两层**，谁也不读对方的文件：

- 本文件回答「这个服务**起不起**、**怎么起**」（进程/端口/健康/日志）；
- core 的 ``config.yaml``（如 ``runtime.knowledge.memory.vector``）回答
  「core **用不用**它」（业务开关 + 调用地址）。

所以同一个能力可能「起着但没人用」（资源层开、业务层关），Web 模型页需要把
这种状态显示出来，而不是让用户以为它在生效。

描述里支持两个占位符，避免把本机绝对路径写进配置文件：
``{repo}`` = 仓库根；``{models}`` = ``runtime_paths.models_root()``。
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict

from utils import get_logger

logger = get_logger("model_hub.settings")

_SETTINGS_NAME = "model_hub.yaml"

#: 出厂描述：三项都是「python 解释器 + 参数」的同一形状。
#: ``python`` 留空 = 用当前进程的解释器（语音引擎跑在项目 .venv 里）。
#: ``args`` 里的路径用占位符；``port`` 与 ``health`` 供探活和 Web 展示。
DEFAULT_SERVICES: Dict[str, Dict[str, Any]] = {
    "embed-svc": {
        "enabled": True,
        "autostart": True,
        "python": "{models}/embed-svc/venv/Scripts/python.exe",
        "args": [
            "-m", "model_hub.servers.embed_server",
            "--model-dir", "{models}/bge-m3",
            "--port", "18743",
            "--device", "auto",
        ],
        "cwd": "{repo}",
        "port": 18743,
        "health": "/health",
    },
    "voice": {
        "enabled": True,
        "autostart": True,
        "python": "",
        "args": ["-m", "model_hub.servers.voice_engine", "--port", "18742"],
        "cwd": "{repo}",
        "port": 18742,
        "health": "/healthz",
    },
    "llm": {
        "enabled": True,
        "autostart": False,
        "kind": "gguf",
        "models_dir": "D:\\AI\\Models",
    },
}


def _root() -> str:
    try:
        import omni_core.local.runtime_paths as rp

        return str(rp.global_omni())
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".omniagent")


def path() -> str:
    """``~/.omniagent/model_hub.yaml`` 的绝对路径。"""
    return os.path.join(_root(), _SETTINGS_NAME)


def _read_yaml(p: str) -> Dict[str, Any]:
    try:
        import yaml

        with open(p, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        return doc if isinstance(doc, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 —— 描述损坏不阻断启动，回落出厂值
        logger.warning("model_hub.yaml 解析失败（回落出厂值）: %s", e)
        return {}


def load() -> Dict[str, Any]:
    """读服务清单；文件不存在时落一份出厂值（幂等）。"""
    p = path()
    doc = _read_yaml(p)
    if not doc:
        doc = {"services": copy.deepcopy(DEFAULT_SERVICES)}
        try:
            save(doc)
            logger.info("已生成出厂服务清单: %s", p)
        except Exception as e:  # noqa: BLE001
            logger.warning("写 model_hub.yaml 失败（仅内存生效）: %s", e)
    services = doc.get("services")
    if not isinstance(services, dict):
        services = {}
    # 合并出厂项：新增服务不需要用户手动补文件（缺的补上，已有的原样）
    for name, spec in DEFAULT_SERVICES.items():
        if name not in services or not isinstance(services.get(name), dict):
            services[name] = copy.deepcopy(spec)
    doc["services"] = services
    return doc


def save(doc: Dict[str, Any]) -> str:
    """写回服务清单（保留未知字段）。返回文件路径。"""
    import yaml

    p = path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
    return p


def service(name: str) -> Dict[str, Any]:
    """取单个服务的描述（含出厂兜底）；未知服务返回空 dict。"""
    spec = (load().get("services") or {}).get(name)
    return dict(spec) if isinstance(spec, dict) else {}


def dump_json() -> str:
    """便于日志/API 调试。"""
    return json.dumps(load(), ensure_ascii=False)
