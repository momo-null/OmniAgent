"""通道抽象层（接入设计 v1.1 §2）——三协议先定死，再写实现。

通用化判据：新增企微/钉钉/Telegram = 只写一个适配器文件 + 一段配置，
dispatch / plugins/messaging / scheduler 零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

#: 入站消息统一回调：on_message(InboundMessage) -> None（实现方负责异步/排队策略）
OnMessage = Callable[["InboundMessage"], None]


@dataclass
class InboundMessage:
    """一切通道归一到此。适配器负责把通道原始事件翻译成本结构，不做意图加工。"""

    channel: str                     # "feishu" | "wecom" | ...
    chat_id: str                     # 会话标识（通道内唯一）
    user_id: str                     # 发送者标识
    text: str                        # 纯文本投影（富消息取其文本）
    raw: Dict[str, Any] = field(default_factory=dict)   # 原始事件（审计）
    reply: Optional["ReplyChannel"] = None              # 绑定本消息的回发句柄
    # 能力门（fail-closed）：外部可达的通道默认为 False = 该来源的任务是纯对话，
    # 不具备任何工具能力（他人发消息无法驱动 shell/文件/设备）。通道确认「只有本人
    # 能发消息」（如内网/自用）时才可置 True。网关只读本字段，不感知具体通道。
    allow_tools: bool = False


@runtime_checkable
class ReplyChannel(Protocol):
    """回发协议：dispatch / msg_send_* 工具只依赖此协议，不感知具体通道 API。"""

    def send_text(self, text: str) -> Dict[str, Any]:
        """发纯文本到本句柄绑定的会话。返回 {"ok": bool, "error": str?}。"""
        ...

    def send_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发通道原生卡片（payload 结构由通道定义；飞书=新卡片schema直作content）。"""
        ...


@runtime_checkable
class ChannelAdapter(Protocol):
    """适配器协议：新增通道 = 实现它 + 配置段 channels.<name>。"""

    name: str

    def connect(self, on_message: OnMessage) -> None:
        """建立长连接/轮询并开始接收（可阻塞或自行起线程，由实现决定）。"""
        ...

    def reply_to(self, chat_id: str) -> ReplyChannel:
        """构造指向指定会话的回发句柄。"""
        ...

    def broadcast(self, text: str) -> Dict[str, Any]:
        """发默认会话（cron 结果推送等无来源场景）。"""
        ...

    def close(self) -> None:
        """断开（可选实现，默认忽略）。"""
        ...
