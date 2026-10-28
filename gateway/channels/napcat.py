"""QQ 通道适配器（NapCat / OneBot 11 协议）。

设计（对齐接入设计 v1.1 §2/§3，与 feishu.py 同构）：
- 接收：正向 WebSocket 连 NapCat（缺省 ``ws://127.0.0.1:3001``），后台线程 +
  指数退避重连；只消费 ``post_type == "message"`` 的事件。
- 发送：OneBot 11 HTTP API（缺省 ``http://127.0.0.1:3000``），
  ``send_private_msg`` / ``send_group_msg``，Bearer token 可选。
- 归一：``InboundMessage.chat_id`` = ``private:<qq>`` / ``group:<群号>``，
  与网关其它通道共用 ``channel:chat_id`` 的会话键语义。
- 触发规则（配置化，写死=违规）：私聊缺省全回（``allow_users`` 非空即白名单），
  群聊缺省必须 @ 本机（``require_at``），``allow_groups`` 非空即群白名单。
- 硬性细节：忽略 ``post_type=message_sent``（否则本机发出的消息经 WS 回流
  变成新输入，形成自激循环）；``message_id`` 去重；长文本按 1200 字符分条。

配置段 ``channels.napcat``（``~/.omniagent/config.yaml``）：:

    channels:
      napcat:
        enabled: true
        http_url: http://127.0.0.1:3000
        ws_url: ws://127.0.0.1:3001
        token: ""                 # 明文或留空
        token_env: NAPCAT_TOKEN   # 也可从环境变量取（优先 token 非空）
        require_at: true          # 群聊必须 @ 才响应
        allow_users: []           # 私聊白名单（空 = 不限）
        allow_groups: []          # 群白名单（空 = 不限）
        default_chat_id: ""       # cron / 广播的兜底目标
        timeout_sec: 10
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Set, Tuple
from urllib.parse import quote

from utils import get_logger

from .base import InboundMessage, OnMessage, ReplyChannel

logger = get_logger("channels.napcat")

#: 消息段类型 → 文本占位（非文本内容不丢"有人发了东西"这个事实）
_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "reply": "[引用]",
    "forward": "[转发消息]",
    "json": "[卡片消息]",
    "xml": "[卡片消息]",
    "markdown": "[富文本]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "poke": "[戳一戳]",
    "share": "[分享]",
    "music": "[音乐]",
    "location": "[位置]",
    "contact": "[名片]",
}

#: CQ 码：[CQ:type,key=value,...]
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")
_CQ_UNESCAPE = (("&#91;", "["), ("&#93;", "]"), ("&#44;", ","), ("&amp;", "&"))


def _cq_unescape(text: str) -> str:
    for k, v in _CQ_UNESCAPE:
        text = text.replace(k, v)
    return text


def _ats_of_cq(params: str) -> Optional[str]:
    m = re.search(r"qq=([^,\]]+)", params or "")
    return m.group(1).strip() if m else None


def _from_segments(segments: Any) -> Tuple[str, Set[str]]:
    """OneBot 消息段数组 → (文本投影, 被 @ 的 qq 集合)。"""
    parts: list = []
    ats: Set[str] = set()
    if not isinstance(segments, list):
        return "", ats
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        elif seg_type == "at":
            qq = str(data.get("qq") or "")
            if not qq:
                continue
            ats.add(qq)
            # @ 目标本身不进 objective（模型不需要看到自己的号）；@全体保留提示
            if qq == "all":
                parts.append("@全体成员")
        else:
            parts.append(_PLACEHOLDER.get(seg_type, f"[{seg_type}]"))
    return "".join(parts), ats


def _from_cq(raw: str) -> Tuple[str, Set[str]]:
    """CQ 码字符串 → (文本投影, 被 @ 的 qq 集合)。"""
    ats: Set[str] = set()

    def _sub(m: "re.Match[str]") -> str:
        seg_type = m.group(1)
        params = m.group(2) or ""
        if seg_type == "at":
            qq = _ats_of_cq(params)
            if not qq:
                return ""
            ats.add(qq)
            return "@全体成员" if qq == "all" else ""
        return _PLACEHOLDER.get(seg_type, f"[{seg_type}]")

    return _cq_unescape(_CQ_RE.sub(_sub, raw or "")), ats


def parse_message(raw: Dict[str, Any]) -> Tuple[str, Set[str]]:
    """任意格式的 OneBot 消息（段数组 / CQ 字符串）→ (文本投影, @ 集合)。

    ``messagePostFormat`` 是 NapCat 侧配置，array / string 两种都要兼容。
    """
    message = raw.get("message")
    if isinstance(message, list):
        return _from_segments(message)
    if isinstance(message, str) and message:
        return _from_cq(message)
    return _from_cq(str(raw.get("raw_message") or ""))


def chunk_text(text: str, limit: int = 1200) -> list:
    """按字符上限切分（QQ 单条约 4500 字节，中文 3 字节/字，1200 留足余量）。"""
    text = text or ""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out: list = []
    buf = ""
    for line in text.splitlines(keepends=True) or [text]:
        while len(line) > limit:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            out.append(buf)
            buf = ""
        buf += line
    if buf:
        out.append(buf)
    return out


def _to_int(value: str) -> Any:
    """QQ 号 / 群号转 int（OneBot 要求数字）；非法则原样返回。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


