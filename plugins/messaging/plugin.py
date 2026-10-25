"""通用消息发送插件（接入设计 v1.1 §2 工具侧）。

- 工具按**行为**命名（msg_send_text / msg_send_card），通道只是参数——
  接十个通道工具清单不变（通用化红线）。
- 通道实现来自 gateway/channels 注册表（与网关同进程闭环）：
  · channel 缺省 = 当前任务来源通道（dispatch 注入的线程上下文）
  · target  缺省 = 来源会话 / 通道默认会话
- 网关未运行时（如本地 CLI 跑 run_agent_autonomous），工具返回明确的
  {"ok": false, "error": "无可用通道"}——不猜、不静默。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from omni_core.tools.base import function_tool

#: 本插件自有配置（~/.omniagent/plugins/messaging.yaml），网关未跑时的缺省通道
_CFG: Dict[str, Any] = {"default_channel": ""}


def configure(cfg: Optional[dict]) -> None:
    cfg = cfg or {}
    for key in ("default_channel",):
        if key in cfg:
            _CFG[key] = cfg[key]


def _resolve(channel: str, target: str):
    """解析 (adapter, chat_id)。返回 (adapter, chat_id, err)。"""
    from gateway.channels import context

    cur = context.get_current_reply()
    # 通道名：显式 > 当前来源 > 插件配置缺省
    ch_name = channel or ""
    if not ch_name and cur is not None:
        ch_name = getattr(cur, "channel_name", "") or ""
    if not ch_name:
        ch_name = _CFG.get("default_channel", "")
    adapter = context.get_adapter(ch_name) if ch_name else None
    if adapter is None:
        return None, None, f"无可用通道（channel={ch_name or '未指定'}；网关未运行或通道未启用）"

    # 目标：显式 > 当前来源会话
    chat_id = target or ""
    if not chat_id and cur is not None:
        chat_id = getattr(cur, "chat_id", "") or ""
    if not chat_id:
        chat_id = getattr(adapter, "default_chat_id", "") or ""
    if not chat_id:
        return adapter, None, "未指定 target 且无来源会话/默认会话"
    return adapter, chat_id, ""


@function_tool(
    description="发送纯文本消息到消息通道（飞书/企微等）。channel/target 不填时发回本任务来源会话。",
    unit="messaging",
)
def msg_send_text(text: str, channel: str = "", target: str = "") -> Dict[str, Any]:
    """发纯文本。

    Args:
        text: 文本内容。
        channel: 通道名（feishu/wecom…，缺省=来源通道）。
        target: 会话 ID（缺省=来源会话/通道默认会话）。
    """
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    try:
        reply = adapter.reply_to(chat_id)
        return reply.send_text(text)
    except Exception as e:
        return {"ok": False, "error": f"发送失败: {e}"}


@function_tool(
    description="发送原生卡片消息（交互卡片）。payload 为通道原生卡片结构（飞书=新版卡片 schema 直接作为 content）。",
    unit="messaging",
)
def msg_send_card(payload: Dict[str, Any], channel: str = "", target: str = "") -> Dict[str, Any]:
    """发原生卡片。

    Args:
        payload: 通道原生卡片结构（飞书新版卡片 JSON 直接放这里，勿包 {"card": ...} 外层——230099 坑）。
        channel: 通道名（缺省=来源通道）。
        target: 会话 ID（缺省=来源会话/通道默认会话）。
    """
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    try:
        reply = adapter.reply_to(chat_id)
        return reply.send_card(payload)
    except Exception as e:
        return {"ok": False, "error": f"发送失败: {e}"}
