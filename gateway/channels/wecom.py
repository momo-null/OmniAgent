"""企微通道 stub —— 通用化验收件（接入设计 v1.1 §4/§8）。

存在的意义：证明「新增通道 = 只写本文件 + 一段配置，dispatch/插件/调度器零改动」。
P1 实装时替换 send/connect 的 TODO（回调URL模式或群机器人 webhook），
协议面（InboundMessage/ReplyChannel/ChannelAdapter）不得为此改动——
若必须改协议才能接入，说明抽象是假的，回炉。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from .base import InboundMessage, OnMessage, ReplyChannel


class WecomReply:
    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.channel_name = "wecom"

    def send_text(self, text: str) -> Dict[str, Any]:
        return {"ok": False, "error": "wecom 通道 P1 未实装"}  # TODO(P1): 企微 API

    def send_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "error": "wecom 通道 P1 未实装"}


class WecomAdapter:
    name = "wecom"
    default_chat_id = ""

    def connect(self, on_message: OnMessage) -> None:
        # TODO(P1): 企微回调 URL / 群机器人轮询 → InboundMessage → on_message
        threading.Thread(target=lambda: None, daemon=True).start()

    def reply_to(self, chat_id: str) -> ReplyChannel:
        return WecomReply(chat_id)

    def broadcast(self, text: str) -> Dict[str, Any]:
        return {"ok": False, "error": "wecom 通道 P1 未实装"}

    def close(self) -> None:
        pass


def build(cfg: Dict[str, Any]) -> Optional[WecomAdapter]:
    if not cfg.get("enabled", False):
        return None
    return WecomAdapter()
