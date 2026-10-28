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

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    if adapter is None and ch_name:
        # 非网关进程（如 Web 后端）里没有注册表实例 → 按 config.channels.<name> 临时
        # 构造一个「只用于发送」的句柄（不建立长连接）。QQ 的发送走 OneBot HTTP，
        # 因此不依赖网关进程；这样 msg_send_text 在任何进程都能一次成功，模型不必
        # 绕去 shell 手撸 curl（实测踩过：绕了 9 步）。
        try:
            from gateway.channel_gateway import build_adapter

            adapter = build_adapter(ch_name)
        except Exception:
            adapter = None
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
    description="发送纯文本消息到消息通道。channel=通道名（如 napcat/feishu）；"
                "target=会话标识，按通道自身的格式：napcat 为 private:<QQ号> 或 group:<群号>，"
                "feishu 为 chat_id。不填则发回本任务的来源会话。发送失败会返回明确错误。",
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


# ── 消息往来查询（跨会话） ────────────────────────────────────────────────
# 动机：一个会话里发生的事，换个会话（Web ↔ QQ）她就不知道了，只能 shell 翻文件解析
# （实测绕了 7~9 步）。这里给她一条直路：谁找过、什么时候、她当时怎么回的。
# 数据源就是会话记录本身（projects/<pid>/s_*.jsonl）：不新增存储，不依赖网关进程。

#: 通道写进 user 文本首行的「场合说明」前缀（见 gateway/channels/napcat.py）
_CTX_PREFIXES = ("（这是你的 QQ",)
_PEER_RE = re.compile(r"「(.+?)」\((\d+)\)")


def _strip_ctx(content: str) -> Tuple[str, str]:
    """剥掉首行场合说明，返回 (正文, 对方标识)。"""
    text = content or ""
    first, sep, rest = text.partition("\n")
    if first.startswith(_CTX_PREFIXES):
        m = _PEER_RE.search(first)
        peer = f"{m.group(1)}({m.group(2)})" if m else ""
        return (rest if sep else ""), peer
    return text, ""


def _channel_of_session(session_id: str) -> str:
    """会话 id → 通道名：`s_napcat_<hash>` → napcat；无通道段（如 `s_<hash>`）→ local。"""
    parts = (session_id or "").split("_")
    if len(parts) >= 3 and parts[0] == "s":
        return parts[1]
    return "local"


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _collect_recent(channel: str, hours: float, max_files: int = 24) -> List[Dict[str, Any]]:
    """扫最近改动的会话文件，配成「对方说 → 我回」的往来组，按时间倒序。"""
    from omni_core.local import runtime_paths as P

    root = P.projects_root()
    if not root.exists():
        return []
    files = sorted(root.glob("*/s_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(hours)))

    pairs: List[Dict[str, Any]] = []
    for path in files[:max_files]:
        sid = path.stem
        ch = _channel_of_session(sid)
        if channel and ch != channel:
            continue
        pending: Optional[Dict[str, Any]] = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            role = str(rec.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            ts = _parse_ts(rec.get("ts") or "")
            if ts is not None and ts < cutoff:
                continue
            content = str(rec.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                body, peer = _strip_ctx(content)
                pending = {"ts": ts, "said": body, "peer": peer,
                           "project": path.parent.name, "session": sid, "channel": ch}
            elif pending is not None:
                pending["me"] = content
                pairs.append(pending)
                pending = None

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    pairs.sort(key=lambda p: p["ts"] or epoch, reverse=True)
    return pairs


@function_tool(
    description="查看最近的消息往来（跨会话/跨项目）：谁找过你、什么时候、你当时怎么回的。"
                "回答『有人找我吗』『我上次怎么跟他说的』用它，不要用 shell 去翻会话文件。",
    unit="messaging",
)
def msg_recent(channel: str = "", limit: int = 20, hours: float = 24) -> Dict[str, Any]:
    """最近消息往来。

    Args:
        channel: 只看某个通道（napcat/feishu…；缺省=全部）
        limit: 最多返回多少组往来（对方一句 + 我一句算一组）
        hours: 时间窗（小时）
    """
    try:
        pairs = _collect_recent(channel, hours)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        cap = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        cap = 20

    items: List[Dict[str, Any]] = []
    for p in pairs[:cap]:
        items.append({
            "when": p["ts"].isoformat() if p["ts"] else "",
            "channel": p["channel"],
            "project": p["project"],
            "who": p["peer"] or ("主人" if p["channel"] == "local" else ""),
            "said": (p["said"] or "")[:600],
            "my_reply": (p.get("me") or "")[:600],
        })
    return {
        "ok": True,
        "count": len(items),
        "hours": hours,
        "note": "按时间倒序；said=对方说的（已剥掉通道场合前缀），my_reply=我当时回他的",
        "messages": items,
    }
