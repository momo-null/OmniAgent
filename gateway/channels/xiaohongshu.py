"""小红书通道适配器（大肥鱼自运营 M4，plan §5；传输=xiaohongshu-mcp 的 MCP 端点）。

- **不自造浏览器自动化**：登录（扫码在其自带浏览器里完成）、风控环境、cookie 全部
  由 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) 承担——本适配器
  只做一件事：把它的 MCP 端点（streamable HTTP，JSON-RPC）翻译成网关通道契约。
  这是「能直接复用的现成件直接用」（MCP 优先纪律）的落地。
- 前置：本机跑起 xiaohongshu-mcp 服务（二进制/docker），首次登录用它的浏览器扫码
  （GUI/noVNC 或本机弹出的浏览器窗口）。**同一账号不要再在其他网页端登录**，会把
  服务端登录态踢掉（官方 README 强调）。
- 归一：chat_id 两种形态——
    ``note``                      → 发布图文笔记（send_image；小红书笔记必须有图）
    ``comment:<feed_id>:<xsec_token>`` → 给某条笔记发评论
  xsec_token 是随 feed 返回的访问令牌（每个链接都带，非账号秘钥），由读侧
  （plugins/xiaohongshu 的 xhs_* 只读工具）取到后随目标传回。
- 四道闸与 bilibili 同构（暂停→配额→黑名单→模式门，fail-closed 网关侧）；
  **小红书风控更凶：配额缺省压得更低**（评论 2/天、笔记 1/天）。
- 入站（评论回复）现状：xiaohongshu-mcp 没有「我的通知」类工具——本适配器轮询
  **自己已发笔记的评论区**（state 里登记 publish 返回的 feed_id），新评论归一成
  InboundMessage。TODO_XHS_LIVE：publish 的返回结构以实盘为准。
- 马甲不是人格（plan §8）：对外身份/笔墨 = 通道 yaml 的 outbound 块。

配置 ``~/.omniagent/channels/xiaohongshu.yaml``（模板 examples/channels/xiaohongshu.yaml）::

    enabled: false             # 出厂关：xiaohongshu-mcp 跑起来并登录后再开
    mode: dry_run              # dry_run | draft | live（与 bilibili 同语义）
    mcp_url: http://127.0.0.1:18060/mcp
    quotas: {comments_per_day: 2, notes_per_day: 1}
    blacklist: [...]           # 出站黑名单（时政等，同 bilibili 纪律）
    risk_pause_minutes: 480    # 小红书风控更凶，暂停更长
    poll_interval_sec: 300     # 轮询自己笔记的评论区
    default_chat_id: ""        # cron 广播兜底（一般留空）
    allow_tools: false
    timeout_sec: 20
    state_dir: ""              # 留空 = ~/.omniagent

命令行（联调用；MCP 端点不需要网关在跑）::

    python -m gateway.channels.xiaohongshu --login-status   # 检查登录态
    python -m gateway.channels.xiaohongshu --poll-once      # 拉自己笔记的评论区
"""
from __future__ import annotations

import argparse
import json
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..dispatch import ensure_worker  # noqa: F401  (收消息即确保派发 worker 在跑)
from .base import InboundMessage, OnMessage, ReplyChannel
from utils import get_logger

logger = get_logger("channels.xiaohongshu")

_XHS_TOOLS = {
    "login": "check_login_status",
    "publish": "publish_content",       # {title, content, images: [本地绝对路径]}
    "list": "list_feeds",
    "search": "search_feeds",           # {keyword}
    "detail": "get_feed_detail",        # {feed_id, xsec_token}
    "comment": "post_comment_to_feed",  # {feed_id, xsec_token, content}
}


class XhsReply:
    """绑定会话的回发句柄（实现 ReplyChannel 协议；写路径全部收口到适配器闸）。"""

    def __init__(self, adapter: "XhsAdapter", chat_id: str):
        self._adapter = adapter
        self.chat_id = chat_id
        self.channel_name = "xiaohongshu"

    def send_text(self, text: str) -> Dict[str, Any]:
        return self._adapter.send_text(self.chat_id, text)

    def send_image(self, image_path: str, text: str = "", title: str = "") -> Dict[str, Any]:
        """图文笔记（小红书笔记必须有图：image_path=本地绝对路径，标题+正文）。"""
        return self._adapter.send_note(image_path, text or "", title or "")

    def send_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "error": "xiaohongshu 通道不支持卡片（发笔记用 msg_send_image）"}


