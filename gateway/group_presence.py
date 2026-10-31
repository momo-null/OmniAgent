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
- T2 节制器：意愿经济 / 连续抑制 / 空闲退避 / 硬闸，全部在打分前拦截。

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


def _static_threshold(talk_value: float) -> float:
    """预算窗样本不足（<20 波）时的静态分段阈值（冷启动）。"""
    if talk_value <= 0.2:
        return 0.75
    if talk_value <= 0.4:
        return 0.62
    if talk_value <= 0.6:
        return 0.52
    return 0.45


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
    message_id: str = ""
    reply_to: str = ""


@dataclass
class PresenceConfig:
    """``channels.<name>.group_presence`` 配置段的解析结果。"""
    enabled_groups: frozenset = frozenset()
    talk_value: float = 0.3           # 话痨度 0~1 → 每小时主动目标 1+5·tv 句
    quiet_sec: float = 2.0            # 防抖静默窗（新消息重置）
    same_author_extra: float = 0.5    # 同作者连发追加
    media_quiet_sec: float = 4.0      # 纯媒体消息最低等待
    batch_cap_sec: float = 8.0        # 单批总时长上限
    buffer_max_msgs: int = 12
    buffer_max_sec: float = 600.0
    thread_hot_min: float = 15.0      # 线程热窗口（自己发过言后的跟进期）
    hourly_cap: int = 3               # 每群每小时主动硬闸
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
        """每小时主动发言目标句数（talk_value 0~1 → 约 1~6 句）。"""
        return 1.0 + 5.0 * self.talk_value


class GroupPresence:
    """每群一份状态：旁听缓冲 + 防抖合并 + 结构打分 + 说话预算 + 节制器。"""

    #: 意愿经济 v0 参数：主动发言消耗 / 沉默回充（约 17 分钟回满）/ 低水位禁主动
    _WILL_COST = 0.34
    _WILL_REGEN_PER_SEC = 0.001
    _WILL_LOW = 0.15
    #: 升格与自己发言的关联窗口（此后发出的消息不再算该波的「模型说了」）
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

    def want(self, group_id: str) -> bool:
        """该群是否启用自然存在感（enabled_groups 显式开启，缺省全关）。"""
        return str(group_id) in self.cfg.enabled_groups

    # ── T1：旁听 + 判定 ──

    def feed(self, ev: GroupEvent) -> None:
        """群消息入缓冲并重排防抖 Timer（通道线程调用；被点名消息也喂，保上下文完整）。"""
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
        # 空闲退避：连续判「潜」时拉长下一次评估（积压旁路在 _evaluate 里做）
        backoff = self._backoff_until.get(group_id, 0.0)
        if backoff > self._now():
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
            candidates = [e for e in buf if not e.is_own and e.ts > last_eval]
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
        # 积压旁路退避：群里吵起来了（≥6 条未判定）就别睡了
        if self._backoff_until.get(group_id, 0.0) > now and len(candidates) < 6:
            return "skip", "backoff"
        window = self._window.setdefault(group_id, deque(maxlen=600))
        self._prune_locked(window, now)
        actual = sum(1 for _, k, _ in window if k == "sent")
        if actual >= self.cfg.hourly_cap:
            return "skip", "hourly_cap"
        if self._willingness_locked(group_id, now) < self._WILL_LOW:
            return "skip", "willing_low"
        if self._consecutive.get(group_id, 0) >= 3:
            return "skip", "consecutive"
        # 两次主动发言之间必须插有他人消息（我最后一条主动之后没有新发言 → 不追话）
        last_pro = self._last_proactive_ts.get(group_id, 0.0)
        if last_pro and all(e.ts <= last_pro for e in candidates):
            return "skip", "no_new_other_msg"
        return "pass", ""

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
        window = self._window.setdefault(group_id, deque(maxlen=600))
        self._prune_locked(window, now)
        samples = sum(1 for _, k, _ in window if k == "wave")
        base = (_static_threshold(self.cfg.talk_value) if samples < 20
                else self._budget_threshold_locked(group_id))
        hot = self._thread_hot(group_id, now)
        if hot:  # 线程热窗口：自己刚说过话的跟进期，阈值下修一档
            base = max(max(0.30, self.cfg.wave_floor - 0.12), base * 0.8)
        return _clamp(base, 0.05, 0.98)

    def _budget_threshold_locked(self, group_id: str) -> float:
        """说话预算门：这个群每小时想说的量（target）换算成动态阈值。

        keep_ratio = 前馈（预算减去被点名占用后，还付得起多少期望评分）
                   × 反馈（实际主动数超/欠目标的松紧）。
        付得起的占比越低 → 阈值越高（keep=0 时几乎闭嘴）；keep≥1 → 回到最低阈值。
        """
        window = self._window[group_id]
        target = self.cfg.target
        expected = sum(s for _, k, s in window if k == "wave")
        forced = sum(s for _, k, s in window if k == "named")
        actual = sum(1 for _, k, _ in window if k == "sent")
        feedforward = (target - forced) / max(expected, 1e-6)
        feedback = max(0.0, 1.0 + (target - actual) / max(1.0, target))
        keep = _clamp(feedforward, 0.0, 1.5) * _clamp(feedback, 0.0, 2.0)
        floor = self.cfg.wave_floor
        if keep >= 1.0:
            return floor
        return floor + (1.0 - keep) * (0.98 - floor)

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

    def note_own_speech(self, group_id: str, text: str = "") -> None:
        """自己往群里发了消息（任何来源：T0 回复 / T1 主动 / cron）。

        - 更新 last_own_ts（沉默时长与线程热窗口的基准），并把消息记入缓冲
          （is_own）——recent_self_ratio 与两次主动之间必须插他人消息的判定都靠它；
        - 与最近的升格关联（150s 内）→ 记「模型说了」：actual++、意愿消耗、
          连续主动计数 +1，供预算反馈与否则决率统计。
        """
        now = self._now()
        with self._lock:
            self._last_own_ts[group_id] = now
            buf = self._buffers.setdefault(group_id, deque(maxlen=self.cfg.buffer_max_msgs))
            buf.append(GroupEvent(ts=now, group_id=group_id, user_id=self._self_id,
                                  speaker="我", text=text or "", ats=frozenset(), is_own=True))
            self._trim_locked(group_id, now)
            promos = self._promotions.get(group_id)
            if promos and now - promos[-1] <= self._SPOKE_WINDOW_SEC:
                self._window.setdefault(group_id, deque(maxlen=600)).append((now, "sent", 1.0))
                self._willing[group_id] = max(
                    0.0, self._willingness_locked(group_id, now) - self._WILL_COST)
                self._consecutive[group_id] = self._consecutive.get(group_id, 0) + 1
                self._last_proactive_ts[group_id] = now
                self._log(group_id, {"kind": "outcome", "spoke": True,
                                     "latency": round(now - promos[-1], 1)})

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
