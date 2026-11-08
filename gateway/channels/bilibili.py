"""B站通道适配器（大肥鱼自运营，plan 见 doc/worklog/xhs-bili-autonomous-ops-plan-2026-11-07.md）。

设计（与 napcat/feishu 同构，通用化判据：本文件 + 一段配置，dispatch/messaging/scheduler 零改动；
内核 omni_core/ 零改动是本特性的硬约束）：

- 接收：轮询「回复我的」通知（``x/msgfeed/reply``，Web cookie 鉴权），后台线程 +
  抖动间隔；首拍静默基线（只记 seen 不派发）防历史灌入，之后仅派发新增并去重落盘。
- 发送：评论回复（``x/v2/reply/add``）与发动态（``x/dyn/create``），csrf=bili_jct。
  ⚠ 端点为社区已知 Web 形态（TODO_BILI_LIVE 标记处）：闸门逻辑是耐久的，端点常量
  以首次实盘联调为准逐一核对。
- **写路径收口**：一切平台写操作必过四道闸（顺序固定，fail-closed）：
  风控暂停 → 每日配额 → 出站黑名单 → 模式门（dry_run / draft / live）。
  模型侧永不直接拿平台写工具（与「MCP 审批未收口前只接只读 server」同源纪律）。
- 凭证：**TV 端扫码登录**（``--login``，终端二维码 → App 确认 → cookie 落 state_dir
  凭证文件，与 dsh-bilibili-plugin/biliup 同款协议）；也兼容 yaml 直填 / 环境变量
  （BILI_SESSDATA / BILI_JCT / DEDEUSERID）。优先级：yaml 直填 > 环境变量 > 凭证文件。
- 归一：``chat_id = comment:<oid>:<type>:<root>:<parent>`` / ``dynamic``，
  共用网关 ``channel:chat_id`` 会话键语义（routes.yaml 把 ``bilibili:*`` 钉到项目）。
- 马甲不是人格（plan §8，2026-11-08 裁决）：对外身份=本通道 yaml 的 ``outbound.identity``、
  笔墨=``outbound.style``（机械闸在 msg_send_*，归一见 gateway/outbound.py）；人格统一在
  角色卡不分身；routes.yaml → project_id=bilibili 只是工作目录，承载运营纪律与虚构边界
  （projects/bilibili/AGENTS.md），不承载人格。

配置文件 ``~/.omniagent/channels/bilibili.yaml``（模板 examples/channels/bilibili.yaml）::

    enabled: false             # 出厂关
    mode: dry_run              # dry_run=只演练不真发 | draft=入待发队列 | live=真发
    sessdata: ""               # cookie 三件套；留空则取环境变量
    bili_jct: ""
    dedeuserid: ""
    sessdata_env: BILI_SESSDATA
    bili_jct_env: BILI_JCT
    dedeuserid_env: DEDEUSERID
    poll_interval_sec: 90      # 轮询间隔（叠加随机抖动，避免机械规律）
    poll_jitter_sec: 30
    lookback_hours: 24         # 只派发此新鲜度内的通知
    self_uid: ""               # 留空 = 连接时用 nav 接口自取（过滤自己、识别身份）
    quotas:                    # 每日配额账本（本地自然日；超限拒发并报错给模型）
      comments_per_day: 4
      dynamics_per_day: 1
    blacklist: []              # 出站文本黑名单（子串命中即拒发；建议至少覆盖时政）
    risk_pause_minutes: 240    # 撞风控（-412/-352/HTTP 412/429）后的暂停时长
    default_chat_id: ""        # cron/广播兜底（如 "dynamic"）；留空 = 结果只落记录
    allow_tools: false         # 外部可达通道缺省纯对话（B站评论区任何人可触达）
    timeout_sec: 10
    state_dir: ""              # 留空 = ~/.omniagent；测试/多号可另指目录

命令行（联调与土法审批，不依赖网关进程；配置读 channels/bilibili.yaml）::

    python -m gateway.channels.bilibili --login        # TV 扫码登录（推荐）
    python -m gateway.channels.bilibili --whoami      # 校验 cookie，打印 uid/uname
    python -m gateway.channels.bilibili --poll-once   # 拉一次通知并打印归一结果（不派发）
    python -m gateway.channels.bilibili --outbox      # 列 draft 模式的待发草稿
    python -m gateway.channels.bilibili --approve N   # 批准发送第 N 条草稿（仍过配额/黑名单）
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

logger = get_logger("channels.bilibili")

# ── 端点（TODO_BILI_LIVE：首次实盘联调逐一对参）─────────────────────────────
_API_NAV = "https://api.bilibili.com/x/web-interface/nav"
_API_REPLY_FEED = "https://api.bilibili.com/x/msgfeed/reply"
_API_REPLY_ADD = "https://api.bilibili.com/x/v2/reply/add"
# 动态发布：x/dyn/create 已 404（实盘 2026-11-08），用 legacy dynamic_svr（type=4 纯文字）
_API_DYN_CREATE = "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/create"

# ── TV 端扫码登录（与 dsh-bilibili-plugin / biliup 同款协议）────────────────
# TV 协议的优势：确认后 cookie 直接在 JSON body 返回（不用解析 Set-Cookie、
# 不需要浏览器环境），并顺带返回 mid。appkey/appsec 是 B站 TV 客户端的公开
# 常量（bilibili-API-collect 社区文档收录，全生态通用），非秘钥——真正授权
# 永远要本人用 App 扫码确认。
_TV_QR_GEN = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
_TV_QR_POLL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
_TV_APPKEY = "4409e2ce8ffd12b8"
_TV_APPSEC = "59b43e04ad6965f34319062b478f83dd"

# ── WBI 签名（现代读接口通行证：动态流/热榜/搜索等，bilibili-API-collect 标准算法）──
_WBI_NAV = "https://api.bilibili.com/x/web-interface/nav"
_WBI_TAB = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
            33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
            26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
            20, 34, 44, 52)
_WBI_FILTER = set("!'()*")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

#: 风控/凭证类响应码：命中即持久化暂停（对齐养鱼插件「撞限流就停手」纪律）
_RISK_CODES = {-412, -352}
_CRED_CODES = {-101}
#: HTTP 层风控状态码
_RISK_HTTP = {412, 429}

_COMMENT_MAX_CHARS = 1000  # B站评论区单条上限量级；超长拒发让模型自己精简


def _tv_sign(params: Dict[str, str]) -> str:
    """TV 端签名：参数按 key 排序拼接 + appsec 取 md5（对齐生态通用实现）。"""
    import hashlib
    from urllib.parse import quote

    qs = "&".join(f"{k}={quote(str(params[k]), safe='')}" for k in sorted(params))
    return hashlib.md5((qs + _TV_APPSEC).encode("utf-8")).hexdigest()


def _mixin_key(img_key: str, sub_key: str) -> str:
    """WBI：img_key+sub_key 按 Mixin 表重排取前 32 位。"""
    return "".join((img_key + sub_key)[i] for i in _WBI_TAB)[:32]


def _wbi_sign(params: Dict[str, Any], mixin: str, wts: int) -> Dict[str, Any]:
    """WBI：参数加 wts → 按 key 排序、过滤 !'()* → urlencode 拼 mixin_key 取 md5 得 w_rid。"""
    import hashlib
    from urllib.parse import urlencode

    clean = {}
    for k, v in params.items():
        v = str(v)
        clean[k] = "".join(ch for ch in v if ch not in _WBI_FILTER)
    clean["wts"] = wts
    qs = urlencode(sorted(clean.items()))
    clean["w_rid"] = hashlib.md5((qs + mixin).encode("utf-8")).hexdigest()
    return clean


