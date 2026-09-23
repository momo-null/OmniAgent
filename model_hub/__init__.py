"""模型管理模块

独立管理本地 GGUF 模型的生命周期（启停、健康检查、进程追踪、扫描、
侧注持久化）。与 web 前端、agent 核心解耦。

注：原 ``registry``（读 config 注册表）已移除（model-meta-refactor, 2026-07-11），
模型发现改为扫描目录 + 侧注文件（.meta.json）。
"""
from .manager import ModelManager

__all__ = [
    "ModelManager",
]