class XhsAdapter:
    name = "xiaohongshu"

    def __init__(self, cfg: Dict[str, Any]):
        self.mode = str(cfg.get("mode", "dry_run")).strip().lower()
        if self.mode not in ("dry_run", "draft", "live"):
            raise ValueError(f"xiaohongshu 通道 mode 非法: {self.mode}（dry_run|draft|live）")
        self.mcp_url = str(cfg.get("mcp_url", "http://127.0.0.1:18060/mcp"))
        self.timeout_sec = int(cfg.get("timeout_sec", 20))
        self.poll_interval_sec = max(60, int(cfg.get("poll_interval_sec", 300)))
        quotas = cfg.get("quotas") or {}
        self.quotas = {"comment": int(quotas.get("comments_per_day", 2)),
                       "note": int(quotas.get("notes_per_day", 1))}
        self.blacklist = [str(w) for w in (cfg.get("blacklist") or []) if str(w)]
        self.risk_pause_minutes = int(cfg.get("risk_pause_minutes", 480))
        self.default_chat_id = str(cfg.get("default_chat_id", "") or "")
        self.allow_tools = bool(cfg.get("allow_tools", False))

        base = Path(cfg.get("state_dir") or (Path.home() / ".omniagent"))
        base.mkdir(parents=True, exist_ok=True)
        self.state_file = base / "xiaohongshu_state.json"
        self.outbox_file = base / "xiaohongshu_outbox.jsonl"

        self._state_lock = threading.Lock()
        self._mcp_session = ""
        self._rpc_id = 0
        self._login_cache: Tuple[float, bool] = (0.0, False)
        self._stop = threading.Event()

    # ── 状态/配额（与 bilibili 同构） ───────────────────────────────────────
    #: 跨天保留键（笔记登记/评论去重/暂停）——日翻账只清当日计数
    _STATE_KEEP = ("notes", "seen_comments", "paused_until", "paused_reason")

    def _load_state(self) -> Dict[str, Any]:
        st: Dict[str, Any] = {}
        try:
            st = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            st = {}  # 缺文件/坏 JSON 与跨日一样走翻账
        if not isinstance(st, dict):
            st = {}
        if st.get("date") != self._today():
            keep = {k: v for k, v in st.items() if k in self._STATE_KEEP}
            keep["date"] = self._today()
            st = keep
        return st

    def _save_state(self, st: Dict[str, Any]) -> None:
        self.state_file.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _gate_paused(self) -> Optional[str]:
        st = self._load_state()
        until = float(st.get("paused_until") or 0)
        if until > time.time():
            mins = int((until - time.time()) / 60) + 1
            return f"通道风控暂停中（剩 {mins} 分钟）：{st.get('paused_reason', '')}"
        return None

    def _pause(self, reason: str) -> None:
        with self._state_lock:
            st = self._load_state()
            st["paused_until"] = time.time() + self.risk_pause_minutes * 60
            st["paused_reason"] = reason
            self._save_state(st)
        logger.error("xiaohongshu 通道暂停 %s 分钟：%s", self.risk_pause_minutes, reason)

    def _gate_quota(self, kind: str) -> Optional[str]:
        with self._state_lock:
            st = self._load_state()  # 已处理跨日翻账
            used = int(st.get(kind, 0))
            limit = self.quotas.get(kind, 0)
            if used >= limit:
                label = "评论" if kind == "comment" else "笔记"
                return f"今日{label}配额已用尽（{used}/{limit}），明天再来"
            return None

    def _consume_quota(self, kind: str) -> None:
        with self._state_lock:
            st = self._load_state()
            st[kind] = int(st.get(kind, 0)) + 1
            self._save_state(st)

    def _gate_blacklist(self, text: str) -> Optional[str]:
        for w in self.blacklist:
            if w and w in text:
                return f"命中出站黑名单（{w[:2]}**），已拒发"
        return None

    def _gates(self, kind: str, text: str) -> Optional[str]:
        for err in (self._gate_paused(), self._gate_quota(kind), self._gate_blacklist(text)):
            if err:
                return err
        return None

    # ── MCP 传输（streamable HTTP JSON-RPC；登录态由服务端浏览器承担） ──────
    def _mcp_rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
                 notify: bool = False) -> Dict[str, Any]:
        """一次 JSON-RPC POST；兼容 JSON 与 SSE 两种响应。返回原始响应 dict。"""
        import requests

        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self._mcp_session:
            headers["Mcp-Session-Id"] = self._mcp_session
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._rpc_id += 1
            payload["id"] = self._rpc_id
        if params is not None:
            payload["params"] = params
        try:
            resp = requests.post(self.mcp_url, json=payload, headers=headers,
                                 timeout=self.timeout_sec)
        except Exception as e:
            return {"ok": False, "error": f"MCP 端点不可达（{self.mcp_url}）: {e}"}
        sid = resp.headers.get("Mcp-Session-Id")
        if sid:
            self._mcp_session = sid
        if resp.status_code >= 400:
            return {"ok": False, "error": f"MCP HTTP {resp.status_code}: {resp.text[:200]}"}
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.text
        if "text/event-stream" in ctype:
            for line in raw.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    blob = line[5:].strip()
                    if blob and blob != "[DONE]":
                        try:
                            return json.loads(blob)
                        except Exception:
                            continue
            return {"ok": False, "error": "SSE 响应里没有可解析的数据帧"}
        try:
            return resp.json()
        except Exception:
            return {"ok": False, "error": f"MCP 返回非 JSON ({resp.status_code}): {raw[:200]}"}

    def _mcp_init(self) -> Dict[str, Any]:
        ret = self._mcp_rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "omniagent-xhs", "version": "1.0.0"},
        })
        if ret.get("ok") is False or not ret:
            return ret
        self._mcp_rpc("notifications/initialized", notify=True)
        return ret

    def _mcp_call(self, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用 xiaohongshu-mcp 工具；返回 {"ok", "data"(解析后的文本)/"raw"}。"""
        if not self._mcp_session:
            init = self._mcp_init()
            if init.get("ok") is False or not init:
                return {"ok": False, "error": init.get("error", "MCP initialize 失败")}
        resp = self._mcp_rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        if resp.get("ok") is False:
            return resp
        err = resp.get("error")
        if err:
            return {"ok": False, "error": f"MCP error: {err.get('message', err)}"}
        result = resp.get("result") or {}
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content") or []]
            return {"ok": False, "error": " / ".join(t for t in texts if t) or "工具执行失败"}
        texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
        blob = "\n".join(texts).strip()
        try:
            return {"ok": True, "data": json.loads(blob)}
        except Exception:
            return {"ok": True, "data": blob}

    # ── 就绪与登录态 ───────────────────────────────────────────────────────
    def _login_status(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self._login_cache[0] < 300:
            return self._login_cache[1]
        ret = self._mcp_call(_XHS_TOOLS["login"])
        logged = bool(ret.get("ok")) and bool(
            (ret.get("data") or {}).get("logged_in")
            if isinstance(ret.get("data"), dict) else ret.get("data"))
        # 容忍非 JSON 文本返回：包含 logged_in/true 字样也算
        if not logged and isinstance(ret.get("data"), str):
            low = ret["data"].lower()
            logged = ("logged_in" in low and "false" not in low) or "true" in low
        self._login_cache = (now, logged)
        return logged

    def ready(self) -> bool:
        """调度触发闸：MCP 端点可达且已登录才就绪（结果缓存 5 分钟）。"""
        return self._login_status()

    # ── 发送（全部收口到这里） ─────────────────────────────────────────────
    def send_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        chat_id = (chat_id or "").strip()
        text = (text or "").strip()
        if chat_id == "note":
            return {"ok": False,
                    "error": "小红书笔记必须有图：用 msg_send_image 发图文（send_note）"}
        parts = chat_id.split(":")
        if len(parts) == 3 and parts[0] == "comment":
            _, feed_id, xsec = parts
            if not (feed_id and xsec):
                return {"ok": False, "error": f"comment 目标不完整: {chat_id}"}
            kind, args = "comment", {"feed_id": feed_id, "xsec_token": xsec, "content": text}
        else:
            return {"ok": False, "error": (f"未知目标 chat_id: {chat_id}"
                                           "（应为 note 或 comment:<feed_id>:<xsec_token>）")}
        if not text:
            return {"ok": False, "error": "空文本不发送"}
        if self.mode in ("draft", "live") and not self.ready():
            return {"ok": False, "error": "xiaohongshu 未登录：先在 xiaohongshu-mcp 的浏览器里扫码"}
        if self.mode == "draft":
            err = self._gate_paused() or self._gate_blacklist(text)
            if err:
                return {"ok": False, "error": err}
            self._outbox_append(kind, chat_id, text)
            return {"ok": True, "queued": True, "chat_id": chat_id}
        err = self._gates(kind, text)
        if err:
            return {"ok": False, "error": err}
        if self.mode == "dry_run":
            logger.info("[xhs dry_run] %s %s", chat_id, text[:60])
            return {"ok": True, "dry_run": True, "chat_id": chat_id}
        ret = self._mcp_call(_XHS_TOOLS[kind], args)
        if not ret.get("ok"):
            err = str(ret.get("error", ""))
            if any(k in err for k in ("风控", "频繁", "captcha", "登录")) or "未登录" in err:
                self._pause(f"发送被拒：{err[:120]}")
                return {"ok": False, "error": f"发送被拒并触发通道暂停：{err[:120]}"}
            return {"ok": False, "error": err}
        self._consume_quota(kind)
        out: Dict[str, Any] = {"ok": True, "chat_id": chat_id}
        data = ret.get("data")
        if kind == "comment" and isinstance(data, dict):
            cid = str(data.get("comment_id") or data.get("id") or "")
            if cid:
                out["comment_id"] = cid
        return out

    def send_note(self, image_path: str, content: str, title: str = "") -> Dict[str, Any]:
        """图文笔记（小红书笔记必有图；配额按 note 计）。"""
        import os

        path = str(image_path or "").strip()
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": f"图片不存在: {path}"}
        title = (title or "").strip()[:20]          # 小红书标题上限 20 字
        content = (content or "").strip()
        if not title or not content:
            return {"ok": False, "error": "标题与正文都不能为空（title ≤20 字）"}
        if self.mode in ("draft", "live") and not self.ready():
            return {"ok": False, "error": "xiaohongshu 未登录：先在 xiaohongshu-mcp 的浏览器里扫码"}
        if self.mode == "draft":
            err = self._gate_paused() or self._gate_blacklist(f"{title}{content}")
            if err:
                return {"ok": False, "error": err}
            self._outbox_append("note", "note", f"{title}｜{content}｜[图片:{path}]")
            return {"ok": True, "queued": True, "chat_id": "note"}
        err = self._gates("note", f"{title}{content}")
        if err:
            return {"ok": False, "error": err}
        if self.mode == "dry_run":
            logger.info("[xhs dry_run] note %s %s", title, content[:40])
            return {"ok": True, "dry_run": True, "chat_id": "note"}
        ret = self._mcp_call(_XHS_TOOLS["publish"],
                             {"title": title, "content": content,
                              "images": [os.path.abspath(path)]})
        if not ret.get("ok"):
            err = str(ret.get("error", ""))
            if any(k in err for k in ("风控", "频繁", "captcha", "登录")):
                self._pause(f"发布被拒：{err[:120]}")
                return {"ok": False, "error": f"发布被拒并触发通道暂停：{err[:120]}"}
            return {"ok": False, "error": err}
        self._consume_quota("note")
        data = ret.get("data") if isinstance(ret.get("data"), dict) else {}
        # TODO_XHS_LIVE：publish 返回里的笔记 id/xsec_token 字段名以实盘为准；登记进
        # state["notes"] 供轮询自己笔记的评论区（入站回评链路）
        st = self._load_state()
        notes = st.get("notes") or []
        notes.append({"ts": time.time(), "title": title,
                      "feed_id": str(data.get("feed_id") or data.get("id") or ""),
                      "xsec_token": str(data.get("xsec_token") or "")})
        st["notes"] = notes[-50:]
        self._save_state(st)
        return {"ok": True, "chat_id": "note", "title": title}

    # ── 入站：轮询自己笔记的评论区 ─────────────────────────────────────────
    def poll_once(self, on_message: Optional[OnMessage] = None) -> List[InboundMessage]:
        """对 state 里登记的自己笔记逐条 get_feed_detail，新评论归一为 InboundMessage。

        xiaohongshu-mcp 无通知类工具——这是 M4 入站的替代路径；评论区的评论
        ``comment_id`` 作去重键落 state["seen_comments"]。
        """
        dispatched: List[InboundMessage] = []
        if not self._login_status():
            return dispatched
        st = self._load_state()
        seen = set(st.get("seen_comments") or [])
        for note in reversed((st.get("notes") or [])[-10:]):
            feed_id, xsec = note.get("feed_id", ""), note.get("xsec_token", "")
            if not (feed_id and xsec):
                continue  # TODO_XHS_LIVE：publish 未回 token 的笔记轮询不了，先跳过
            ret = self._mcp_call(_XHS_TOOLS["detail"], {"feed_id": feed_id, "xsec_token": xsec})
            if not ret.get("ok"):
                continue
            data = ret.get("data") or {}
            comments = data.get("comments") if isinstance(data, dict) else None
            for c in comments or []:
                cid = str(c.get("comment_id") or c.get("id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                msg = InboundMessage(
                    channel="xiaohongshu", chat_id=f"comment:{feed_id}:{xsec}",
                    user_id=str(c.get("user_id") or ""),
                    text=str(c.get("content") or c.get("text") or ""),
                    title=f"「{c.get('nickname') or c.get('user_id') or '?'}」的笔记评论",
                    display_text=str(c.get("content") or c.get("text") or ""),
                    raw={"note_title": note.get("title", ""), "comment": c},
                    allow_tools=self.allow_tools, reply_mode="explicit",
                    speaker=f"{c.get('nickname') or ''}({c.get('user_id') or ''})",
                    reply=XhsReply(self, f"comment:{feed_id}:{xsec}"),
                )
                dispatched.append(msg)
                if on_message is not None:
                    try:
                        on_message(msg)
                    except Exception:
                        logger.exception("xiaohongshu 入站派发回调异常")
        with self._state_lock:
            st = self._load_state()
            st["seen_comments"] = list(seen)[-2000:]
            self._save_state(st)
        return dispatched

    def _outbox_append(self, kind: str, chat_id: str, text: str) -> None:
        item = {"ts": time.time(), "status": "pending", "kind": kind,
                "chat_id": chat_id, "text": text}
        with self._state_lock:
            with self.outbox_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def outbox(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        try:
            for line in self.outbox_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    items.append(json.loads(line))
        except Exception:
            pass
        return items

    def _rewrite_outbox(self, items: List[Dict[str, Any]]) -> None:
        with self._state_lock:
            self.outbox_file.write_text(
                "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n",
                encoding="utf-8")

    def approve_item(self, index: int) -> Dict[str, Any]:
        """批准待发第 index 条（1 起）真发（live 语义，仍过全量四道闸）。CLI 与审批 API 共用。"""
        items = self.outbox()
        if not (1 <= int(index) <= len(items)):
            return {"ok": False, "error": f"序号越界（1..{len(items)}）"}
        it = items[int(index) - 1]
        if it.get("status") == "sent":
            return {"ok": False, "error": "该条已发送", "already_sent": True}
        if it.get("status") == "discarded":
            return {"ok": False, "error": "该条已作废"}
        if it.get("kind") == "note":
            # note 草稿文本是 "标题｜正文｜[图片:路径]" 的队列投影——批准走 send_note 全量闸
            title, content, img = (it["text"].split("｜") + ["", ""])[:3]
            path = img.replace("[图片:", "").rstrip("]")
            saved = self.mode
            self.mode = "live"
            try:
                ret = self.send_note(path, content, title)
            finally:
                self.mode = saved
            if ret.get("ok"):
                it["status"] = "sent"
                self._rewrite_outbox(items)
            return {"ok": bool(ret.get("ok")), "result": ret, "item": it}
        saved = self.mode
        self.mode = "live"
        try:
            ret = self.send_text(it["chat_id"], it["text"])
        finally:
            self.mode = saved
        if ret.get("ok"):
            it["status"] = "sent"
            self._rewrite_outbox(items)
        return {"ok": bool(ret.get("ok")), "result": ret, "item": it}

    def discard_item(self, index: int) -> Dict[str, Any]:
        """作废待发第 index 条（不发送，留审计痕迹）。"""
        items = self.outbox()
        if not (1 <= int(index) <= len(items)):
            return {"ok": False, "error": f"序号越界（1..{len(items)}）"}
        it = items[int(index) - 1]
        it["status"] = "discarded"
        self._rewrite_outbox(items)
        return {"ok": True, "item": it}

    # ── ChannelAdapter 协议 ────────────────────────────────────────────────
    def connect(self, on_message: OnMessage) -> None:
        if not self.ready():
            logger.warning("xiaohongshu 通道未就绪（MCP 未跑或未登录）：先启动 xiaohongshu-mcp "
                           "并扫码登录；登录后重启网关开始轮询")
            return
        t = threading.Thread(target=self._poll_loop, args=(on_message,),
                             name="xhs-poll", daemon=True)
        t.start()

    def _poll_loop(self, on_message: OnMessage) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once(on_message)
            except Exception:
                logger.exception("xiaohongshu 轮询异常（下一轮重试）")
            self._stop.wait(self.poll_interval_sec + random.uniform(0, 30))

    def reply_to(self, chat_id: str) -> ReplyChannel:
        return XhsReply(self, chat_id)

    def broadcast(self, text: str) -> Dict[str, Any]:
        if not self.default_chat_id:
            return {"ok": False, "error": "xiaohongshu 未配置 default_chat_id（结果只落运行记录）"}
        return self.send_text(self.default_chat_id, text)

    def close(self) -> None:
        self._stop.set()


def build(cfg: Dict[str, Any]) -> Optional["XhsAdapter"]:
    """工厂：enabled 才实例化（与 feishu/napcat 同构）。"""
    if not cfg.get("enabled", False):
        return None
    return XhsAdapter(cfg)


# ── 命令行：联调 ────────────────────────────────────────────────────────────
def _main() -> int:
    ap = argparse.ArgumentParser(description="xiaohongshu 通道联调工具")
    ap.add_argument("--login-status", action="store_true", help="检查 xiaohongshu-mcp 登录态")
    ap.add_argument("--poll-once", action="store_true", help="拉一次自己笔记的评论区（不派发）")
    ap.add_argument("--config-dir", default="", help="覆盖状态目录")
    args = ap.parse_args()

    from ..channel_gateway import channel_config
    cfg = channel_config("xiaohongshu")
    if args.config_dir:
        cfg["state_dir"] = args.config_dir
    if not cfg.get("enabled", False):
        print("提示：channels/xiaohongshu.yaml 未启用——登录/联调照常继续；"
              "要让网关拉起本通道请置 enabled: true 并重启网关")
    a = XhsAdapter(cfg)
    if args.login_status:
        logged = a._login_status(force=True)
        print(json.dumps({"logged_in": logged, "mcp_url": a.mcp_url}, ensure_ascii=False))
        return 0 if logged else 1
    if args.poll_once:
        ret = a._mcp_init()
        if ret.get("ok") is False:
            print(json.dumps(ret, ensure_ascii=False))
            return 1
        st = a._load_state()
        for note in (st.get("notes") or [])[-5:]:
            if not (note.get("feed_id") and note.get("xsec_token")):
                continue
            r = a._mcp_call(_XHS_TOOLS["detail"],
                            {"feed_id": note["feed_id"], "xsec_token": note["xsec_token"]})
            print(json.dumps(r.get("data") or r, ensure_ascii=False)[:800])
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
