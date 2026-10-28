"""插件间能力注册表：插件 ``startup`` 注册、消费方查询，内核不持有任何具体实现。

用途（替代旧的 env 厚句柄）：一个插件想消费另一个插件的能力（如 host 的 observe
想用 OCR 插件的文字识别），经此**按名字符串**查询——内核与双方插件互不 import、
互不知道对方的存在细节：

- 提供方：``register_capability("ocr", fn)``（进程级，重复注册覆盖）；
- 消费方：``get_capability("ocr")``——未注册返回 ``None``，**绝不抛错**（提供方
  未启用 = 能力不存在，消费方自行跳过，这是「可选增强」的契约）；
- 内核零具体 capability 知识：名字只是插件间的字符串约定，内核无任何分支。

北极星校验：这里只有"存 / 取"两个动词，不认识任何具体能力名。
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

_REGISTRY: Dict[str, Callable] = {}


def register_capability(name: str, fn: Callable) -> None:
    """注册一个能力（进程级；同名覆盖）。非可调用对象忽略。"""
    if callable(fn):
        _REGISTRY[str(name or "")] = fn


def get_capability(name: str) -> Optional[Callable]:
    """查询能力；未注册返回 ``None``（不抛错——提供方未启用 = 能力不存在）。"""
    return _REGISTRY.get(str(name or ""))


def unregister_capability(name: str) -> None:
    """注销能力（插件 shutdown 用；不存在则静默）。"""
    _REGISTRY.pop(str(name or ""), None)
