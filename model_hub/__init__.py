"""模型管理模块

独立管理本地 GGUF 模型的生命周期（启停、健康检查、进程追踪、扫描、
侧注持久化）。与 web 前端、agent 核心解耦。

模型发现基于扫描目录 + 侧注文件（.meta.json）。
"""
from .manager import ModelManager

__all__ = [
    "ModelManager",
]