class NapcatReply:
    """绑定会话的回发句柄（实现 ReplyChannel 协议）。"""

    def __init__(self, adapter: "NapcatAdapter", chat_id: str):
        self._adapter = adapter
        self.chat_id = chat_id
        self.channel_name = "napcat"

    def send_text(self, text: str) -> Dict[str, Any]:
        return self._adapter.send_text(self.chat_id, text)

    def send_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # QQ 无原生交互卡片：明确拒绝，让模型改用 msg_send_text（不静默吞）
        return {"ok": False, "error": "napcat（QQ）通道无原生卡片，请改用 msg_send_text"}


class NapcatAdapter:
    """QQ（NapCat / OneBot 11）通道适配器。"""

    name = "napcat"

    def __init__(self, cfg: Dict[str, Any]):
        self.http_url = str(cfg.get("http_url") or "http://127.0.0.1:3000").rstrip("/")
        self.ws_url = str(cfg.get("ws_url") or "ws://127.0.0.1:3001")
        token = str(cfg.get("token") or "")
        if not token:
            env_name = str(cfg.get("token_env") or "NAPCAT_TOKEN")
            token = os.environ.get(env_name, "") or ""
        self.token = token
        self.require_at = bool(cfg.get("require_at", True))
        self.allow_users = {str(x) for x in (cfg.get("allow_users") or [])}
        self.allow_groups = {str(x) for x in (cfg.get("allow_groups") or [])}
        self.default_chat_id = str(cfg.get("default_chat_id") or "")
        # 能力门：QQ 任何人都能发消息 → 缺省纯对话（无工具）。只有明确确认
        # "这个号只我自己能发"时才可在配置里置 true。
        self.allow_tools = bool(cfg.get("allow_tools", False))
        try:
            self.timeout_sec = float(cfg.get("timeout_sec", 10))
        except (TypeError, ValueError):
            self.timeout_sec = 10.0

        self._self_id = ""
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._seen_max = 2000
        self._seen_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    # ── 出站（HTTP API） ──
    def _api(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        import requests

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = requests.post(f"{self.http_url}/{action}", json=payload,
                                 headers=headers, timeout=self.timeout_sec)
            data = resp.json()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if not isinstance(data, dict):
            return {"ok": False, "error": f"非法响应: {str(data)[:200]}"}
        retcode = data.get("retcode", 0)
        if retcode not in (0, None) or data.get("status") == "failed":
            wording = data.get("message") or data.get("wording") or data.get("msg") or ""
            return {"ok": False, "error": f"retcode={retcode} {wording}".strip()}
        return {"ok": True, "data": data.get("data") or {}}

    def send_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        kind, _, ident = (chat_id or "").partition(":")
        if kind not in ("private", "group") or not ident:
            return {"ok": False, "error": f"chat_id 非法（应形如 private:<qq> / group:<群号>）: {chat_id!r}"}
        chunks = chunk_text(text)
        if not chunks:
            return {"ok": False, "error": "文本为空"}
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        id_key = "group_id" if kind == "group" else "user_id"
        message_ids = []
        for chunk in chunks:
            payload = {id_key: _to_int(ident),
                       "message": [{"type": "text", "data": {"text": chunk}}]}
            result = self._api(action, payload)
            if not result.get("ok"):
                return {"ok": False, "error": result.get("error", "发送失败"),
                        "sent": len(message_ids)}
            message_ids.append((result.get("data") or {}).get("message_id"))
        return {"ok": True, "message_id": message_ids[0] if message_ids else None,
                "chunks": len(message_ids)}

    # ── ChannelAdapter 协议 ──
    def connect(self, on_message: OnMessage) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_message,),
                                        name="napcat-ws", daemon=True)
        self._thread.start()

    def reply_to(self, chat_id: str) -> ReplyChannel:
        return NapcatReply(self, chat_id)

    def broadcast(self, text: str) -> Dict[str, Any]:
        if not self.default_chat_id:
            return {"ok": False, "error": "napcat 未配置 default_chat_id"}
        return self.send_text(self.default_chat_id, text)

    def close(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    # ── 入站（WebSocket 长连接） ──
    def _ws_url_with_token(self) -> str:
        if not self.token or "access_token=" in self.ws_url:
            return self.ws_url
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}access_token={quote(self.token)}"

    def _run(self, on_message: OnMessage) -> None:
        from websockets.sync.client import connect as ws_connect

        url = self._ws_url_with_token()
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with ws_connect(url, open_timeout=15, close_timeout=5,
                                max_size=None, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    backoff = 1.0
                    logger.info("napcat 已连接 %s", self.ws_url)
                    for raw_text in ws:
                        if self._stop.is_set():
                            break
                        self._dispatch_raw(raw_text, on_message)
            except Exception as e:
                if not self._stop.is_set():
                    logger.warning("napcat 连接中断: %s: %s", type(e).__name__, e)
            finally:
                self._ws = None
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _dispatch_raw(self, raw_text: Any, on_message: OnMessage) -> None:
        try:
            raw = json.loads(raw_text)
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        try:
            self._handle_event(raw, on_message)
        except Exception as e:  # 单条事件异常不拖垮长连接
            logger.warning("napcat 事件处理失败: %s: %s", type(e).__name__, e)

    def _is_dup(self, message_id: str) -> bool:
        now = time.time()
        with self._seen_lock:
            dup = message_id in self._seen
            self._seen[message_id] = now
            while len(self._seen) > self._seen_max:
                self._seen.popitem(last=False)
        return dup

    def _handle_event(self, raw: Dict[str, Any], on_message: OnMessage) -> None:
        self_id = str(raw.get("self_id") or "")
        if self_id:
            self._self_id = self_id
        # 只消费新消息；message_sent（自己发的）/ meta_event / notice / request 一律忽略
        if raw.get("post_type") != "message":
            return
        user_id = str(raw.get("user_id") or "")
        if not user_id or user_id == self._self_id:
            return
        message_id = str(raw.get("message_id") or "")
        if message_id and self._is_dup(message_id):
            return

        text, ats = parse_message(raw)
        message_type = str(raw.get("message_type") or "")
        if message_type == "group":
            group_id = str(raw.get("group_id") or "")
            if not group_id:
                return
            if self.allow_groups and group_id not in self.allow_groups:
                return
            if self.require_at and self._self_id and self._self_id not in ats:
                return
            chat_id = f"group:{group_id}"
        elif message_type == "private":
            if self.allow_users and user_id not in self.allow_users:
                return
            chat_id = f"private:{user_id}"
        else:
            return

        text = (text or "").strip()
        if not text:
            text = "[图片]" if self._has_image(raw) else "（收到一条非文本消息）"
        sender = raw.get("sender") or {}
        nickname = str(sender.get("card") or sender.get("nickname") or "").strip()
        # ── 场合自述（通道职责，内核零改动）──
        # 产品定位：QQ 是主人的代理通道，她**以主人的身份**对外说话（数字分身），
        # 服务对象始终是主人。每条消息必须让模型知道四件事：
        #   ① 这是你自己的 QQ（不是"别人转给你看的信息"）；
        #   ② 你代表主人，用第一人称「我」；
        #   ③ 回复直接发出去（不是让谁转达）——旧文案缺这条时，模型答过
        #      "我帮你想好了，就这么回他…"（把自己当秘书）；
        #   ④ 代答边界：可以说"话"，不能做"决定"（钱/账号/承诺/隐私/评价他人）。
        _bounds = ("你代表他，但不替他做决定——钱、账号、承诺、隐私、评价他人这类问题，"
                   "一律回「我确认下再回你」。")
        display = f"「{nickname}」({user_id})" if nickname else f"QQ {user_id}"
        if message_type == "group":
            group_id = chat_id.split(":", 1)[1]
            text = (f"（这是你的 QQ。你在群 {group_id} 里以主人的身份说话，用第一人称「我」；"
                    f"{display} @ 了你，回复会直接发在群里，其他人也看得到。{_bounds}）\n{text}")
        else:
            text = (f"（这是你的 QQ。你在以主人的身份跟 {display} 说话，用第一人称「我」，"
                    f"回复会直接发给对方，不用请谁转达。{_bounds}）\n{text}")
        on_message(InboundMessage(
            channel="napcat",
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            raw={
                "message_id": message_id,
                "message_type": message_type,
                "group_id": str(raw.get("group_id") or ""),
                "self_id": self._self_id,
                "nickname": str(sender.get("card") or sender.get("nickname") or ""),
                "ts": raw.get("time") or time.time(),
            },
            reply=NapcatReply(self, chat_id),
            allow_tools=self.allow_tools,
        ))

    @staticmethod
    def _has_image(raw: Dict[str, Any]) -> bool:
        message = raw.get("message")
        if isinstance(message, list):
            return any(isinstance(s, dict) and s.get("type") == "image" for s in message)
        return "CQ:image" in str(message or raw.get("raw_message") or "")


def build(cfg: Dict[str, Any]) -> Optional[NapcatAdapter]:
    """工厂：enabled 才实例化。"""
    if not cfg.get("enabled", False):
        return None
    return NapcatAdapter(cfg)
