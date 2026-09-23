"""LLM 运行时适配层

支持多种推理后端：
- LlamaRuntime: llama-completion CLI 单次推理
- ApiRuntime: OpenAI 兼容外部 API
- LlamaServerRuntime: llama-server 常驻服务模式

注意：模型生命周期管理（ModelManager）已迁移至独立的 ``model_hub`` 模块，
本包仅保留推理后端。
"""
from .base import LLMRuntime
from .llama_backend import LlamaRuntime
from .api_backend import ApiRuntime
from .server_backend import LlamaServerRuntime

__all__ = [
    "LLMRuntime",
    "LlamaRuntime",
    "ApiRuntime",
    "LlamaServerRuntime",
]
