"""兼容 shim：协议已迁入 ``gateway.events.protocol``（阶段 1）。

阶段 6 删除本文件；新代码请 ``from gateway.events.protocol import ...``。
"""
from gateway.events.protocol import (  # noqa: F401
    ChannelAdapter,
    EventSource,
    InboundMessage,
    OnMessage,
    ReplyChannel,
)

__all__ = [
    "OnMessage",
    "InboundMessage",
    "ReplyChannel",
    "ChannelAdapter",
    "EventSource",
]
