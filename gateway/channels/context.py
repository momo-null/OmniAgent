"""通道注册表 + 当前任务回发上下文（进程内共享）。

- 网关启动时把各适配器实例注册进 REGISTRY（name → adapter）；
- dispatch 在跑任务前 set_current(reply) —— plugins/messaging 的 msg_send_* 工具
  据此实现"缺省发回来源会话"；
- 同进程内闭环，零 IPC。
"""
from __future__ import annotations

import threading
from typing import Dict, Optional

from .base import ReplyChannel

_REGISTRY: Dict[str, "object"] = {}   # name -> ChannelAdapter
_CURRENT_REPLY: Dict[int, ReplyChannel] = {}   # thread_id -> 本线程当前任务的回发句柄
_LOCK = threading.Lock()


def register(adapter) -> None:
    with _LOCK:
        _REGISTRY[adapter.name] = adapter


def get_adapter(name: str):
    with _LOCK:
        return _REGISTRY.get(name)


def all_channels() -> list:
    with _LOCK:
        return sorted(_REGISTRY)


def set_current_reply(reply: Optional[ReplyChannel]) -> None:
    """dispatch 在任务开始时调用；任务结束置 None。按线程隔离（串行队列单worker下天然安全）。"""
    _CURRENT_REPLY[threading.get_ident()] = reply


def get_current_reply() -> Optional[ReplyChannel]:
    return _CURRENT_REPLY.get(threading.get_ident())
