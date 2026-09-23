"""LLM 运行时抽象接口"""
from typing import Dict, Any


class LLMRuntime:
    """LLM 运行时接口"""

    def generate(self, prompt: str, params: Dict[str, Any]) -> str:
        """生成模型输出"""
        raise NotImplementedError
