"""群自然存在感（group presence）离线测试。

不连网：presence 注入时钟 + 关闭防抖 Timer（直接驱动 _evaluate）；
napcat 接线用假事件流（同 test_napcat_channel.py 范式），判定日志重定向到 tmp。
"""
import json
import time

import pytest

from gateway import group_presence as gp_mod
from gateway.channels import napcat as mod
from gateway.channels.napcat import NapcatAdapter
from gateway.group_presence import GroupEvent, GroupPresence


def _ev(ts, uid="u1", speaker="张三", text="hi", **kw):
    return GroupEvent(ts=ts, group_id="g1", user_id=uid, speaker=speaker, text=text, **kw)


def _presence(tmp_path, cfg=None):
    """注入时钟 + 关 Timer 的 presence；state['now'] 手动推进。"""
    state = {"now": 1000.0}
    promoted = []
    p = GroupPresence(cfg or {}, lambda focus, wave: promoted.append((focus, wave)),
                      now=lambda: state["now"], log_path=tmp_path / "j.jsonl", timers=False)
    return p, promoted, state


def _judgments(tmp_path):
    path = tmp_path / "j.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# === 开关 ===

def test_disabled_by_default(tmp_path):
    p, _, _ = _presence(tmp_path)
    assert p.want("g1") is False


def test_explicit_enable(tmp_path):
    p, _, _ = _presence(tmp_path, {"enabled_groups": ["g1"]})
    assert p.want("g1") is True and p.want("g2") is False


# === 打分与阈值 ===

def test_high_score_promotes(tmp_path):
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "weights": {"intercept": 5.0}})
    p.feed(_ev(state["now"]))
    p._evaluate("g1")
    assert len(promoted) == 1
    focus, wave = promoted[0]
    assert focus.user_id == "u1" and len(wave) == 1


def test_low_score_stays_silent_and_logs(tmp_path):
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "weights": {"intercept": -10.0}})
    p.feed(_ev(state["now"]))
    p._evaluate("g1")
    assert promoted == []
    recs = _judgments(tmp_path)
    assert recs and recs[-1]["kind"] == "wave" and recs[-1]["promoted"] is False