def _deep_find(obj: Any, keys: Tuple[str, ...], depth: int = 3) -> Any:
    """在嵌套 dict/list 里浅层深搜第一个命中键（通知结构社区文档有出入，取宽容解析）。

    只取标量值：命中 dict/list（如 content 是容器）时继续下钻，不当结果返回。
    """
    if depth < 0:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            hit = _deep_find(v, keys, depth - 1)
            if hit not in (None, ""):
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _deep_find(v, keys, depth - 1)
            if hit not in (None, ""):
                return hit
    return None


class BilibiliReply:
    """绑定会话的回发句柄（实现 ReplyChannel 协议；发送全部走适配器闸门）。"""

    def __init__(self, adapter: "BilibiliAdapter", chat_id: str):
        self._adapter = adapter
        self.chat_id = chat_id
        self.channel_name = "bilibili"

    def send_text(self, text: str) -> Dict[str, Any]:
        return self._adapter.send_text(self.chat_id, text)

    def send_image(self, image_path: str, text: str = "") -> Dict[str, Any]:
        """带图动态（M2：draw 上传 + type=2；走适配器全量闸）。"""
        return self._adapter.send_image(self.chat_id, image_path, text)

    def send_card(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "error": "bilibili 通道不支持卡片（msg_send_text 即可）"}


