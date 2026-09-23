"""Prompt引擎模块

负责动态加载和生成不同场景的System Prompt，支持模板变量替换。
"""
import os
from typing import Dict, Optional


class PromptEngine:
    """Prompt引擎类"""

    def __init__(self, prompt_dir: Optional[str] = None):
        """初始化Prompt引擎

        Args:
            prompt_dir: Prompt模板文件目录，默认使用项目根目录下的prompts文件夹
        """
        if prompt_dir is None:
            self.prompt_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "prompts"
            )
        else:
            self.prompt_dir = prompt_dir

        self._template_cache: Dict[str, str] = {}

    def load_template(self, template_name: str) -> str:
        """加载Prompt模板文件

        Args:
            template_name: 模板文件名（包含后缀）

        Returns:
            模板内容字符串

        Raises:
            FileNotFoundError: 模板文件不存在时抛出
        """
        if template_name in self._template_cache:
            return self._template_cache[template_name]

        template_path = os.path.join(self.prompt_dir, template_name)
        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Prompt模板文件不存在: {template_path}")

        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()

        self._template_cache[template_name] = template_content
        return template_content

    def build_system_prompt(self, template_name: str, **kwargs) -> str:
        """构建System Prompt，支持变量替换

        Args:
            template_name: 模板文件名
            **kwargs: 模板变量键值对

        Returns:
            替换变量后的完整Prompt字符串
        """
        template = self.load_template(template_name)
        return template.format(**kwargs)
