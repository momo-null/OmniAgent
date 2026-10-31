"""通道抽象层（接入设计 v1.1 §2）——三协议先定死，再写实现。

通用化判据：新增企微/钉钉/Telegram = 只写一个适配器文件 + 一段配置，
dispatch / plugins/messaging / scheduler 零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

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
    # 会话展示名（**给人和 UI 看的标题**，如「与「昵称」(1234)的对话」）。
    # 通道自声明：能识别对方身份就填，填不了（飞书/企微等）留空 →
    # dispatch 回退用 objective 当标题。与 text 分离的用意：text 里可能带通道附加的
    # 场合自述（喂模型的上下文），那段不该出现在侧栏标题里。
    title: str = ""
    # 消息原文（不含 channel 附加的场合自述）：写会话历史时用它，
    # 使 Web 端回看会话是"人说的话"而不是括号说明。空 = 回退 text。
    display_text: str = ""
    reply: Optional["ReplyChannel"] = None              # 绑定本消息的回发句柄
    # 能力门（fail-closed）：外部可达的通道默认为 False = 该来源的任务是纯对话，
    # 不具备任何工具能力（他人发消息无法驱动 shell/文件/设备）。通道确认「只有本人
    # 能发消息」（如内网/自用）时才可置 True。网关只读本字段，不感知具体通道。
    allow_tools: bool = False
    # 多模态附件（**只作用于当轮模型输入**）：元素是 dict，目前形如
    # ``{"type": "image", "data_url": "data:image/jpeg;base64,..."}``。
    # 用 dict 而非固定字段：以后加文件 / 音频不必再改内核契约（一次收敛）。
    # 会话记录里仍只存文本投影（如 `[图片]`），不落 base64——否则历史膨胀，
    # 且每轮重建上下文都要重传（每张图约 1k+ tokens）。通道负责下载与尺寸限制。
    attachments: List[Dict[str, str]] = field(default_factory=list)
    # 本条消息来自**语音输入**（转写内容在 text，且保留 `[语音]` 前缀标明媒介）。
    # auto 通道（Web/飞书这类直连）据此让支持语音的 ReplyChannel 语音回发；
    # explicit 通道（QQ 等 IM）不据此改写发送形式——发不发语音由模型调
    # msg_send_voice 自己决定，通道不替它选。
    voice: bool = False
    # 回发模式（通道自声明）：
    # - ``"auto"``（缺省）：本轮 agent 的最终回答**自动**发回本会话（Web/飞书这类
    #   "直接对模型说话"的通道）；
    # - ``"explicit"``：**不自动发**，由模型显式调用 ``msg_send_text`` 才发出
    #   （QQ 这类"替主人在 IM 里跟人互动"的通道：想说话才说，独白只留在任务里）。
    # 纯对话通道（tools_enabled=false）在 explicit 模式下仍会被放行发送工具。
    reply_mode: str = "auto"
    # 触发来源（通道自声明）：``"named"`` = 被点名（@/引用回复/文本点名/直连对话），
    # ``"wave"`` = 群旁听波次升格（无人点名，开口判定放行，见 gateway/group_presence.py）。
    # 网关/日志据此区分「被动的对话」与「主动的插话」。
    origin: str = "named"


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