class BilibiliAdapter:
    name = "bilibili"

    def __init__(self, cfg: Dict[str, Any]):
        self.mode = str(cfg.get("mode", "dry_run")).strip().lower()
        if self.mode not in ("dry_run", "draft", "live"):
            raise ValueError(f"bilibili 通道 mode 非法: {self.mode}（dry_run|draft|live）")
        self.timeout_sec = int(cfg.get("timeout_sec", 10))
        self.poll_interval_sec = max(15, int(cfg.get("poll_interval_sec", 90)))
        self.poll_jitter_sec = max(0, int(cfg.get("poll_jitter_sec", 30)))
        self.lookback_hours = float(cfg.get("lookback_hours", 24))
        quotas = cfg.get("quotas") or {}
        self.quotas = {"comment": int(quotas.get("comments_per_day", 4)),
                       "dynamic": int(quotas.get("dynamics_per_day", 1))}
        # 活人感冷却闸：连发 N 天动态后强制歇一天（0=关）。真人不是日更机器。
        self.dynamic_cooldown_days = int(cfg.get("dynamic_cooldown_days", 2))
        self.blacklist = [str(w) for w in (cfg.get("blacklist") or []) if str(w)]
        self.risk_pause_minutes = int(cfg.get("risk_pause_minutes", 240))
        self.default_chat_id = str(cfg.get("default_chat_id", "") or "")
        self.allow_tools = bool(cfg.get("allow_tools", False))
        # cookie：直填优先，其次环境变量（密钥不落盘）
        self.sessdata = str(cfg.get("sessdata", "") or "") or _env(cfg, "sessdata")
        self.bili_jct = str(cfg.get("bili_jct", "") or "") or _env(cfg, "bili_jct")
        self.dedeuserid = str(cfg.get("dedeuserid", "") or "") or _env(cfg, "dedeuserid")
        self.self_uid = str(cfg.get("self_uid", "") or "")

        # 状态目录（state_dir 可覆盖 → 测试/多号隔离）
        base = Path(cfg.get("state_dir") or (Path.home() / ".omniagent"))
        base.mkdir(parents=True, exist_ok=True)
        self.state_file = base / "bilibili_state.json"
        self.seen_file = base / "bilibili_seen.json"
        self.outbox_file = base / "bilibili_outbox.jsonl"

        self._state_lock = threading.Lock()
        self._seen: Dict[str, float] = {}
        self._seen_lock = threading.Lock()
        self._stop = threading.Event()
        self._load_seen()
        if not self.self_uid:  # 凭证文件里有 mid 就直接用（--login 写入）
            self.self_uid = str(self._load_credentials_file().get("mid") or "")

    # ── 状态/去重（对齐 feishu 的落盘去重） ─────────────────────────────────
    #: 跨天保留键（冷却连发/暂停/笔记登记等长期状态）——日翻账只清当日计数
    _STATE_KEEP = ("dyn_last_date", "dyn_streak", "paused_until", "paused_reason")

    def _load_state(self) -> Dict[str, Any]:
        st: Dict[str, Any] = {}
        try:
            st = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            st = {}  # 缺文件/坏 JSON 与跨日一样走翻账——否则首次消费写出无 date 状态被下次清零
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

    def _load_seen(self) -> None:
        try:
            self._seen = json.loads(self.seen_file.read_text(encoding="utf-8"))
        except Exception:
            self._seen = {}

    def _mark_seen(self, key: str) -> None:
        with self._seen_lock:
            self._seen[key] = time.time()
            if len(self._seen) > 4000:  # 滚动清理
                cutoff = sorted(self._seen.values())[-2000]
                self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
            try:
                self.seen_file.write_text(json.dumps(self._seen), encoding="utf-8")
            except Exception:
                pass

    # ── 凭证文件（--login 写入；优先级：yaml 直填 > 环境变量 > 本文件） ──────
    @property
    def credentials_file(self) -> Path:
        return self.state_file.parent / "bilibili_credentials.json"

    def _save_credentials(self, cookies: Dict[str, str], mid: str = "") -> None:
        payload = {"sessdata": cookies.get("SESSDATA", ""),
                   "bili_jct": cookies.get("bili_jct", ""),
                   "dedeuserid": cookies.get("DedeUserID", ""),
                   "mid": str(mid or ""),
                   "saved_at": time.time()}
        self.credentials_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        logger.info("bilibili 凭证已保存 %s（uid=%s）", self.credentials_file, mid or "?")

    def _load_credentials_file(self) -> Dict[str, Any]:
        try:
            return json.loads(self.credentials_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    # ── TV 端扫码登录（与 dsh-bilibili-plugin / biliup 同款协议）────────────
    def start_login(self) -> Dict[str, Any]:
        """发起扫码：返回 {ok, auth_code, url}；url 即二维码内容（175 秒有效）。"""
        params = {"appkey": _TV_APPKEY, "local_id": "0", "ts": str(int(time.time()))}
        params["sign"] = _tv_sign(params)
        ret = self._http("POST", _TV_QR_GEN, data=params)
        body = ret.get("body") or {}
        if body.get("code") != 0:
            return {"ok": False,
                    "error": f"获取二维码失败: code={body.get('code')} {body.get('message', '')}"}
        data = body.get("data") or {}
        return {"ok": True, "auth_code": str(data.get("auth_code") or ""),
                "url": str(data.get("url") or "")}

    def poll_login(self, auth_code: str) -> Dict[str, Any]:
        """轮询扫码状态：pending / scanned / confirmed / expired。

        confirmed 附 cookies（SESSDATA/bili_jct/DedeUserID）与 mid——TV 协议
        cookie 直接在 JSON body 里，无需浏览器环境。
        """
        params = {"appkey": _TV_APPKEY, "auth_code": auth_code, "local_id": "0",
                  "ts": str(int(time.time()))}
        params["sign"] = _tv_sign(params)
        # ⚠ 轮询同样是签名表单 POST——GET 会 405（实盘 2026-11-08 验证）
        ret = self._http("POST", _TV_QR_POLL, data=params)
        body = ret.get("body") or {}
        code = body.get("code")
        if code == 0:
            data = body.get("data") or {}
            cookies = {str(c.get("name") or ""): str(c.get("value") or "")
                       for c in (data.get("cookie_info") or {}).get("cookies") or []}
            return {"status": "confirmed", "cookies": cookies,
                    "mid": str(data.get("mid") or "")}
        if code == 86090:
            return {"status": "scanned", "message": "已扫码，请在手机上确认"}
        if code in (86101, 86039):  # 86039=未确认（实盘返回码，与文档 86101 并存）
            return {"status": "pending", "message": "等待扫码确认"}
        if code == 86038:
            return {"status": "expired", "message": "二维码已过期"}
        return {"status": "pending", "message": f"code={code} {body.get('message', '')}"}

    def login_interactive(self, out: Any = None) -> int:
        """CLI 交互登录：终端二维码 → 轮询 → 存凭证。返回进程退出码。"""
        import sys
        import time as _t

        out = out or sys.stdout
        st = self.start_login()
        if not st.get("ok"):
            print(st.get("error", "发起登录失败"), file=out)
            return 1
        url = st["url"]
        print("用哔哩哔哩 App 扫描下面的二维码并确认登录（175 秒内有效）：", file=out)
        try:
            import segno

            # compact=True 半块字符：宽不变高减半；border=1 收边——默认输出太大糊满屏
            segno.make(url).terminal(out=out, border=1, compact=True)
        except ImportError:
            print("(终端二维码不可用：pip install segno)", file=out)
        print(f"（链接备用，可用手机浏览器打开登录：{url}）", file=out)
        deadline = _t.time() + 175
        while _t.time() < deadline:
            _t.sleep(3)
            r = self.poll_login(st["auth_code"])
            if r["status"] == "confirmed":
                cookies = r.get("cookies") or {}
                self._save_credentials(cookies, r.get("mid", ""))
                if r.get("mid") and not self.self_uid:
                    self.self_uid = r["mid"]
                who = self.whoami()
                print(f"登录成功：{who.get('uname', '')}（UID {who.get('mid') or r.get('mid', '')}）"
                      f"；凭证已存 {self.credentials_file}", file=out)
                return 0
            if r["status"] == "expired":
                print("二维码已过期，请重新运行 --login", file=out)
                return 1
            msg = r.get("message") or r["status"]
            print(f"  [{_t.strftime('%H:%M:%S')}] {msg}", file=out)
        print("等待超时，请重新运行 --login", file=out)
        return 1

    # ── HTTP（cookie 鉴权；风控识别在这里统一做） ───────────────────────────
    def _cookies(self) -> Dict[str, str]:
        c: Dict[str, str] = {}
        if self.sessdata:
            c["SESSDATA"] = self.sessdata
        if self.bili_jct:
            c["bili_jct"] = self.bili_jct
        if self.dedeuserid:
            c["DedeUserID"] = self.dedeuserid
        if not c:  # yaml/env 都没配 → 用 --login 存的凭证文件
            cred = self._load_credentials_file()
            for key, name in (("sessdata", "SESSDATA"), ("bili_jct", "bili_jct"),
                              ("dedeuserid", "DedeUserID")):
                if cred.get(key):
                    c[name] = cred[key]
        return c

    def _http(self, method: str, url: str, *, params: Any = None,
              data: Any = None, json_body: Any = None) -> Dict[str, Any]:
        """返回统一 {"http_status": int, "body": dict}；网络异常返回 {"code": -1}。"""
        import requests
        headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
        try:
            resp = requests.request(method, url, params=params, data=data, json=json_body,
                                    cookies=self._cookies(), headers=headers,
                                    timeout=self.timeout_sec)
        except Exception as e:
            logger.warning("bilibili 请求失败 %s %s: %s", method, url, e)
            return {"http_status": 0, "body": {"code": -1, "message": str(e)}}
        try:
            body = resp.json()
        except Exception:
            body = {"code": -1, "message": f"non-json {resp.status_code}"}
        return {"http_status": resp.status_code, "body": body}

    def _classify(self, ret: Dict[str, Any]) -> Optional[str]:
        """风控/凭证识别：返回暂停原因；None = 不暂停。"""
        body = ret.get("body") or {}
        code = body.get("code")
        if ret.get("http_status") in _RISK_HTTP or code in _RISK_CODES:
            return f"风控响应（http={ret.get('http_status')} code={code}）"
        if code in _CRED_CODES:
            return f"凭证失效（code={code}）：请更新 cookie 三件套"
        return None

    def _pause(self, reason: str) -> None:
        until = time.time() + self.risk_pause_minutes * 60
        with self._state_lock:
            st = self._load_state()
            st["paused_until"] = until
            st["paused_reason"] = reason
            self._save_state(st)
        logger.error("bilibili 通道暂停 %s 分钟：%s", self.risk_pause_minutes, reason)

    def _paused(self) -> Optional[str]:
        st = self._load_state()
        until = float(st.get("paused_until") or 0)
        if until > time.time():
            mins = int((until - time.time()) / 60) + 1
            return f"通道风控暂停中（剩 {mins} 分钟）：{st.get('paused_reason', '')}"
        return None

    # ── 四道闸（顺序固定：暂停 → 配额 → 黑名单 → 模式） ────────────────────
    def _gate_paused(self) -> Optional[str]:
        return self._paused()

    def _gate_quota(self, kind: str) -> Optional[str]:
        with self._state_lock:
            st = self._load_state()
            used = int(st.get(kind, 0))
            limit = self.quotas.get(kind, 0)
            if used >= limit:
                return f"今日{ '评论' if kind == 'comment' else '动态' }配额已用尽（{used}/{limit}），明天再来"
            return None

    def _consume_quota(self, kind: str) -> None:
        with self._state_lock:
            st = self._load_state()  # _load_state 已处理跨日翻账（保留冷却/暂停键）
            st[kind] = int(st.get(kind, 0)) + 1
            if kind == "dynamic":
                # 冷却闸记账：昨天也发→连发 streak+1；隔了天→重置为 1
                from datetime import date, timedelta

                yest = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
                if st.get("dyn_last_date") == self._today():
                    pass  # 今天第二条，streak 不变
                elif st.get("dyn_last_date") == yest:
                    st["dyn_streak"] = int(st.get("dyn_streak") or 0) + 1
                else:
                    st["dyn_streak"] = 1
                st["dyn_last_date"] = self._today()
            self._save_state(st)

    def _gate_cooldown(self, kind: str) -> Optional[str]:
        """活人感冷却闸：连发 N 天动态，今天强制歇一天（fail-closed，与配额并行）。"""
        if kind != "dynamic" or self.dynamic_cooldown_days <= 0:
            return None
        st = self._load_state()
        last = str(st.get("dyn_last_date") or "")
        streak = int(st.get("dyn_streak") or 0)
        if not last or streak < self.dynamic_cooldown_days:
            return None
        from datetime import date, datetime as _dt

        try:
            lastd = _dt.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            return None
        if lastd == date.today():  # 今天已发过：配额闸在管，这里不重复
            return None
        if (date.today() - lastd).days == 1:
            return (f"已连发 {streak} 天动态，今天鱼要休息一天"
                    f"（活人感冷却闸，明天解锁）")
        return None  # 已经隔了天，冷却结束

    def _gate_blacklist(self, text: str) -> Optional[str]:
        for w in self.blacklist:
            if w and w in text:
                return f"命中出站黑名单（{w[:2]}**），已拒发"
        return None

    def _gates(self, kind: str, text: str) -> Optional[str]:
        for err in (self._gate_paused(), self._gate_quota(kind),
                    self._gate_cooldown(kind), self._gate_blacklist(text)):
            if err:
                return err
        return None

    # ── 发送（ReplyChannel 全部收口到这里） ────────────────────────────────
    def send_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        chat_id = (chat_id or "").strip()
        text = (text or "").strip()
        kind, payload = self._resolve_target(chat_id)
        if kind is None:
            return {"ok": False, "error": payload}
        if not text:
            return {"ok": False, "error": "空文本不发送"}
        if kind == "comment" and len(text) > _COMMENT_MAX_CHARS:
            return {"ok": False, "error": f"评论超长（{len(text)} > {_COMMENT_MAX_CHARS}），请精简"}
        if self.mode in ("draft", "live") and not self._cookies():
            # 真发类模式未登录快速拒（dry_run 纯演练放行）：宁可明说，不带空 cookie
            # 去撞 API 的 -101 再触发「凭证失效」暂停
            return {"ok": False, "error": "bilibili 通道未登录：请先运行 --login（或配置 cookie 环境变量）"}

        if self.mode == "draft":
            # 草稿只排队不占配额（配额属于「真发」，在 --approve/live 时核）：
            # 提前过暂停/黑名单闸省一轮废稿，长度闸在上面已过
            err = self._gate_paused() or self._gate_blacklist(text)
            if err:
                return {"ok": False, "error": err}
            self._outbox_append(kind, chat_id, text)
            return {"ok": True, "queued": True, "chat_id": chat_id}

        err = self._gates(kind, text)  # dry_run/live 走全量四道闸
        if err:
            return {"ok": False, "error": err}

        if self.mode == "dry_run":
            logger.info("[bilibili dry_run] %s %s", chat_id, text[:80])
            return {"ok": True, "dry_run": True, "chat_id": chat_id}

        # live：真发；风控/凭证异常 → 暂停并拒发
        ret = self._post_send(kind, payload, text)
        reason = self._classify(ret)
        if reason:
            self._pause(reason)
            return {"ok": False, "error": f"发送被拒并触发通道暂停：{reason}"}
        body = ret.get("body") or {}
        if body.get("code") != 0:
            return {"ok": False, "error": f"bilibili API code={body.get('code')}: {body.get('message', '')}"}
        self._consume_quota(kind)
        out: Dict[str, Any] = {"ok": True, "chat_id": chat_id}
        if kind == "dynamic":
            # 发出的 dynamic_id 回传：首评/数据回流的锚点（模型据此自评或查询）
            data = body.get("data") or {}
            dyn_id = str(data.get("dynamic_id") or data.get("dyn_id") or "")
            if dyn_id:
                out["dynamic_id"] = dyn_id
        return out

    def _resolve_target(self, chat_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """chat_id → (kind, payload)。解析失败返回 (None, 错误文案)。"""
        if chat_id == "dynamic":
            return "dynamic", {}
        parts = chat_id.split(":")
        if parts[0] == "comment":
            if len(parts) != 5 or not all(parts[1:]):
                return None, f"comment 目标不完整: {chat_id}（应为 comment:<oid>:<type>:<root>:<parent>）"
            _, oid, rtype, root, parent = parts
            return "comment", {"oid": oid, "type": rtype, "root": root, "parent": parent}
        return None, (f"未知目标 chat_id: {chat_id}（应为 comment:<oid>:<type>:<root>:<parent> 或 dynamic）")

    def _csrf(self) -> str:
        """csrf=bili_jct：统一从 _cookies() 取（yaml/env/凭证文件三档已在其中收敛，
        不直读实例属性——否则凭证文件登录时 csrf 恒空，-111）。"""
        return self._cookies().get("bili_jct", "")

    def _post_send(self, kind: str, payload: Dict[str, Any], text: str) -> Dict[str, Any]:
        jct = self._csrf()
        if kind == "comment":
            data = {"oid": payload["oid"], "type": payload["type"],
                    "root": payload["root"], "parent": payload["parent"],
                    "message": text, "csrf": jct, "csrf_token": jct}
            return self._http("POST", _API_REPLY_ADD, data=data)
        # legacy dynamic_svr：type=4 纯文字动态，表单提交（csrf 双写兼容）
        return self._http("POST", _API_DYN_CREATE,
                          data={"type": 4, "rid": 0, "content": text,
                                "csrf_token": jct, "csrf": jct})

    # ── M2 图片通路：draw 上传 + 带图动态（写路径同样收口在适配器，过全量闸） ──
    def upload_image(self, image_path: str) -> Dict[str, Any]:
        """上传图片到B站图床（draw 接口），返回 {"ok", "image_url", "width", "height", "size"}。"""
        import os

        path = str(image_path or "")
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": f"图片不存在: {path}"}
        if os.path.getsize(path) > 20 * 1024 * 1024:
            return {"ok": False, "error": "图片超过 20MB 上限"}
        jct = self._csrf()
        url = "https://api.vc.bilibili.com/api/v1/drawImage/upload"
        with open(path, "rb") as f:
            files = {"file_up": (os.path.basename(path), f, "image/png")}
            data = {"biz": "new_dyn", "category": "daily", "csrf": jct, "csrf_token": jct}
            import requests

            try:
                resp = requests.post(url, files=files, data=data,
                                     cookies=self._cookies(),
                                     headers={"User-Agent": _UA,
                                              "Referer": "https://www.bilibili.com/"},
                                     timeout=self.timeout_sec * 3)
            except Exception as e:
                return {"ok": False, "error": f"上传失败: {e}"}
        try:
            body = resp.json()
        except Exception:
            return {"ok": False, "error": f"上传返回非 JSON ({resp.status_code})"}
        if body.get("code") != 0:
            return {"ok": False, "error": f"上传失败 code={body.get('code')}: {body.get('message', '')}"}
        d = body.get("data") or {}
        return {"ok": True, "image_url": d.get("image_url", ""),
                "width": d.get("image_width"), "height": d.get("image_height"),
                "size": d.get("img_size")}

    def send_image(self, chat_id: str, image_path: str, text: str = "") -> Dict[str, Any]:
        """带图动态（图片必须走 draw 上传；配额按 dynamic 计）。"""
        chat_id = (chat_id or "").strip()
        kind, payload = self._resolve_target(chat_id)
        if kind is None:
            return {"ok": False, "error": payload}
        if kind != "dynamic":
            return {"ok": False, "error": "带图发布目前仅支持 dynamic 目标"}
        text = (text or "").strip()
        if self.mode in ("draft", "live") and not self._cookies():
            return {"ok": False, "error": "bilibili 通道未登录：请先运行 --login"}
        if self.mode == "draft":
            err = self._gate_paused() or self._gate_blacklist(text)
            if err:
                return {"ok": False, "error": err}
            self._outbox_append("dynamic", chat_id, f"[图片:{image_path}] {text}".strip())
            return {"ok": True, "queued": True, "chat_id": chat_id}
        err = self._gates("dynamic", text)
        if err:
            return {"ok": False, "error": err}
        if self.mode == "dry_run":
            logger.info("[bilibili dry_run] image -> %s %s", chat_id, text[:60])
            return {"ok": True, "dry_run": True, "chat_id": chat_id}
        up = self.upload_image(image_path)
        if not up.get("ok"):
            return {"ok": False, "error": f"图片上传失败: {up.get('error', '')}"}
        import json as _json

        pics = _json.dumps([{"img_src": up["image_url"],
                             "img_width": up.get("width") or 0,
                             "img_height": up.get("height") or 0,
                             "img_size": str(up.get("size") or "")}])
        jct = self._csrf()
        ret = self._http("POST", _API_DYN_CREATE,
                         data={"type": 2, "rid": 0, "pics": pics,
                               "csrf_token": jct, "csrf": jct, "jsonp": "jsonp"})
        reason = self._classify(ret)
        if reason:
            self._pause(reason)
            return {"ok": False, "error": f"发送被拒并触发通道暂停：{reason}"}
        body = ret.get("body") or {}
        if body.get("code") != 0:
            return {"ok": False, "error": f"bilibili API code={body.get('code')}: {body.get('message', '')}"}
        self._consume_quota("dynamic")
        data = body.get("data") or {}
        return {"ok": True, "chat_id": chat_id,
                "dynamic_id": str(data.get("dynamic_id") or data.get("dyn_id") or "")}

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

    # ── 通知拉取与归一 ─────────────────────────────────────────────────────
    def fetch_reply_feed(self) -> Dict[str, Any]:
        # TODO_BILI_LIVE：分页游标（items 末尾 id）暂不做，单页足够「回复我的」频次
        return self._http("GET", _API_REPLY_FEED, params={"build": 0, "mobi_app": "web"})

    def parse_reply_feed(self, payload: Dict[str, Any], *, self_uid: str = "",
                         now: Optional[float] = None) -> List[Dict[str, Any]]:
        """通知 payload → 归一条目列表（宽容解析，结构漂移不炸）。

        每条：{key, chat_id, user_id, uname, text, ts}；无法定位评论位（oid/type/rpid）
        的条目丢弃并记日志——回不了目标的消息不如不接。
        """
        now = now if now is not None else time.time()
        items = (payload.get("data") or {}).get("items") or []
        out: List[Dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            uid = str(_deep_find(it, ("mid", "user_id", "uid"), depth=2) or "")
            if uid and self_uid and uid == str(self_uid):
                continue  # 自己的回声不接
            text = str(_deep_find(it, ("message", "content", "text"), depth=3) or "").strip()
            oid = _deep_find(it, ("oid", "subject_id"), depth=3)
            rpid = _deep_find(it, ("rpid", "reply_id", "root_rpid"), depth=3)
            rtype = _deep_find(it, ("type", "area_type"), depth=2)
            root = _deep_find(it, ("root", "root_rpid"), depth=3) or rpid
            ts = _deep_find(it, ("ctime", "reply_time", "time", "ts"), depth=3)
            try:
                ts = float(ts) if ts else None
            except (TypeError, ValueError):
                ts = None
            if not (oid and rpid and rtype):
                logger.debug("bilibili 通知缺评论定位（oid/rpid/type），丢弃: %s", str(it)[:200])
                continue
            out.append({
                "key": f"{oid}:{rtype}:{rpid}:{uid}:{int(ts or 0)}",
                "chat_id": f"comment:{oid}:{rtype}:{root}:{rpid}",
                "user_id": uid,
                "uname": str(_deep_find(it, ("uname", "nickname", "name"), depth=2) or uid),
                "text": text,
                "ts": ts if ts is not None else now,
            })
        return out

    def poll_once(self, on_message: Optional[OnMessage] = None) -> List[InboundMessage]:
        """拉一次通知 → 归一 → 过滤（新鲜度/去重）→ 派发。返回本轮派发的消息。"""
        if self._paused():
            logger.info("bilibili 通道暂停中，跳过本轮轮询")
            return []
        ret = self.fetch_reply_feed()
        reason = self._classify(ret)
        if reason:
            self._pause(reason)
            return []
        entries = self.parse_reply_feed(ret.get("body") or {}, self_uid=self.self_uid)
        baseline = not self._seen
        dispatched: List[InboundMessage] = []
        cutoff = time.time() - self.lookback_hours * 3600
        for e in entries:
            is_new = e["key"] not in self._seen
            self._mark_seen(e["key"])
            if baseline or not is_new:
                continue  # 首拍静默基线只记 seen（防历史灌入）；其余去重
            if e["ts"] < cutoff:
                continue
            msg = InboundMessage(
                channel="bilibili", chat_id=e["chat_id"], user_id=e["user_id"],
                # text 附场合说明（display_text 保持原话进历史）：先读再评的锚点
                text=(f"{e['text']}\n"
                      f"（来源：oid={e['chat_id'].split(':')[1]} 的评论区；"
                      f"回复前先用 bili_video_info/bili_video_comments 读内容与上下文）"),
                title=f"「{e['uname']}」的评论回复",
                display_text=e["text"], raw={"entry": e},
                allow_tools=self.allow_tools, reply_mode="explicit",
                speaker=f"{e['uname']}({e['user_id']})",
                reply=BilibiliReply(self, e["chat_id"]),
            )
            dispatched.append(msg)
            if on_message is not None:
                try:
                    on_message(msg)
                except Exception:
                    logger.exception("bilibili 入站派发回调异常")
        return dispatched

    # ── 凭证自检 ───────────────────────────────────────────────────────────
    def _wbi_mixin(self) -> str:
        """取（缓存 12h）当前 WBI mixin_key；key 随 B站下发轮换。"""
        cached = getattr(self, "_wbi_cache", None)
        now = time.time()
        if cached and now - cached[0] < 3600 * 12:
            return cached[1]
        import os

        ret = self._http("GET", _WBI_NAV)
        img = ((ret.get("body") or {}).get("data") or {}).get("wbi_img") or {}
        ik = os.path.basename(str(img.get("img_url") or "")).split(".")[0]
        sk = os.path.basename(str(img.get("sub_url") or "")).split(".")[0]
        mixin = _mixin_key(ik, sk) if (ik and sk) else ""
        self._wbi_cache = (now, mixin)
        return mixin

    def wbi_get(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """带 WBI 签名的 GET（动态流/热榜/搜索等现代读端点）。"""
        mixin = self._wbi_mixin()
        signed = _wbi_sign(dict(params), mixin, int(time.time())) if mixin else params
        return self._http("GET", url, params=signed)

    def whoami(self) -> Dict[str, Any]:
        ret = self._http("GET", _API_NAV)
        body = ret.get("body") or {}
        data = body.get("data") or {}
        if body.get("code") != 0 or not data.get("isLogin"):
            return {"ok": False, "error": f"cookie 无效或未登录（code={body.get('code')}）"}
        mid = str(data.get("mid") or "")
        if not self.self_uid:
            self.self_uid = mid
        return {"ok": True, "mid": mid, "uname": data.get("uname", "")}

    # ── ChannelAdapter 协议 ────────────────────────────────────────────────
    def ready(self) -> bool:
        """调度触发闸（scheduler._channel_gate duck-typing 可选项）：已登录才就绪。

        未登录时人设扮演 job（notify_channel=bilibili）会被调度器跳过并记
        runs.jsonl——「没登录就不触发扮演」是机械闸不是提示词约定。
        """
        return bool(self._cookies())

    def connect(self, on_message: OnMessage) -> None:
        self._load_seen()
        if not self._cookies():
            # 未登录：通道照常注册（ready()=False 让调度闸跳过 her job），但不轮询。
            # 发送侧 draft/live 会拿到明确的「未登录」报错而不是 API 的 -101。
            logger.warning("bilibili 通道未登录：先运行 python -m gateway.channels.bilibili --login，"
                           "登录后重启网关开始轮询")
            return
        if not self.self_uid:
            who = self.whoami()
            if not who.get("ok"):
                raise RuntimeError(f"bilibili 凭证自检失败：{who.get('error')}（也可直接配置 self_uid）")
            logger.info("bilibili 通道身份自取成功 uid=%s uname=%s", who.get("mid"), who.get("uname"))
        t = threading.Thread(target=self._poll_loop, args=(on_message,),
                             name="bilibili-poll", daemon=True)
        t.start()

    def _poll_loop(self, on_message: OnMessage) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once(on_message)
            except Exception:
                logger.exception("bilibili 轮询异常（下一轮重试）")
            self._stop.wait(self.poll_interval_sec + random.uniform(0, self.poll_jitter_sec))

    def reply_to(self, chat_id: str) -> ReplyChannel:
        return BilibiliReply(self, chat_id)

    def broadcast(self, text: str) -> Dict[str, Any]:
        if not self.default_chat_id:
            return {"ok": False, "error": "bilibili 未配置 default_chat_id（结果只落运行记录）"}
        return self.send_text(self.default_chat_id, text)

    def close(self) -> None:
        self._stop.set()


def _env(cfg: Dict[str, Any], key: str) -> str:
    import os
    return os.environ.get(str(cfg.get(f"{key}_env", f"BILI_{key.upper()}")), "")


def build(cfg: Dict[str, Any]) -> Optional["BilibiliAdapter"]:
    """工厂：enabled 才实例化（与 feishu/napcat 同构）。"""
    if not cfg.get("enabled", False):
        return None
    return BilibiliAdapter(cfg)


# ── 命令行：联调与土法审批（不依赖网关进程） ────────────────────────────────
def _main() -> int:
    ap = argparse.ArgumentParser(description="bilibili 通道联调/审批工具")
    ap.add_argument("--login", action="store_true",
                    help="TV 扫码登录：终端出二维码，App 确认后凭证存 state_dir")
    ap.add_argument("--whoami", action="store_true", help="校验 cookie 并打印身份")
    ap.add_argument("--poll-once", action="store_true", help="拉一次通知打印归一结果（不派发）")
    ap.add_argument("--outbox", action="store_true", help="列出待发草稿")
    ap.add_argument("--approve", type=int, metavar="N", help="批准发送第 N 条（1 起）草稿")
    ap.add_argument("--config-dir", default="", help="覆盖状态目录（默认按 yaml 的 state_dir）")
    args = ap.parse_args()

    from ..channel_gateway import channel_config
    cfg = channel_config("bilibili")
    if args.config_dir:
        cfg["state_dir"] = args.config_dir
    if not cfg.get("enabled", False):
        # CLI 是联调/审批工具，不依赖通道在网关里跑：enabled 只管网关自动拉起，
        # 这里降级为提醒继续执行（无配置文件时按缺省 dry_run + ~/.omniagent 状态目录跑）
        print("提示：channels/bilibili.yaml 未启用（enabled: false 或文件缺失）——"
              "登录/联调照常继续；要让网关拉起本通道请置 enabled: true 并重启网关")
    adapter = BilibiliAdapter(cfg)
    if args.login:
        return adapter.login_interactive()
    if args.whoami:
        print(json.dumps(adapter.whoami(), ensure_ascii=False))
        return 0
    if args.poll_once:
        ret = adapter.fetch_reply_feed()
        entries = adapter.parse_reply_feed(ret.get("body") or {}, self_uid=adapter.self_uid)
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    if args.outbox:
        for i, it in enumerate(adapter.outbox(), 1):
            mark = "✓" if it.get("status") == "sent" else "…"
            print(f"{i}. [{mark}] {it['kind']} → {it['chat_id']}\n   {it['text'][:120]}")
        return 0
    if args.approve is not None:
        ret = adapter.approve_item(args.approve)
        if ret.get("already_sent"):
            print("该条已发送")
            return 0
        print(json.dumps(ret.get("result", ret), ensure_ascii=False))
        return 0 if ret.get("ok") else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
