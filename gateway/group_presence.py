"""群聊自然存在感（Group Presence）——通道无关的「开口判定」层。

设计：doc/plans/group-presence-2026-10-30.md。核心原则：**「要不要说话」与「说什么」
分离**——本模块只回答前者，纯结构特征打分 + 说话预算（零 LLM、微秒级、可解释、可热
调参）；判定通过的波次经 promote 回调升格为普通入站消息走既有网关链路，模型保留最终
沉默权（explicit 通道不调 msg_send_* 就什么都不发）。

定位与纪律：开口判定是 ``require_at`` 的模糊化——无状态倾向、fail-silent（判错永远
往安静倒）、只做「这条消息归不归我管」的概率判定；不写记忆、不发消息、不做语义理解，
因此是通道层的门，不是第二大脑。升格路径与 @ 路径完全同链，不造平行管道。

三档：
- T0 被点名：通道侧确定性触发（@/引用回复/文本点名），**不经本模块判定**；
- T1 旁听波次：非点名群消息进 WaveBuffer → 防抖合并 → 结构打分 → 预算门 → 升格；
- T2 节制器：意愿经济（按「开话线程」计费：接话续聊与被点名回复免费）/ 连续抑制 /
  空闲退避 / 硬闸，全部在打分前拦截。

线程模型：feed/note_* 由通道线程调用，_evaluate 由防抖 Timer 线程调用——单锁保护
全部状态（操作均为微秒级内存操作）；promote 回调在锁外执行，通道自行组包并调
on_message（网关入队是线程安全的 queue.Queue）。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Tuple

from utils import get_logger

logger = get_logger("gateway.group_presence")

#: 判定日志（JSONL，每波一条）：重拟合权重 / 校准预算 / 监控否决率的数据来源（plan §7）
_DEFAULT_LOG = Path.home() / ".omniagent" / "presence" / "judgments.jsonl"

#: 整条消息只由占位符组成（图/表情/转发等，无实义文本）→ 打分抑制
_PLACEHOLDER_RE = re.compile(r"^(?:\[[^\[\]]{1,8}\])+$")

#: 结构特征回归打分的 v0 权重（量级占位）——上线后由自家判定日志重拟合替换
_WEIGHT_V0 = {
    "intercept": -1.2,
    "mention_name": 1.5,       # 正文提及 bot 名/别名
    "at_other": -1.1,          # @ 了别人（别去抢话）
    "has_question": 0.5,       # 疑问句
    "placeholder_only": -1.0,  # 纯媒体，没话可接
    "recent_self_ratio": 2.0,  # 最近 5 条里自己发言占比（对话进行中则延续）
    "recent_count": -0.04,     # 窗口内消息条数（刷屏越猛越闭嘴）
    "log_silence": 0.05,       # log1p(距上次发言秒数)（潜水越久越可能冒泡）
    "pending_count": 0.1,      # 本波积压条数
}


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _is_placeholder_only(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and bool(_PLACEHOLDER_RE.match(t))


@dataclass(frozen=True)
class GroupEvent:
    """通道归一化后的群消息（喂给旁听缓冲的最小事件）。"""
    ts: float
    group_id: str
    user_id: str
    speaker: str                      # 已净化显示名
    text: str                         # 文本投影（媒体为占位符）
    ats: frozenset = frozenset()
    is_own: bool = False
    named: bool = False               # T0 点名消息：只作上下文，不作波次候选（防重复提示）
    message_id: str = ""
    reply_to: str = ""


@dataclass
class PresenceConfig:
    """``channels.<name>.group_presence`` 配置段的解析结果。"""
    enabled_groups: frozenset = frozenset()
    talk_value: float = 0.3           # 活跃度 0~1 → 每小时主动开话目标 = 10·tv 个对话线程
    quiet_sec: float = 2.0            # 防抖静默窗（新消息重置）
    same_author_extra: float = 0.5    # 同作者连发追加
    media_quiet_sec: float = 4.0      # 纯媒体消息最低等待
    batch_cap_sec: float = 8.0        # 单批总时长上限
    buffer_max_msgs: int = 12
    buffer_max_sec: float = 600.0
    thread_hot_min: float = 15.0      # 线程热窗口（自己发过言后的跟进期）
    hourly_cap: int = 3               # 每群每小时新开对话线程硬闸（接话续聊不计）
    quiet_hours: str = ""             # "01-08" 本地时间，安静时段不主动
    aliases: Tuple[str, ...] = ()     # 文本点名/mention 特征用的别名表
    wave_floor: float = 0.45          # 预算充足时的最低放行阈值
    backoff_base: float = 5.0         # 空闲退避基数（×2ⁿ）
    backoff_cap: float = 300.0
    weights: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_cfg(cls, cfg: Dict) -> "PresenceConfig":
        cfg = cfg or {}
        return cls(
            enabled_groups=frozenset(str(g) for g in (cfg.get("enabled_groups") or [])),
            talk_value=_clamp(float(cfg.get("talk_value", 0.3) or 0.0), 0.0, 1.0),
            quiet_sec=max(0.2, float(cfg.get("quiet_sec", 2.0))),
            same_author_extra=float(cfg.get("same_author_extra", 0.5)),
            media_quiet_sec=float(cfg.get("media_quiet_sec", 4.0)),
            batch_cap_sec=max(1.0, float(cfg.get("batch_cap_sec", 8.0))),
            buffer_max_msgs=max(6, int(cfg.get("buffer_max_msgs", 12))),
            buffer_max_sec=float(cfg.get("buffer_max_sec", 600.0)),
            thread_hot_min=float(cfg.get("thread_hot_min", 15.0)),
            hourly_cap=int(cfg.get("hourly_cap", 3)),
            quiet_hours=str(cfg.get("quiet_hours") or ""),
            aliases=tuple(str(a).strip() for a in (cfg.get("aliases") or []) if str(a).strip()),
            wave_floor=_clamp(float(cfg.get("wave_floor", 0.45)), 0.05, 0.9),
            backoff_base=max(1.0, float(cfg.get("backoff_base", 5.0))),
            backoff_cap=float(cfg.get("backoff_cap", 300.0)),
            weights=dict(cfg.get("weights") or {}),
        )

    @property
    def target(self) -> float:
        """每小时主动开话/入场目标线程数（talk_value 0~1 → 0~10 个；接话续聊不计）。"""
        return 10.0 * self.talk_value


class GroupPresence:
    """每群一份状态：旁听缓冲 + 防抖合并 + 结构打分 + 说话预算 + 节制器。"""

    #: 意愿经济参数：每次主动「开话/入场」消耗 / 沉默回充（约 17 分钟回满，恰 sustain
    #: ~10 线程/小时）/ 低水位禁主动。接话续聊与被点名回复不扣。
    _WILL_COST = 0.34
    _WILL_REGEN_PER_SEC = 0.001
    _WILL_LOW = 0.15
    #: 连续开话守卫：未被点名期间最多连开 N 个新话头（防连环搭话；被点名即清零）
    _CONSEC_LIMIT = 8
    #: @ 归属窗：最近 N 秒内有人 @ 了别的成员**且被 @ 者已应答** → 对话归他们，
    #: 她不开新话头（2026-10-31 实测：@了别的成员的问答被她插话）。热窗续聊豁免。
    _AT_OWNED_SEC = 180.0
    #: 升格与自己发言的关联窗口（outcome 日志里的 latency 观测口径）
    _SPOKE_WINDOW_SEC = 150.0
    #: 预算窗统计口径（1 小时滑动）
    _WINDOW_SEC = 3600.0

    def __init__(self, cfg: Dict, promote: Callable[[GroupEvent, List[GroupEvent]], None], *,
                 now: Optional[Callable[[], float]] = None,
                 log_path: Optional[Path] = None, timers: bool = True):
        self.cfg = PresenceConfig.from_cfg(cfg)
        self._promote = promote
        self._now = now or time.time
        self._timers_enabled = bool(timers)   # 测试可关（手动调 _evaluate）
        self._log_path = Path(log_path) if log_path else _DEFAULT_LOG
        self._lock = threading.Lock()
        self._buffers: Dict[str, Deque[GroupEvent]] = {}
        self._timers: Dict[str, threading.Timer] = {}
        self._batch_start: Dict[str, float] = {}
        self._last_eval_ts: Dict[str, float] = {}
        self._self_id = ""
        self._bot_names: Tuple[str, ...] = self.cfg.aliases
        # 节制器状态（per group）
        self._last_own_ts: Dict[str, float] = {}
        self._willing: Dict[str, float] = {}
        self._willing_ts: Dict[str, float] = {}
        self._consecutive: Dict[str, int] = {}      # 自上次被点名以来的连续主动发言数
        self._last_proactive_ts: Dict[str, float] = {}
        self._backoff_until: Dict[str, float] = {}
        self._backoff_n: Dict[str, int] = {}
        # 撞名避让（通道注入的其他成员显示名，per group）：mention 匹配时先遮蔽
        self._other_names: Dict[str, Tuple[str, ...]] = {}
        # 1h 预算窗：(ts, kind, score)，kind ∈ wave（已评估）/ named（被点名 0.9）/ sent（主动 1.0）
        self._window: Dict[str, Deque[Tuple[float, str, float]]] = {}
        self._promotions: Dict[str, Deque[float]] = {}

    # ── 通道注入 ──

    def set_self_info(self, self_id: str, nickname: str = "") -> None:
        """通道把 bot 身份给进来（mention_name 特征与 at_other 判定用）。"""
        with self._lock:
            if self_id:
                self._self_id = str(self_id)
            names = [n for n in (nickname, *self.cfg.aliases) if n]
            self._bot_names = tuple(dict.fromkeys(names))

    def set_other_names(self, group_id: str, names) -> None:
        """通道注入该群其他成员的显示名（撞名避让：mention 特征匹配时遮蔽）。

        纯匹配辅助，通道无关——通道决定给不给；不给则退化为现行为。只收
        「包含 bot 名字」之外的过滤由打分处做（这里不假设 bot 名字已就位）。
        """
        clean = tuple(dict.fromkeys(str(n).strip() for n in (names or ()) if str(n).strip()))
        if not clean:
            return
        with self._lock:
            self._other_names[group_id] = clean

    def want(self, group_id: str) -> bool:
        """该群是否启用自然存在感（enabled_groups 显式开启，缺省全关）。"""
        return str(group_id) in self.cfg.enabled_groups

    # ── T1：旁听 + 判定 ──

    def feed(self, ev: GroupEvent) -> None:
        """群消息入缓冲并重排防抖 Timer（通道线程调用）。

        被点名消息也喂（named=True）：留在缓冲里供 recent_* 特征与后续波次的
        上下文完整性，但**不作为波次候选**——点名已直通主链，再升格等于同一条
        消息提示模型两次（轻则注入自相矛盾，重则双重回复）。
        """
        now = self._now()
        with self._lock:
            buf = self._buffers.setdefault(ev.group_id, deque(maxlen=self.cfg.buffer_max_msgs))
            prev = buf[-1] if buf else None
            buf.append(ev)
            self._trim_locked(ev.group_id, now)
            if not self._timers_enabled:
                return
            delay = self._wave_delay_locked(prev, ev)
            start = self._batch_start.get(ev.group_id)
            if start is None or now - start > self.cfg.batch_cap_sec:
                start = now
                self._batch_start[ev.group_id] = start
            deadline = min(now + delay, start + self.cfg.batch_cap_sec)
            self._arm_locked(ev.group_id, max(0.05, deadline - now))

    def _wave_delay_locked(self, prev: Optional[GroupEvent], ev: GroupEvent) -> float:
        """本条消息把决策时点推到多久之后（防抖合并器）。"""
        delay = self.cfg.quiet_sec
        if prev is not None and prev.user_id == ev.user_id:
            delay += self.cfg.same_author_extra   # 同作者连发：再等等，可能还有下一条
        if _is_placeholder_only(ev.text):
            delay = max(delay, self.cfg.media_quiet_sec)   # 看图/表情需要时间
        return delay

    def _arm_locked(self, group_id: str, delay: float) -> None:
        old = self._timers.pop(group_id, None)
        if old is not None:
            old.cancel()
        # 空闲退避：连续判「潜」时拉长下一次评估（积压旁路在 _evaluate 里做）。
        # 热窗口豁免（2026-10-31）：自己刚说过话的跟进期内不退避——活跃期里
        # 每波都按时评估，说不说仍由热阈值/预算门/硬闸管（退避不掐活跃窗）。
        backoff = self._backoff_until.get(group_id, 0.0)
        if backoff > self._now() and not self._thread_hot(group_id, self._now()):
            delay = max(delay, backoff - self._now())
        t = threading.Timer(delay, self._evaluate, args=(group_id,))
        t.daemon = True
        self._timers[group_id] = t
        t.start()

    def _evaluate(self, group_id: str) -> None:
        """防抖到点：对本波做一次开口判定（Timer 线程；测试可直接调）。"""
        now = self._now()
        promote_out: Optional[Tuple[GroupEvent, List[GroupEvent]]] = None
        with self._lock:
            self._timers.pop(group_id, None)
            buf = self._buffers.get(group_id)
            if not buf:
                return
            last_eval = self._last_eval_ts.get(group_id, 0.0)
            # 波次候选 = 他人且非点名的新消息；is_own 与 named 只作上下文
            candidates = [e for e in buf if not e.is_own and not e.named
                          and e.ts > last_eval]
            if not candidates:
                return
            verdict, reason = self._govern_locked(group_id, candidates, now)
            if verdict != "pass":
                self._skip_locked(group_id, candidates, now, reason)
                return
            prob, feats = self._score_locked(group_id, candidates, now)
            threshold = self._threshold_locked(group_id, now)
            ok = prob >= threshold
            window = self._window.setdefault(group_id, deque(maxlen=600))
            window.append((now, "wave", prob))
            self._log(group_id, {"kind": "wave", "features": feats,
                                 "prob": round(prob, 4), "threshold": round(threshold, 4),
                                 "promoted": ok, "pending": len(candidates)})
            self._last_eval_ts[group_id] = max(e.ts for e in candidates)
            if ok:
                self._backoff_n[group_id] = 0
                self._backoff_until[group_id] = 0.0
                self._promotions.setdefault(group_id, deque(maxlen=64)).append(now)
                promote_out = (candidates[-1], list(candidates))
            else:
                self._enter_backoff_locked(group_id, now)
        if promote_out is not None:
            try:
                self._promote(promote_out[0], promote_out[1])
            except Exception as e:  # noqa: BLE001 —— 升格失败不影响通道主流程
                logger.warning("波次升格回调失败（忽略）: %s: %s", type(e).__name__, e)

    def _skip_locked(self, group_id: str, candidates: List[GroupEvent],
                     now: float, reason: str) -> None:
        self._log(group_id, {"kind": "skip", "reason": reason, "pending": len(candidates)})
        self._last_eval_ts[group_id] = max(e.ts for e in candidates)
        self._enter_backoff_locked(group_id, now)

    def _enter_backoff_locked(self, group_id: str, now: float) -> None:
        n = self._backoff_n.get(group_id, 0) + 1
        self._backoff_n[group_id] = n
        self._backoff_until[group_id] = now + min(
            self.cfg.backoff_base * (2 ** min(n, 8)), self.cfg.backoff_cap)

    # ── T2：节制器（打分前的硬拦截） ──

    def _govern_locked(self, group_id: str, candidates: List[GroupEvent],
                       now: float) -> Tuple[str, str]:
        if self._in_quiet_hours(now):
            return "skip", "quiet_hours"
        hot = self._thread_hot(group_id, now)
        # 积压旁路退避：群里吵起来了（≥6 条未判定）就别睡了。
        # 热窗口豁免：跟进期内每波都判（退避不掐活跃窗，见 _arm_locked 同款豁免）。
        if not hot and self._backoff_until.get(group_id, 0.0) > now and len(candidates) < 6:
            return "skip", "backoff"
        # @ 归属：别人点名的对话（被 @ 者已应答）不主动掺和——她只接大众话头。
        # 热窗豁免：自己线程的跟进不因第三者 @ 他人而中断。
        if not hot and self._at_other_owned_locked(group_id, now):
            return "skip", "at_other_thread"
        window = self._window.setdefault(group_id, deque(maxlen=600))
        self._prune_locked(window, now)
        actual = sum(1 for _, k, _ in window if k == "sent")
        if actual >= self.cfg.hourly_cap:
            return "skip", "hourly_cap"
        if self._willingness_locked(group_id, now) < self._WILL_LOW:
            return "skip", "willing_low"
        if self._consecutive.get(group_id, 0) >= self._CONSEC_LIMIT:
            return "skip", "consecutive"
        # 两次主动发言之间必须插有他人消息（我最后一条主动之后没有新发言 → 不追话）
        last_pro = self._last_proactive_ts.get(group_id, 0.0)
        if last_pro and all(e.ts <= last_pro for e in candidates):
            return "skip", "no_new_other_msg"
        return "pass", ""

    def _at_other_owned_locked(self, group_id: str, now: float) -> bool:
        """最近有人 @ 了别的成员、且被 @ 者已应答 → 对话归他们（她不主动掺和）。

        2026-10-31 实测：某成员 @另一成员 → 被点名者回答 → 她以热窗低阈值
        升格插话——@ 是对话归属信号，被点名者的应答期内她只旁观。@ 自己/@全体
        不算归属他人；被 @ 者一直不应答（叫了没人理）不算。
        """
        events = [e for e in self._buffers.get(group_id, ())
                  if now - e.ts <= self._AT_OWNED_SEC]
        called = {a for e in events for a in e.ats
                  if a and a not in ("all", self._self_id)}
        if not called:
            return False
        return any(e.user_id in called for e in events)

    def _willingness_locked(self, group_id: str, now: float) -> float:
        w = self._willing.get(group_id, 1.0)
        ts = self._willing_ts.get(group_id)
        if ts is not None:
            w = min(1.0, w + (now - ts) * self._WILL_REGEN_PER_SEC)
        self._willing[group_id] = w
        self._willing_ts[group_id] = now
        return w

    def _in_quiet_hours(self, now: float) -> bool:
        span = (self.cfg.quiet_hours or "").strip()
        if not span or "-" not in span:
            return False
        try:
            a, b = (int(x) for x in span.split("-", 1))
            hour = time.localtime(now).tm_hour
        except ValueError:
            return False
        return (a <= hour < b) if a <= b else (hour >= a or hour < b)

    # ── 打分与阈值 ──

    def _score_locked(self, group_id: str, candidates: List[GroupEvent],
                      now: float) -> Tuple[float, Dict[str, float]]:
        w = {**_WEIGHT_V0, **self.cfg.weights}
        recent = [e for e in self._buffers.get(group_id, ()) if now - e.ts <= 300.0]
        recent_text = recent[-5:]
        text_blob = "".join(e.text for e in candidates)
        # 撞名避让：他人完整名片**严格包含** bot 别名时先遮掉再匹配 mention——
        # 群里多只同名 agent 时，提到别的成员不该算我们被点名（2026-10-31 实测）。
        # 与 bot 名字**完全同名**的裸名不遮：多个同名 agent 时裸名仍可能是喊我，
        # 让 mention 特征加分、由模型结合身份锚自己分辨。
        for other in self._other_names.get(group_id, ()):
            if (any(n and n in other for n in self._bot_names)
                    and not any(n and n == other for n in self._bot_names)):
                text_blob = text_blob.replace(other, "\uFFFD" * len(other))
        last_own = self._last_own_ts.get(group_id, 0.0)
        silence = max(0.0, now - last_own) if last_own else 3600.0
        feats = {
            "mention_name": 1.0 if any(n and n in text_blob for n in self._bot_names) else 0.0,
            "at_other": 1.0 if any(e.ats - {"all", self._self_id} for e in candidates) else 0.0,
            "has_question": 1.0 if ("?" in text_blob or "？" in text_blob) else 0.0,
            "placeholder_only": 1.0 if all(_is_placeholder_only(e.text) for e in candidates) else 0.0,
            "recent_self_ratio": (sum(1 for e in recent_text if e.is_own) / 5.0),
            "recent_count": float(len(recent)),
            "log_silence": math.log1p(silence),
            "pending_count": float(len(candidates)),
        }
        z = w["intercept"] + sum(wk * feats[k] for k, wk in w.items() if k != "intercept")
        return _sigmoid(z), {k: round(v, 3) for k, v in feats.items()}

    def _threshold_locked(self, group_id: str, now: float) -> float:
        """阈值 = 预算门分位阈值（**任何**样本量都用它，OBS-1 已修）。

        旧版在 1 小时波次样本 <20 时退到静态分段表（tv=0.5 → 0.52）。低流量群
        永远凑不满 20 波，等于被钉死在 0.52；而 v0 权重的实际波次分中位数只有
        0.21–0.23（p90 约 0.33），于是低流量群 T1 结构性全灭（2026-11-01 实测
        试点群一天 10 次升格 / 456 波）。预算门本身已覆盖冷启动：预算花不完时
        ``_budget_threshold_locked`` 直接回 ``wave_floor``（"样本不够就回到最低
        可放行线"），不需要第二套阈值表。
        """
        window = self._window.setdefault(group_id, deque(maxlen=600))
        self._prune_locked(window, now)
        base = self._budget_threshold_locked(group_id)
        hot = self._thread_hot(group_id, now)
        if hot:  # 线程热窗口：自己刚说过话的跟进期，阈值下修一档
            base = max(max(0.30, self.cfg.wave_floor - 0.12), base * 0.8)
        return _clamp(base, 0.05, 0.98)

    def _budget_threshold_locked(self, group_id: str) -> float:
        """说话预算门 v2（线程经济 + 分位数 top-K）。

        target = 每小时想开的新话头数（10·tv）。actual 只数已开的线程（note_own_speech
        的 start 事件；接话续聊与被点名回复不记 sent），remaining = target − actual，
        阈值取最近一小时波次分降序的**第 k 高**（k≈remaining）→ 恰好放行当下最值得
        接的 k 波。旧公式用 Σ全部波次概率当分母，活跃群（45+ 波/小时）里分母恒远超
        target，阈值被顶到 0.78+，主动搭话结构性不可能（2026-10-31 试点实测 0/195）。
        """
        window = self._window.setdefault(group_id, deque(maxlen=600))
        actual = sum(1 for _, k, _ in window if k == "sent")
        remaining = _clamp(self.cfg.target - actual, 0.0, self.cfg.target)
        if remaining <= 0.05:
            return 0.98  # 预算耗尽：几乎必拒（撞名/点名级超高分波仍可能过）
        probs = sorted((s for _, k, s in window if k == "wave"), reverse=True)
        k = int(remaining)
        if k >= len(probs):
            return self.cfg.wave_floor  # 波次太少：预算花不完，回到最低阈值
        return max(self.cfg.wave_floor, probs[k - 1])

    def _thread_hot(self, group_id: str, now: float) -> bool:
        last = self._last_own_ts.get(group_id, 0.0)
        return bool(last) and (now - last) <= self.cfg.thread_hot_min * 60.0

    @staticmethod
    def _prune_locked(window: Deque, now: float) -> None:
        while window and now - window[0][0] > GroupPresence._WINDOW_SEC:
            window.popleft()

    # ── 通道回注（T0 与出站事实） ──

    def note_named(self, group_id: str) -> None:
        """T0 被点名（@/引用回复/文本点名）：计入预算占用、重置连续主动与退避。"""
        now = self._now()
        with self._lock:
            self._window.setdefault(group_id, deque(maxlen=600)).append((now, "named", 0.9))
            self._consecutive[group_id] = 0
            self._backoff_n[group_id] = 0
            self._backoff_until[group_id] = 0.0

    def note_own_speech(self, group_id: str, text: str = "", proactive: bool = True) -> None:
        """自己往群里发了消息（任何来源：T0 回复 / T1 主动 / cron）。

        - 无论来源都更新 last_own_ts（沉默时长、线程热窗、退避豁免的基准）并把消息
          记入缓冲（is_own）——recent_self_ratio 与「两次开话之间必须插他人消息」
          的判定都靠它；
        - 预算按**对话线程**计：``proactive=False``（点名回复这类被叫醒的发言，
          通道传入）不占预算；``proactive=True`` 且距上次**自己开的话头**超过线程
          热窗 = 新开话头 → 记 sent、扣意愿、连续开话 +1；她自己开的线程内接话续聊
          免费。
        - **免费续聊只看 ``_last_proactive_ts``（她主动开的线程），不看"最近说过
          话"**：旧版用 ``_last_own_ts`` 判定，被点名回复也会刷新它 → 点名密集的
          群里热窗永不冷却，每一次主动发言都被判成「续聊免费」，预算门与 hourly_cap
          整体失效（实测：某点名密集的试点群 4 小时主动发言量超目标 3 倍、
          outcome 全是 continue/named 而没有 start）。被点名回复本身仍然免费，
          只是不再替后续主动发言买单。
        """
        now = self._now()
        with self._lock:
            self._last_own_ts[group_id] = now
            buf = self._buffers.setdefault(group_id, deque(maxlen=self.cfg.buffer_max_msgs))
            buf.append(GroupEvent(ts=now, group_id=group_id, user_id=self._self_id,
                                  speaker="我", text=text or "", ats=frozenset(), is_own=True))
            self._trim_locked(group_id, now)
            promos = self._promotions.get(group_id)
            if not proactive:
                self._log(group_id, {"kind": "outcome", "spoke": True, "thread": "named"})
                return
            prev_pro = self._last_proactive_ts.get(group_id, 0.0)
            if prev_pro and now - prev_pro <= self.cfg.thread_hot_min * 60.0:
                # 她自己开的话头内接话：不重复计费
                self._log(group_id, {"kind": "outcome", "spoke": True, "thread": "continue"})
                return
            self._window.setdefault(group_id, deque(maxlen=600)).append((now, "sent", 1.0))
            self._willing[group_id] = max(
                0.0, self._willingness_locked(group_id, now) - self._WILL_COST)
            self._consecutive[group_id] = self._consecutive.get(group_id, 0) + 1
            self._last_proactive_ts[group_id] = now
            self._log(group_id, {"kind": "outcome", "spoke": True, "thread": "start",
                                 "latency": (round(now - promos[-1], 1)
                                              if promos else None)})

    def shutdown(self) -> None:
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()

    # ── 内部 ──

    def _trim_locked(self, group_id: str, now: float) -> None:
        buf = self._buffers.get(group_id)
        if not buf:
            return
        cutoff = now - self.cfg.buffer_max_sec
        while buf and buf[0].ts < cutoff:
            buf.popleft()

    def _log(self, group_id: str, payload: Dict) -> None:
        """判定日志：尽力而为，失败静默（观测不可拖垮主流程）。"""
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": round(self._now(), 3), "group": str(group_id), **payload}
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