def test_mention_alias_and_question_promote(tmp_path):
    """文本提及别名 + 疑问句越阈值；纯闲聊潜。"""
    base = {"enabled_groups": ["g1"], "talk_value": 0.7,
            "weights": {"log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, dict(base))
    p.feed(_ev(state["now"], text="随便聊聊"))
    p._evaluate("g1")
    assert promoted == []                      # z=-1.2 → prob≈0.23
    p2, promoted2, state2 = _presence(tmp_path, {**base, "aliases": ["小欧"]})
    p2.feed(_ev(state2["now"], text="小欧 这是啥？"))
    p2._evaluate("g1")
    assert len(promoted2) == 1                 # z=-1.2+1.5+0.5 → prob≈0.69


def test_at_other_suppresses(tmp_path):
    """@ 了别人 → 抑制（别去抢话）。"""
    cfg = {"enabled_groups": ["g1"], "talk_value": 0.7,
           "weights": {"log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, {**cfg, "aliases": ["小欧"]})
    p.feed(_ev(state["now"], text="小欧 你看", ats=frozenset({"999"})))
    p._evaluate("g1")
    assert promoted == []                      # -1.1 抢话惩罚
    p2, promoted2, state2 = _presence(tmp_path, {**cfg, "aliases": ["小欧"]})
    p2.feed(_ev(state2["now"], text="小欧 你看"))
    p2._evaluate("g1")
    assert len(promoted2) == 1


def test_placeholder_only_suppresses(tmp_path):
    cfg = {"enabled_groups": ["g1"], "talk_value": 0.7,
           "weights": {"log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, cfg)
    p.feed(_ev(state["now"], text="[图片]"))
    p._evaluate("g1")
    assert promoted == []                      # 纯图没话可接


def test_thread_hot_lowers_threshold(tmp_path):
    """自己刚发过言 → 线程热窗口内阈值下修一档，跟帖波次能接上。"""
    cfg = {"enabled_groups": ["g1"], "talk_value": 0.3,
           "weights": {"log_silence": 0.0, "pending_count": 0.0,
                       "mention_name": 0.0, "intercept": 0.2}}   # prob≈0.55
    p, promoted, state = _presence(tmp_path, cfg)
    p.feed(_ev(state["now"], text="嗨"))
    p._evaluate("g1")
    assert promoted == []                      # 冷：static 0.62 > 0.55
    state["now"] += 20.0                       # 跳出空闲退避
    p.note_own_speech("g1", "我说了一句")
    p.feed(_ev(state["now"], uid="u2", speaker="李四", text="接着聊"))
    p._evaluate("g1")
    assert len(promoted) == 1                  # 热：0.62×0.8=0.496 ≤ 0.55


# === 节制器 ===

def test_hourly_cap_blocks(tmp_path):
    cfg = {"enabled_groups": ["g1"], "hourly_cap": 2,
           "weights": {"intercept": 5.0, "log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, cfg)
    for i in range(3):
        state["now"] += 60.0
        p.feed(_ev(state["now"], uid=f"u{i}", text=f"m{i}"))
        p._evaluate("g1")
        if len(promoted) == i + 1:
            p.note_own_speech("g1", f"回{i}")   # 150s 内 → 记「模型说了」
    assert len(promoted) == 2
    recs = _judgments(tmp_path)
    assert recs[-1]["kind"] == "skip" and recs[-1]["reason"] == "hourly_cap"


def test_willingness_exhaustion_blocks(tmp_path):
    cfg = {"enabled_groups": ["g1"], "hourly_cap": 10,
           "weights": {"intercept": 5.0, "log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, cfg)
    for i in range(3):
        state["now"] += 1.0                     # 推进时钟：同刻消息会被候选集判重
        p.feed(_ev(state["now"], uid=f"u{i}", text=f"m{i}"))
        p._evaluate("g1")
        p.note_own_speech("g1", f"回{i}")       # 意愿 -0.34 ×3
    assert len(promoted) == 3
    state["now"] += 60.0
    p.feed(_ev(state["now"], uid="u9", text="再来"))
    p._evaluate("g1")
    assert len(promoted) == 3                   # 社交电量耗尽 → 低水位禁主动
    assert _judgments(tmp_path)[-1]["reason"] == "willing_low"


def test_consecutive_proactive_blocks_then_named_resets(tmp_path):
    cfg = {"enabled_groups": ["g1"], "hourly_cap": 10,
           "weights": {"intercept": 5.0, "log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, cfg)
    for i in range(3):
        state["now"] += 600.0                   # 拉开间隔：意愿回充不干扰本用例
        p.feed(_ev(state["now"], uid=f"u{i}", text=f"m{i}"))
        p._evaluate("g1")
        p.note_own_speech("g1", f"回{i}")
    assert len(promoted) == 3 and p._consecutive["g1"] == 3
    state["now"] += 600.0
    p.feed(_ev(state["now"], uid="u9", text="x"))
    p._evaluate("g1")
    assert len(promoted) == 3                   # 连续 3 次主动 → 禁
    p.note_named("g1")                          # 被点名重置计数
    p.feed(_ev(state["now"] + 1, uid="ua", text="y"))
    p._evaluate("g1")
    assert len(promoted) == 4


def test_quiet_hours_blocks(tmp_path):
    """安静时段不主动（时段覆盖当前小时，本地时区无关）。"""
    hour = time.localtime(time.time()).tm_hour
    promoted = []
    p = GroupPresence({"enabled_groups": ["g1"], "quiet_hours": f"{hour:02d}-{(hour + 1) % 24:02d}",
                       "weights": {"intercept": 5.0}},
                      lambda f, w: promoted.append((f, w)),
                      log_path=tmp_path / "j.jsonl", timers=False)
    p.feed(GroupEvent(ts=time.time(), group_id="g1", user_id="u1", speaker="s", text="x"))
    p._evaluate("g1")
    assert promoted == []


def test_backoff_and_backlog_bypass(tmp_path):
    """连续判潜 → 空闲退避；积压 ≥6 条旁路退避（群里吵起来了别睡）。"""
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "weights": {"intercept": -10.0}})
    p.feed(_ev(state["now"]))
    p._evaluate("g1")
    until = p._backoff_until["g1"]
    assert until > state["now"]
    state["now"] = until - 1
    p.feed(_ev(state["now"], uid="u2", text="x2"))
    p._evaluate("g1")
    recs = _judgments(tmp_path)
    assert recs[-1]["kind"] == "skip" and recs[-1]["reason"] == "backoff"
    for i in range(6):
        state["now"] += 1
        p.feed(_ev(state["now"], uid=f"u{i}", text=f"x{i}"))
    p._evaluate("g1")
    assert _judgments(tmp_path)[-1]["kind"] == "wave"   # 旁路退避，正常评估


def test_wave_delay_rules(tmp_path):
    """防抖：静默窗 / 同作者追加 / 纯媒体最低等待。"""
    p, _, _ = _presence(tmp_path, {"enabled_groups": ["g1"]})
    assert p._wave_delay_locked(None, _ev(0)) == 2.0
    assert p._wave_delay_locked(_ev(0, uid="a"), _ev(0, uid="a")) == 2.5
    assert p._wave_delay_locked(None, _ev(0, text="[图片]")) == 4.0


# === napcat 接线 ===

@pytest.fixture
def adapter(monkeypatch, tmp_path):
    """带 presence（判定权重恒过）的 napcat 适配器：不连网、日志进 tmp。"""
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    monkeypatch.setattr(gp_mod, "_DEFAULT_LOG", tmp_path / "j.jsonl")
    a = NapcatAdapter({"enabled": True,
                       "group_presence": {"enabled_groups": ["333"], "timers": False,
                                          "weights": {"intercept": 5.0}}})
    got = []
    a._on_message_cb = got.append
    monkeypatch.setattr(a, "group_name", lambda gid: "研发群")
    monkeypatch.setattr(a, "group_history", lambda gid, limit=8: [])
    monkeypatch.setattr(a, "_bot_nickname", lambda: "")
    return a, got


def _group_event(message_id, text, **extra):
    ev = {"post_type": "message", "message_type": "group", "self_id": 111,
          "user_id": 222, "group_id": 333, "message_id": message_id,
          "time": time.time(),
          "message": [{"type": "text", "data": {"text": text}}]}
    ev.update(extra)
    return ev


def test_wave_promotion_dispatches_explicit(adapter):
    a, got = adapter
    a._handle_event(_group_event(801, "有人聊聊吗"), lambda m: None)
    assert got == []                       # 未点名：不直接派发，进旁听缓冲
    a.presence._evaluate("333")
    assert len(got) == 1                   # 判定放行 → 升格
    msg = got[0]
    assert msg.chat_id == "group:333" and msg.origin == "wave"
    assert msg.reply_mode == "explicit"
    assert "没有人 @ 你" in msg.text and "msg_send_text" in msg.text
    assert "保持沉默" in msg.text          # 模型保留最终沉默权
    assert "有人聊聊吗" in msg.display_text  # 会话历史记波次原文
    assert msg.title == "「研发群」(333) 群里的对话"


def test_send_text_records_spoke(adapter, monkeypatch):
    a, got = adapter
    monkeypatch.setattr(a, "_api",
                        lambda action, payload: {"ok": True, "data": {"message_id": 1}})
    a._handle_event(_group_event(802, "聊聊"), lambda m: None)
    a.presence._evaluate("333")
    assert got and a.presence._promotions["333"]
    assert a.send_text("group:333", "我的回复")["ok"]
    kinds = [k for _, k, _ in a.presence._window["333"]]
    assert "sent" in kinds                 # 预算反馈：actual++ / 意愿消耗 / 连续 +1
    assert a.presence._consecutive["333"] == 1


def test_reply_to_bot_triggers_without_at(adapter, monkeypatch):
    a, got = adapter
    monkeypatch.setattr(a, "_api", lambda action, payload: {"ok": True, "data": {
        "user_id": 111, "sender": {"nickname": "机器人"},
        "message": [{"type": "text", "data": {"text": "明天开会"}}]}})
    a._self_id = "111"
    a._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 803, "time": time.time(),
        "message": [{"type": "reply", "data": {"id": 9}},
                    {"type": "text", "data": {"text": "几点？"}}],
    }, got.append)                              # 被点名消息走 on_message 参数
    assert len(got) == 1                   # 回复 bot 的消息 = 被点名（无需 @）
    msg = got[0]
    assert msg.origin == "named"
    assert "[引用] 机器人: 明天开会" in msg.text   # 引用展开：模型知道在接哪句
    assert "回复了你之前的消息" in msg.text
    assert "几点？" in msg.text


def test_reply_to_other_still_needs_at(adapter, monkeypatch):
    a, got = adapter
    monkeypatch.setattr(a, "_api", lambda action, payload: {"ok": True, "data": {
        "user_id": 999, "sender": {"nickname": "路人"},
        "message": [{"type": "text", "data": {"text": "原话"}}]}})
    a._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 804, "time": time.time(),
        "message": [{"type": "reply", "data": {"id": 10}},
                    {"type": "text", "data": {"text": "同意"}}],
    }, lambda m: None)
    assert got == []                       # 引用别人 ≠ 点名；进旁听缓冲等判定


def test_name_mention_triggers(monkeypatch, tmp_path):
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    monkeypatch.setattr(gp_mod, "_DEFAULT_LOG", tmp_path / "j.jsonl")
    a = NapcatAdapter({"enabled": True,
                       "group_presence": {"enabled_groups": ["333"], "timers": False,
                                          "aliases": ["小欧"]}})
    got = []
    a._on_message_cb = got.append
    monkeypatch.setattr(a, "group_name", lambda gid: "")
    monkeypatch.setattr(a, "group_history", lambda gid, limit=8: [])
    monkeypatch.setattr(a, "_api", lambda action, payload:
                        {"ok": True, "data": {"nickname": "欧酱"}}
                        if action == "get_login_info" else {"ok": False, "error": "x"})
    a._handle_event(_group_event(805, "小欧 在吗"), got.append)
    assert len(got) == 1                   # 文本点名（没打 @）
    assert "在消息里点名了你" in got[0].text


def test_rate_limit_drops(monkeypatch):
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    a = NapcatAdapter({"enabled": True})
    got = []
    for i in range(31):
        a._handle_event({"post_type": "message", "message_type": "private",
                         "self_id": 111, "user_id": 222, "message_id": 1000 + i,
                         "message": [{"type": "text", "data": {"text": f"m{i}"}}]},
                        got.append)
    assert len(got) == 30                  # 30 条/分钟，超限丢弃


def test_sanitize_name():
    f = mod.sanitize_name
    assert f("张三") == "张三"
    assert f("\x01<i>mg</i>&x") == "img/ix"  # 控制字符与标签字符剥离（斜杠无害保留）
    assert f("长" * 40) == "长" * 32       # 32 码点上限
