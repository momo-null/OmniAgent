"""``ExecutionModule``：内核持有的"当前环境"句柄。

只委派契约三成员（``kind`` / ``text_of`` / ``verify_done``）；具体环境由
``devices.registry`` 按 ``runtime.backend`` + ``~/.omniagent/environments/<kind>.yaml``
构造（见 ``omni_core/tools/env_loader.py``）。

``platform`` 是**环境自报的展示名**（如 Windows / Android），仅供 system prompt 对齐
语义（如键码），**不属调用面**；内核不硬编码任何平台知识。
"""
from typing import Any, Dict


class ExecutionModule:
    """当前环境的薄句柄（内核只经它使用环境）。"""

    def __init__(self, kind: str, backend: Any, platform: str = ""):
        self.kind = kind
        self.backend = backend
        self.platform = str(platform or "")

    def text_of(self, percept: Dict[str, Any]) -> str:
        return self.backend.text_of(percept)

    def verify_done(self, condition: str, percept: Dict[str, Any]) -> tuple:
        return self.backend.verify_done(condition, percept)
