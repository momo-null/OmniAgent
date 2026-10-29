"""通道注册表 + 当前任务回发上下文（进程内共享）。

- 网关启动时把各适配器实例注册进 REGISTRY（name → adapter）；
- dispatch 在跑任务前 set_current(reply) —— plugins/messaging 的 msg_send_* 工具
  据此实现"缺省发回来源会话"；
- 同进程内闭环，零 IPC。
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from .base import ReplyChannel

_REGISTRY: Dict[str, "object"] = {}   # name -> ChannelAdapter
# 当前任务的回发句柄：**进程级**（不是线程级）。
# 根因（2026-10-29 实测）：SDK 的工具调用跑在 async_bridge 的常驻事件循环线程上，
# 而 dispatch 在 worker 线程 set —— 线程本地键读写不在同一线程，msg_send_text 拿到
# None 后报「无可用通道（channel=未指定）」，消息发不出去。
# 网关只有一条串行 dispatch worker（同一时刻只有一个任务在跑），进程级单值是安全的。
_CURRENT_REPLY: Optional[ReplyChannel] = None
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
    """dispatch 在任务开始时调用；任务结束置 None。

    进程级单值（不按线程隔离）：工具执行在常驻事件循环线程上，线程本地会读不到。
    网关的 dispatch 是单 worker 串行执行，不存在两个任务同时需要不同句柄的情况。
    """
    global _CURRENT_REPLY
    with _LOCK:
        _CURRENT_REPLY = reply


def get_current_reply() -> Optional[ReplyChannel]:
    with _LOCK:
        return _CURRENT_REPLY


# ── 本轮来源是否「外部可达」────────────────────────────────────────────────
# 外部可达 = tools_enabled=False 的通道（QQ 群/陌生人私聊、任何他人能给账号发消息的
# 入口）。发送类工具据此**锁死**目标：只准发回来源会话，忽略模型给的 channel/target。
# 为什么必须锁：否则群里的人可以让它 `msg_send_text(channel="feishu", target=...)`，
# 消息从别的通道发出去——绕过工具白名单做跨通道/跨会话外发（社会工程学跳板）。
_SOURCE_EXTERNAL: bool = False


def set_source_external(enabled: bool) -> None:
    """dispatch 在任务开始时调用：本轮输入来自外部可达通道。"""
    global _SOURCE_EXTERNAL
    with _LOCK:
        _SOURCE_EXTERNAL = bool(enabled)


def get_source_external() -> bool:
    with _LOCK:
        return _SOURCE_EXTERNAL


# ── 本轮「实际发出」的文本 ─────────────────────────────────────────────────
# 发送成功的唯一事实来源：由 msg_send_* 工具在拿到 ok 返回时记账。
# 为什么不让 dispatch 事后去解析工具结果字符串：工具结果经 SDK 序列化后可能是
# Python repr（单引号），JSON 解析会失败——实测就因此把"已发送"记成了 0 条。
_SENT: List[str] = []


def reset_sent() -> None:
    """dispatch 在每轮开始时清空（防止上一轮/其它任务残留）。"""
    global _SENT
    with _LOCK:
        _SENT = []


def note_sent(text: str) -> None:
    """发送工具成功后调用：记下真正发出去的文本。"""
    t = str(text or "").strip()
    if not t:
        return
    with _LOCK:
        _SENT.append(t)


def take_sent() -> List[str]:
    """取走本轮发出的文本并清空。"""
    global _SENT
    with _LOCK:
        out = list(_SENT)
        _SENT = []
        return out
