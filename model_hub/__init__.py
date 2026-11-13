"""模型管理模块（GGUF 与服务型模型的唯一归属地）。

两件事分开：

- **GGUF 模型**（``ModelManager``）：扫描、启动 llama-server、健康检查、进程追踪、
  侧注文件（``.meta.json``）持久化。
- **服务型模型**（``ServiceManager``）：自己就是一个 HTTP 服务的模型（向量嵌入
  sidecar、语音引擎等），由 ``~/.omniagent/model_hub.yaml`` 描述「起不起、怎么起」。

执行器统一在 ``model_hub/servers/``，以 ``python -m model_hub.servers.<name>`` 运行。

**导出是懒的**（PEP 562）：``python -m model_hub.servers.embed_server`` 会先执行本
文件，若这里顶层 import ``ModelManager`` 就会把 ``psutil`` 拖进只装了 torch 的嵌入
venv，导致启动即崩。懒导出让包导入零重依赖。
"""
from __future__ import annotations

from typing import Any

__all__ = ["ModelManager", "ServiceManager"]


def __getattr__(name: str) -> Any:
    if name == "ModelManager":
        from .manager import ModelManager

        return ModelManager
    if name == "ServiceManager":
        from .services import ServiceManager

        return ServiceManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
