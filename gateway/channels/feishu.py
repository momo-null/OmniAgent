"""飞书通道适配器（P0，接入设计 v1.1 §3）。

- 接收：lark-oapi WebSocket 长连接（免公网 IP）→ 事件去重 → 白名单 → InboundMessage
- 发送：im/v1 文本 + 原生卡片（新版卡片 schema 直作 content——230099 坑见 ETF MEMORY.md）
- 凭证：app_secret 走环境变量（默认 FEISHU_APP_SECRET），仓库/配置零秘钥
- 兼容垫片：venv setuptools≥81 移除了 pkg_resources.declare_namespace，
  lark-oapi 1.7.5 仍在调用——import 前补回（升级 SDK 后可删）。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..dispatch import ensure_worker  # noqa: F401  (收消息即确保派发worker在跑)
from .base import InboundMessage, OnMessage, ReplyChannel

_OMNI_DIR = Path.home() / ".omniagent"
_SEEN_FILE = _OMNI_DIR / "feishu_seen_message_ids.json"
_SEEN_MAX = 2000


def _lark():
    """惰性 import + pkg_resources 垫片（setuptools≥81 兼容）。"""
    import pkg_resources
    if not hasattr(pkg_resources, "declare_namespace"):
        pkg_resources.declare_namespace = lambda *a, **k: None
    import lark_oapi as lark
    return lark


class FeishuReply:
    """绑定会话的回发句柄（实现 ReplyChannel 协议）。"""

    def __init__(self, client, chat_id: str):
        self._client = client
        self.chat_id = chat_id
        self.channel_name = "feishu"

    def _send(self, msg_type: str, content: str) -> Dict[str, Any]:
        lark = _lark()
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = (CreateMessageRequest.builder()
               .receive_id_type("chat_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(self.chat_id)
                             .msg_type(msg_type)
                             .content(content)
                             .build())
               .build())
        resp = self._client.im.v1.message.create(req)
        if not resp.success():
            return {"ok": False, "error": f"{resp.code}: {resp.msg}"}
        return {"ok": True, "message_id": resp.data.message_id or ""}

    def send_text(self, text: str) -> Dict[str, Any]:
        return self._send("text", json.dumps({"text": text[:4000]}, ensure_ascii=False))

    def send_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # 新版卡片 schema 直接作 content；包 {"card": ...} 外层会 230099 parse err
        return self._send("interactive", json.dumps(payload, ensure_ascii=False))


class FeishuAdapter:
    name = "feishu"

    def __init__(self, cfg: Dict[str, Any]):
        self.app_id = cfg.get("app_id", "")
        self.app_secret = os.environ.get(cfg.get("app_secret_env", "FEISHU_APP_SECRET"), "")
        self.default_chat_id = cfg.get("default_chat_id", "")
        # 能力门：飞书机器人对所在组织/外部联系人可达 → 缺省纯对话（无工具）。
        # 只允许本人可达的场景（allow_user_ids 收紧到单账号）才建议置 true。
        self.allow_tools = bool(cfg.get("allow_tools", False))
        self.allow_chat_ids = set(cfg.get("allow_chat_ids", []) or [self.default_chat_id])
        self.allow_user_ids = set(cfg.get("allow_user_ids", []) or [])
        self._client = None
        self._seen = self._load_seen()
        self._seen_lock = threading.Lock()

    # ── 去重（WS 会重推事件；对齐 hermes 同名机制） ──
    def _load_seen(self) -> Dict[str, float]:
        try:
            return json.loads(_SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _is_dup(self, msg_id: str) -> bool:
        now = __import__("time").time()
        with self._seen_lock:
            dup = msg_id in self._seen
            self._seen[msg_id] = now
            if len(self._seen) > _SEEN_MAX:  # 滚动清理
                cutoff = sorted(self._seen.values())[-_SEEN_MAX // 2]
                self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
            try:
                _SEEN_FILE.write_text(json.dumps(self._seen), encoding="utf-8")
            except Exception:
                pass
        return dup

    # ── ChannelAdapter 协议 ──
    def connect(self, on_message: OnMessage) -> None:
        lark = _lark()
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        if not (self.app_id and self.app_secret):
            raise RuntimeError("feishu 通道未配置凭证（app_id / FEISHU_APP_SECRET）")

        self._client = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret).build()
        adapter = self

        def _on_event(data: P2ImMessageReceiveV1) -> None:
            try:
                adapter._handle_event(data, on_message)
            except Exception:
                import traceback
                traceback.print_exc()

        # lark-oapi 1.7.5：dispatcher 叫 EventDispatcherHandler（旧示例的 EventDispatcher 已不存在）
        dispatcher = (lark.EventDispatcherHandler.builder("", "")
                      .register_p2_im_message_receive_v1(_on_event).build())
        ws = lark.ws.Client(self.app_id, self.app_secret, event_handler=dispatcher, log_level=lark.LogLevel.INFO)
        threading.Thread(target=ws.start, name="feishu-ws", daemon=True).start()

    def _handle_event(self, data, on_message: OnMessage) -> None:
        import time
        event = data.event
        msg = event.message
        msg_id, chat_id = msg.message_id, msg.chat_id
        sender = getattr(getattr(event, "sender", None), "sender_id", None)
        user_id = getattr(sender, "open_id", "") or ""

        # 白名单（chat+user 双维；user 白名单为空则只限 chat）
        if self.allow_chat_ids and chat_id not in self.allow_chat_ids:
            return
        if self.allow_user_ids and user_id not in self.allow_user_ids:
            return
        if self._is_dup(msg_id):
            return

        # 文本投影：text 消息取 content.text；富消息取其文本字段
        text = ""
        try:
            content = json.loads(msg.content or "{}")
            text = content.get("text", "") or json.dumps(content, ensure_ascii=False)[:500]
        except Exception:
            text = (msg.content or "")[:500]

        on_message(InboundMessage(
            channel="feishu", chat_id=chat_id, user_id=user_id, text=text.strip(),
            raw={"message_id": msg_id, "message_type": msg.message_type, "ts": time.time()},
            reply=FeishuReply(self._client, chat_id),
            allow_tools=self.allow_tools,
        ))

    def reply_to(self, chat_id: str) -> ReplyChannel:
        return FeishuReply(self._client, chat_id)

    def broadcast(self, text: str) -> Dict[str, Any]:
        if not self.default_chat_id:
            return {"ok": False, "error": "feishu 未配置 default_chat_id"}
        return FeishuReply(self._client, self.default_chat_id).send_text(text)

    def close(self) -> None:  # lark ws 无显式关停句柄，进程退出即断
        pass


def build(cfg: Dict[str, Any]) -> Optional[FeishuAdapter]:
    """工厂：enabled 才实例化。"""
    if not cfg.get("enabled", False):
        return None
    return FeishuAdapter(cfg)
