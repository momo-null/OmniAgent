"""群自然存在感（group presence）离线测试。

不连网：presence 注入时钟 + 关闭防抖 Timer（直接驱动 _evaluate）；
napcat 接线用假事件流（同 test_napcat_channel.py 范式），判定日志重定向到 tmp。
"""
import json
import time
from collections import deque

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
    cfg = {"enabled_groups": ["g1"], "talk_value": 0.3, "wave_floor": 0.62,
           "weights": {"log_silence": 0.0, "pending_count": 0.0,
                       "mention_name": 0.0, "intercept": 0.2}}   # prob≈0.55
    p, promoted, state = _presence(tmp_path, cfg)
    p.feed(_ev(state["now"], text="嗨"))
    p._evaluate("g1")
    assert promoted == []                      # 冷：wave_floor 0.62 > 0.55
    state["now"] += 20.0                       # 跳出空闲退避
    p.note_own_speech("g1", "我说了一句")
    state["now"] += 1.0                        # 他人接话在助手发言之后
    p.feed(_ev(state["now"], uid="u2", speaker="李四", text="接着聊"))
    p._evaluate("g1")
    assert len(promoted) == 1                  # 热：0.62×0.8=0.496 ≤ 0.55


def test_cold_start_falls_back_to_wave_floor(tmp_path):
    """OBS-1：低流量群（1h 波次远不到 20）不再被静态表钉死在 0.52。

    预算花不完时预算门回 ``wave_floor`` → 低流量群也能按 talk_value 节奏接话。
    """
    cfg = {"enabled_groups": ["g1"], "talk_value": 0.5, "wave_floor": 0.35,
           "weights": {"log_silence": 0.0, "pending_count": 0.0,
                       "mention_name": 0.0, "intercept": -0.4}}   # prob≈0.40
    p, promoted, state = _presence(tmp_path, cfg)
    p.feed(_ev(state["now"], text="有人吗"))
    p._evaluate("g1")
    assert len(promoted) == 1                  # 0.40 ≥ 0.35（旧的静态 0.52 会全拒）


# === 节制器 ===

def test_hourly_cap_blocks(tmp_path):
    cfg = {"enabled_groups": ["g1"], "hourly_cap": 2,
           "weights": {"intercept": 5.0, "log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, cfg)
    for i in range(3):
        state["now"] += 910.0                   # 拉开 > 热窗：每次发送都是新开话头
        p.feed(_ev(state["now"], uid=f"u{i}", text=f"m{i}"))
        p._evaluate("g1")
        if len(promoted) == i + 1:
            p.note_own_speech("g1", f"回{i}")   # 新线程 → 记 sent（线程计费）
    assert len(promoted) == 2
    recs = _judgments(tmp_path)
    assert recs[-1]["kind"] == "skip" and recs[-1]["reason"] == "hourly_cap"


def test_willingness_exhaustion_blocks(tmp_path):
    cfg = {"enabled_groups": ["g1"], "hourly_cap": 10, "thread_hot_min": 0.01,
           "weights": {"intercept": 5.0, "log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, cfg)
    for i in range(3):
        state["now"] += 1.0                     # 推进时钟：同刻消息会被候选集判重
        p.feed(_ev(state["now"], uid=f"u{i}", text=f"m{i}"))
        p._evaluate("g1")
        p.note_own_speech("g1", f"回{i}")       # 热窗外 → 新开话头：意愿 -0.34 ×3
    assert len(promoted) == 3
    state["now"] += 60.0
    p.feed(_ev(state["now"], uid="u9", text="再来"))
    p._evaluate("g1")
    assert len(promoted) == 3                   # 社交电量耗尽 → 低水位禁主动
    assert _judgments(tmp_path)[-1]["reason"] == "willing_low"


def test_consecutive_proactive_blocks_then_named_resets(tmp_path):
    cfg = {"enabled_groups": ["g1"], "hourly_cap": 20, "thread_hot_min": 0.01,
           "weights": {"intercept": 5.0, "log_silence": 0.0, "pending_count": 0.0}}
    p, promoted, state = _presence(tmp_path, cfg)
    for i in range(GroupPresence._CONSEC_LIMIT):
        state["now"] += 400.0                   # 拉开间隔：热窗外=新线程，回充盖过消耗
        p.feed(_ev(state["now"], uid=f"u{i}", text=f"m{i}"))
        p._evaluate("g1")
        p.note_own_speech("g1", f"回{i}")
    assert (len(promoted) == GroupPresence._CONSEC_LIMIT
            and p._consecutive["g1"] == GroupPresence._CONSEC_LIMIT)
    state["now"] += 400.0
    p.feed(_ev(state["now"], uid="u9", text="x"))
    p._evaluate("g1")
    assert len(promoted) == GroupPresence._CONSEC_LIMIT   # 连续开话到限 → 禁
    p.note_named("g1")                          # 被点名重置计数
    p.feed(_ev(state["now"] + 1, uid="ua", text="y"))
    p._evaluate("g1")
    assert len(promoted) == GroupPresence._CONSEC_LIMIT + 1


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
    assert "msg_stay_silent" in msg.text   # 旁听轮也要能显式收口「不发」
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


# === 类别总闸（reply_groups / reply_private）与 presence 身份同步 ===

def _private_event(message_id, text="hi"):
    return {"post_type": "message", "message_type": "private", "self_id": 111,
            "user_id": 222, "message_id": message_id, "time": time.time(),
            "message": [{"type": "text", "data": {"text": text}}]}


def test_reply_groups_off_skips_group_entirely(adapter):
    a, got = adapter
    a.reply_groups = False
    a._handle_event(_group_event(810, "随便聊聊"), lambda m: None)
    assert got == []                       # 不派发
    assert "333" not in a.presence._buffers  # 总闸在旁听缓冲之前：连喂都不喂
    a._handle_event(_private_event(811, "在吗"), got.append)
    assert len(got) == 1                   # 私聊不受群总闸连坐


def test_reply_private_off_skips_private(adapter):
    a, got = adapter
    a.reply_private = False
    a._handle_event(_private_event(812, "在吗"), got.append)
    assert got == []                       # 私聊整体忽略
    a._handle_event({                       # 群 @ 仍正常（reply_groups 总闸开着）
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 813, "time": time.time(),
        "message": [{"type": "at", "data": {"qq": 111}},
                    {"type": "text", "data": {"text": "在吗"}}],
    }, got.append)
    assert len(got) == 1


def test_self_info_synced_to_presence(adapter, monkeypatch):
    """P1 修复：通道拿到 self_id 时同步给 presence（at_other/mention_name/own 归属用）。"""
    a, got = adapter
    monkeypatch.setattr(a, "_bot_nickname", lambda: "小鱼")
    assert a.presence._self_id == ""
    a._handle_event(_group_event(814, "有人吗"), lambda m: None)
    assert a.presence._self_id == "111"
    assert "小鱼" in a.presence._bot_names  # 登录昵称进 mention_name 词表


# === P2：点名消息不重复升格 / P4：语音回复回注预算 ===

def test_named_event_excluded_from_wave_candidates(tmp_path):
    """named 事件只作上下文；同波的其他人消息照常评估。"""
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "weights": {"intercept": 5.0}})
    p.feed(_ev(state["now"], text="几点？", named=True))
    p._evaluate("g1")
    assert promoted == []                       # 权重恒过仍不升格：named 不作候选
    state["now"] += 1
    p.feed(_ev(state["now"], uid="u2", text="我也想知道"))
    p._evaluate("g1")
    assert len(promoted) == 1 and promoted[0][0].user_id == "u2"


def test_named_message_not_repromoted_as_wave(adapter):
    """T0 派发过的 @ 消息，防抖到点后不得再当波次提示一次（同一消息双回复）。"""
    a, got = adapter
    a._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 815, "time": time.time(),
        "message": [{"type": "at", "data": {"qq": 111}},
                    {"type": "text", "data": {"text": "几点了？"}}],
    }, got.append)
    assert len(got) == 1                        # T0 正常派发
    a.presence._evaluate("333")                 # fixture 权重恒过：若仍作候选必升格
    assert len(got) == 1                        # 不重复升格


# === T0 冷却 / 出站同文去重（2026-11-01 跨群刷屏实测） ===

def _at_event(message_id, text="在吗", user_id=222):
    return {"post_type": "message", "message_type": "group", "self_id": 111,
            "user_id": user_id, "group_id": 333, "message_id": message_id,
            "time": time.time(),
            "message": [{"type": "at", "data": {"qq": 111}},
                        {"type": "text", "data": {"text": text}}]}


def test_t0_dedup_cools_repeated_call(adapter):
    """同一人 90s 内用同一句话连点 → 只唤醒一次（防连点连回刷屏）。"""
    a, got = adapter
    a._handle_event(_at_event(901), got.append)
    a._handle_event(_at_event(902), got.append)
    assert len(got) == 1                        # 第二次冷却命中：不派发
    a._handle_event(_at_event(903, user_id=888), got.append)
    assert len(got) == 2                        # 换个人说同样的话不算重复
    a._handle_event(_at_event(904, text="几点？"), got.append)
    assert len(got) == 3                        # 换了内容照常唤醒


def test_duplicate_send_suppressed(adapter, monkeypatch):
    """出站同文去重：5 分钟内逐字相同的发言不再重发（自激回声防护）。"""
    a, _ = adapter
    calls = []

    def fake_api(action, payload):
        calls.append(action)
        return {"ok": True, "data": {"message_id": 1}}

    monkeypatch.setattr(a, "_api", fake_api)
    r1 = a.send_text("group:333", "主人这词我只有一个")
    r2 = a.send_text("group:333", "主人这词我只有一个")
    assert r1["ok"] and not r1.get("duplicate")
    assert r2["ok"] and r2.get("duplicate") is True
    assert len(calls) == 1                      # 只真发了一次


def test_duplicate_send_retry_after_failure(adapter, monkeypatch):
    """发送失败的文本不占去重位（留出重试余地）。"""
    a, _ = adapter
    monkeypatch.setattr(a, "_api", lambda action, payload: {"ok": False, "error": "boom"})
    assert a.send_text("group:333", "同一句")["ok"] is False
    monkeypatch.setattr(a, "_api",
                        lambda action, payload: {"ok": True, "data": {"message_id": 2}})
    r = a.send_text("group:333", "同一句")
    assert r["ok"] and not r.get("duplicate")


def test_send_voice_records_spoke(adapter, monkeypatch):
    """P4：群内语音回复同样回注（沉默基准/热窗口/预算），与 send_text 同口径。"""
    a, got = adapter
    monkeypatch.setattr(a, "_api",
                        lambda action, payload: {"ok": True, "data": {"message_id": 1}})
    monkeypatch.setattr("omni_core.tools.capability.get_capability",
                        lambda name: (lambda t: b"RIFFwav") if name == "voice_tts" else None)
    a._handle_event(_group_event(816, "聊聊"), lambda m: None)   # 非点名 → 旁听
    a.presence._evaluate("333")                                  # 升格（权重恒过）
    assert a.presence._promotions["333"]
    assert a.send_voice("group:333", "我的语音回复")["ok"]
    assert "sent" in [k for _, k, _ in a.presence._window["333"]]
    assert a.presence._consecutive["333"] == 1


# === 撞名避让 / 热窗口豁免退避 / 自我名片锚（2026-10-31 群试点实测） ===

def _fish_adapter(monkeypatch, tmp_path):
    """带别名「测试鱼」+ 恒过权重 + 关 Timer 的适配器。"""
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    monkeypatch.setattr(gp_mod, "_DEFAULT_LOG", tmp_path / "j.jsonl")
    a = NapcatAdapter({"enabled": True,
                       "group_presence": {"enabled_groups": ["333"], "timers": False,
                                          "aliases": ["测试鱼"],
                                          "weights": {"intercept": 5.0}}})
    a._on_message_cb = lambda m: None
    monkeypatch.setattr(a, "group_name", lambda gid: "")
    monkeypatch.setattr(a, "group_history", lambda gid, limit=8: [])
    return a


def test_name_mention_masks_other_members(monkeypatch, tmp_path):
    """他人完整名片含别名 → 不算点名；裸别名与自己的名片照常命中。"""
    a = _fish_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "_bot_nickname", lambda: "")
    monkeypatch.setattr(a, "_member_names",
                        lambda gid: {"1": "香辣测试鱼", "2": "蓝色测试鱼"})
    a._self_id = "2"
    assert a._name_mentioned("333", "香辣测试鱼唱一个") is False
    assert a._name_mentioned("333", "[引用] 香辣测试鱼: [语音] 就这水平也好意思发") is False
    assert a._name_mentioned("333", "测试鱼，在干嘛") is True       # 裸别名
    assert a._name_mentioned("333", "蓝色测试鱼出来") is True       # 自己名片不遮
    assert a._name_mentioned("333", "今天天气不错") is False


def test_mention_feature_masks_other_members(tmp_path):
    """presence mention 特征同款遮蔽：没注入他人名片时不遮（退化兼容）。"""
    p, _, state = _presence(tmp_path, {"enabled_groups": ["g1"], "aliases": ["测试鱼"]})
    ev = _ev(state["now"], text="香辣测试鱼唱一个")
    _, feats = p._score_locked("g1", [ev], state["now"])
    assert feats["mention_name"] == 1.0                             # 未避让：子串误命中
    p.set_other_names("g1", ["香辣测试鱼"])
    _, feats = p._score_locked("g1", [ev], state["now"])
    assert feats["mention_name"] == 0.0                             # 避让后不算被点名


def test_name_mention_allows_exact_same_name(monkeypatch, tmp_path):
    """与别名完全同名的其他成员不遮（2026-10-31 实测：三个名片就叫「测试鱼」的
    bot 在群里，裸名是唯一呼叫形式，全遮 = 助手聋了）；严格超集照遮。"""
    a = _fish_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(a, "_bot_nickname", lambda: "")
    monkeypatch.setattr(a, "_member_names",
                        lambda gid: {"1": "测试鱼", "2": "蓝色测试鱼", "3": "香辣测试鱼"})
    a._self_id = "2"
    assert a._name_mentioned("333", "测试鱼，在干嘛") is True       # 同名裸名不遮
    assert a._name_mentioned("333", "香辣测试鱼唱一个") is False    # 超集照遮


def test_mention_feature_allows_exact_same_name(tmp_path):
    """presence mention 特征同规则：同名裸名不遮（加分），超集遮。"""
    p, _, state = _presence(tmp_path, {"enabled_groups": ["g1"], "aliases": ["测试鱼"]})
    p.set_other_names("g1", ["测试鱼", "香辣测试鱼"])
    _, feats = p._score_locked("g1", [_ev(state["now"], text="测试鱼，在干嘛")], state["now"])
    assert feats["mention_name"] == 1.0
    _, feats = p._score_locked("g1", [_ev(state["now"], text="香辣测试鱼唱一个")], state["now"])
    assert feats["mention_name"] == 0.0


def test_thread_continuation_free_of_charge(tmp_path):
    """线程经济：新开话头计费；热窗内接话续聊免费；点名回复免费。"""
    p, _, state = _presence(tmp_path, {"enabled_groups": ["g1"], "aliases": ["测试鱼"]})

    def sent() -> int:
        return sum(1 for _, k, _ in p._window.get("g1", ()) if k == "sent")

    p.note_own_speech("g1", "开场")                                 # 新开话头：计费
    assert sent() == 1
    state["now"] += 60.0
    p.note_own_speech("g1", "接话")                                 # 热窗内续聊：免费
    assert sent() == 1
    state["now"] += 60.0
    p.note_own_speech("g1", "点名回复", proactive=False)             # 被叫醒的发言：免费
    assert sent() == 1
    state["now"] += 1000.0                                          # 超过热窗（5min）
    p.note_own_speech("g1", "另起话头")                              # 再开话头：计费
    assert sent() == 2


def test_named_reply_does_not_make_next_proactive_free(tmp_path):
    """预算门：被点名回复不替后续主动发言买单（2026-11-01 跨群刷屏根因）。

    旧版按「最近说过话」判续聊免费，被点名回复也刷新它 → 点名密集的群里热窗永不
    冷却，主动发言全免费（实测某试点群 4 小时主动量超目标 3 倍、outcome 无 start）。
    """
    p, _, state = _presence(tmp_path, {"enabled_groups": ["g1"]})

    def sent() -> int:
        return sum(1 for _, k, _ in p._window.get("g1", ()) if k == "sent")

    p.note_named("g1")
    p.note_own_speech("g1", "被点名回复", proactive=False)     # 被叫醒的发言：免费
    state["now"] += 60.0
    p.note_own_speech("g1", "自己插一句", proactive=True)        # 不在助手开的线程里：计费
    assert sent() == 1


def test_at_other_owned_blocks_new_thread(tmp_path):
    """@ 归属：有人 @ 了别的成员且被 @ 者已应答 → 对话归他们，助手不开新话头。"""
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "aliases": ["测试鱼"],
                                              "weights": {"intercept": 5.0}})
    p.set_self_info("bot1", "测试鱼")
    # 张三 @ 王五 → 王五应答（他们俩的对话进行中）
    p.feed(GroupEvent(ts=state["now"], group_id="g1", user_id="u1", speaker="张三",
                      text="没了？", ats=frozenset({"u2"})))
    p.feed(GroupEvent(ts=state["now"] + 1, group_id="g1", user_id="u2", speaker="王五",
                      text="管饭的", ats=frozenset()))
    state["now"] += 3.0
    p.feed(_ev(state["now"], uid="u9", text="随便聊聊"))
    p._evaluate("g1")
    assert not promoted
    assert _judgments(tmp_path)[-1]["reason"] == "at_other_thread"


def test_at_other_unanswered_or_self_not_owned(tmp_path):
    """@ 了没人应、或 @ 的是助手自己 → 不算归属他人，照常判定。"""
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "aliases": ["测试鱼"],
                                              "weights": {"intercept": 5.0}})
    p.set_self_info("bot1", "测试鱼")
    # 张三 @ 王五，王五不应答
    p.feed(GroupEvent(ts=state["now"], group_id="g1", user_id="u1", speaker="张三",
                      text="在吗", ats=frozenset({"u2"})))
    state["now"] += 3.0
    p.feed(_ev(state["now"], uid="u9", text="大家聊"))
    p._evaluate("g1")
    assert len(promoted) == 1                     # 叫了没人理 → 大众话头
    # 李四 @ 助手（bot1）→ 对话归助手
    p.feed(GroupEvent(ts=state["now"] + 1, group_id="g1", user_id="u3", speaker="李四",
                      text="hello", ats=frozenset({"bot1"})))
    state["now"] += 3.0
    p.feed(_ev(state["now"], uid="u9", text="继续"))
    p._evaluate("g1")
    assert len(promoted) == 2


def test_at_other_owned_hot_thread_exempt(tmp_path):
    """热窗续聊豁免：助手自己的线程跟进不因第三者 @ 他人而中断。"""
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "aliases": ["测试鱼"],
                                              "weights": {"intercept": 5.0}})
    p.set_self_info("bot1", "测试鱼")
    p.feed(_ev(state["now"], uid="u1", text="开场"))
    p._evaluate("g1")
    assert promoted
    p.note_own_speech("g1", "我说")               # 热窗口打开
    # 助手发言之后，张三 @ 王五且王五应答（旁边开新对话）
    p.feed(GroupEvent(ts=state["now"] + 1, group_id="g1", user_id="u1", speaker="张三",
                      text="问你", ats=frozenset({"u2"})))
    p.feed(GroupEvent(ts=state["now"] + 2, group_id="g1", user_id="u2", speaker="王五",
                      text="答了", ats=frozenset()))
    state["now"] += 3.0
    p.feed(_ev(state["now"], uid="u1", text="接着我的话说"))
    before = len(promoted)
    p._evaluate("g1")
    assert len(promoted) == before + 1            # 热窗续聊不被归属规则掐断


def test_budget_gate_quantile_topk(tmp_path):
    """预算门 v2：阈值 = 第 k 高波次分（k≈剩余线程预算）→ 恰好放行最值得接的 k 波；
    预算耗尽抬到 0.98；被点名（named）不挤占主动预算。"""
    p, _, state = _presence(tmp_path, {"enabled_groups": ["g1"], "talk_value": 0.3,
                                       "wave_floor": 0.2})          # target = 3
    now = state["now"]
    probs_desc = [round(0.8 - i * 0.03, 4) for i in range(20)]       # 0.80, 0.77, 0.74, …
    with p._lock:   # 预填 20 波越过冷启动，直进预算门
        for i, s in enumerate(probs_desc):
            p._window.setdefault("g1", deque(maxlen=600)).append((now - 100 - i, "wave", s))
    assert p._threshold_locked("g1", now) == pytest.approx(probs_desc[2])  # 第 3 高 → 恰 3 波可过
    with p._lock:   # 开满 3 个话头 → 预算耗尽
        for _ in range(3):
            p._window["g1"].append((now, "sent", 1.0))
    assert p._threshold_locked("g1", now) == 0.98
    with p._lock:   # 被点名再多也不改变 remaining（T0 不挤占 T1 预算）
        for i in range(5):
            p._window["g1"].append((now - 10 - i, "named", 0.9))
    assert p._threshold_locked("g1", now) == 0.98


def test_hot_window_bypasses_backoff(tmp_path):
    """热窗口豁免：自己刚说过话时退避不再压住评估（活跃期每波都判）。"""
    p, promoted, state = _presence(tmp_path, {"enabled_groups": ["g1"],
                                              "weights": {"intercept": 5.0}})
    p.feed(_ev(state["now"], text="x"))
    p._evaluate("g1")
    assert promoted                                                 # 升格（恒过权重）
    p.note_own_speech("g1", "我说了")                                # 热窗口打开
    p._backoff_until["g1"] = state["now"] + 300                     # 人为制造退避
    p._backoff_n["g1"] = 3
    state["now"] += 1
    p.feed(_ev(state["now"], uid="u2", text="y"))
    before = len(promoted)
    p._evaluate("g1")
    assert len(promoted) == before + 1                              # 退避没掐住活跃窗


def test_group_prompt_carries_self_card(adapter, monkeypatch):
    """身份锚：T0 prompt 告诉模型自己在本群的群名片（分得清哪条鱼是自己）。"""
    a, got = adapter
    monkeypatch.setattr(a, "_bot_nickname", lambda: "蓝色测试鱼")
    monkeypatch.setattr(a, "_member_names",
                        lambda gid: {"111": "蓝色测试鱼", "222": "张三"})
    a._self_id = "111"
    a._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 830, "time": time.time(),
        "message": [{"type": "at", "data": {"qq": 111}},
                    {"type": "text", "data": {"text": "在吗"}}],
    }, got.append)
    assert any("群名片是「蓝色测试鱼」" in m.text for m in got)


def test_wake_origin_drives_proactive_flag(adapter, monkeypatch):
    """点名唤醒 → 随后回复不占主动预算；波次升格 → 占（线程计费由 presence 判）。"""
    a, got = adapter
    monkeypatch.setattr(a, "_bot_nickname", lambda: "蓝色测试鱼")
    monkeypatch.setattr(a, "_member_names", lambda gid: {"111": "蓝色测试鱼"})
    a._self_id = "111"
    a._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 831, "time": time.time(),
        "message": [{"type": "at", "data": {"qq": 111}},
                    {"type": "text", "data": {"text": "在吗"}}],
    }, got.append)
    assert got and a._wake_origin["group:333"][0] == "named"
    assert a._proactive_speech("group:333") is False
    a._wake_origin["group:333"] = ("wave", time.time())
    assert a._proactive_speech("group:333") is True
    assert a._proactive_speech("group:999") is True     # 无来源默认按主动（保守）


def test_voice_mode_dice_and_sticky(adapter):
    """语音模式：ratio 边界确定；热窗内粘住本轮实际模式；出热窗重新掷骰。"""
    a, _ = adapter
    a._voice_reply_ratio = 1.0
    assert a._group_voice_mode("group:333") is True
    a._voice_reply_ratio = 0.0
    assert a._group_voice_mode("group:333") is False
    a._voice_reply_ratio = 0.5
    a._voice_mode_state["group:333"] = (True, time.time())      # 本轮发过语音
    assert a._group_voice_mode("group:333") is True             # 热窗内粘住
    a._voice_mode_state["group:333"] = (False, time.time())     # 本轮发过文字
    assert a._group_voice_mode("group:333") is False
    a._voice_mode_state["group:333"] = (True, time.time() - 2000.0)  # 出热窗
    assert a._group_voice_mode("group:333") in (True, False)    # 重新掷骰（不报错即继续）


def test_group_note_carries_voice_mode(adapter, monkeypatch):
    """场合自述带本轮回复模式句：ratio=1 → 语音，0 → 文字（T0 与波次同款）。"""
    a, got = adapter
    monkeypatch.setattr(a, "_member_names", lambda gid: {"111": "蓝色测试鱼"})
    a._self_id = "111"

    def _at_event(mid, text):
        return {"post_type": "message", "message_type": "group", "self_id": 111,
                "user_id": 222, "group_id": 333, "message_id": mid, "time": time.time(),
                "message": [{"type": "at", "data": {"qq": 111}},
                            {"type": "text", "data": {"text": text}}]}

    a._voice_reply_ratio = 1.0
    a._handle_event(_at_event(832, "在吗"), got.append)
    assert any("这轮回复用语音" in m.text for m in got)
    a._voice_reply_ratio = 0.0
    a._voice_mode_state.clear()
    a._handle_event(_at_event(833, "又来了"), got.append)
    assert any("这轮回复用文字" in m.text for m in got)


# === P2：最近日记注入已下线（2026-11-02 plan memory-recall P6：职责归 L2 导航的日记 summary） ===


def test_chat_routes_exact_beats_channel(monkeypatch):
    """路由：精确键优先于通道通配，会话 ID 与 chat 绑定。"""
    from gateway import chat_routes

    monkeypatch.setattr(chat_routes, "load_routes", lambda: {
        "napcat:*": {"project_id": "qq"},
        "napcat:group:9": {"project_id": "qq-g9"},
    })
    assert chat_routes.resolve("napcat", "group:9")[0] == "qq-g9"
    assert chat_routes.resolve("napcat", "group:8")[0] == "qq"
    sid = chat_routes.resolve("napcat", "group:8")[1]
    assert sid.startswith("s_napcat_") and sid == chat_routes.resolve("napcat", "group:8")[1]
    assert chat_routes.resolve_project("feishu", "oc_x") == "default"   # 未配置 → 兜底


def test_peer_note_no_longer_injected_into_group_prompt(adapter, monkeypatch, tmp_path):
    """关系卡改「可检索资产」（plan personal-agent-memory NP-2/D-2）：通道自动注入
    已物理删除——场合自述不再出现印象卡正文；「记得这个人」改由 narrative_search
    /narrative_read 按需自取（工具层出口约束见 test_narrative_tool）。"""
    from omni_core.local import runtime_paths as rp

    a, got = adapter
    rel = tmp_path / "relations"
    rel.mkdir()
    (rel / "222.md").write_text(
        "-----META-START-----\nname: 张三\nuid: 222\n-----META-END-----\n", encoding="utf-8")
    monkeypatch.setattr(rp, "global_relations", lambda: rel)

    a._handle_event(_at_event(851, "在吗", user_id=222), got.append)
    assert not any("关于这个人的印象" in m.text or "警惕" in m.text for m in got)
    import pytest as _pytest
    _pytest.raises(AttributeError, getattr, a, "_peer_note")   # 零痕迹删除


def test_send_records_voice_mode_state(adapter, monkeypatch):
    """发送成功回写语音模式状态：文字 False / 语音 True（线程粘性的依据）。"""
    a, _ = adapter
    monkeypatch.setattr(a, "_api",
                        lambda action, payload: {"ok": True, "data": {"message_id": 1}})
    monkeypatch.setattr("omni_core.tools.capability.get_capability",
                        lambda name: (lambda t: b"RIFFwav") if name == "voice_tts" else None)
    a.send_text("group:333", "文字回复")
    assert a._voice_mode_state["group:333"][0] is False
    assert a.send_voice("group:333", "语音回复")["ok"]
    assert a._voice_mode_state["group:333"][0] is True


def test_bare_name_ignores_quote_line(adapter, monkeypatch):
    """裸名点名只看正文：引用别的同名鱼的消息不唤醒（回复助手本人仍走 quoted_self）。"""
    a, got = adapter
    monkeypatch.setattr(a, "_member_names", lambda gid: {"111": "蓝色测试鱼"})
    monkeypatch.setattr(a, "_gp_aliases", ("测试鱼",))
    a._self_id = "111"

    def fake_api(action, payload):
        if action == "get_msg":
            if str((payload or {}).get("message_id")) == "11101":   # 助手本人的消息
                return {"ok": True, "data": {"user_id": 111,
                                             "message": [{"type": "text",
                                                          "data": {"text": "我说的"}}],
                                             "sender": {"user_id": 111, "card": "蓝色测试鱼",
                                                        "nickname": ""}}}
            return {"ok": True, "data": {"user_id": 999,
                                         "message": [{"type": "text",
                                                      "data": {"text": "群里今天真热闹"}}],
                                         "sender": {"user_id": 999, "card": "测试鱼",
                                                    "nickname": ""}}}
        return {"ok": False}

    monkeypatch.setattr(a, "_api", fake_api)

    def _reply_event(mid, body, quote_id):
        return {"post_type": "message", "message_type": "group", "self_id": 111,
                "user_id": 222, "group_id": 333, "message_id": mid, "time": time.time(),
                "message": [{"type": "reply", "data": {"id": quote_id}},
                            {"type": "text", "data": {"text": body}}]}

    a._handle_event(_reply_event(850, "那你来", 555), got.append)
    assert got == []                               # 引用行里的「测试鱼」不算点名
    a._handle_event(_reply_event(851, "测试鱼你来", 556), got.append)
    assert len(got) == 1                           # 正文点名照常唤醒
    a._handle_event(_reply_event(852, "说得对", 11101), got.append)
    assert len(got) == 2                           # 引用助手本人（quoted_self）仍唤醒


def test_group_gates_layered(adapter, monkeypatch):
    """闸门分层：allow_groups=响应范围（T0+T1 外层，空=不限）；
    enabled_groups=主动参与白名单（只管 T1 内层）。"""
    a, got = adapter
    monkeypatch.setattr(a, "_member_names", lambda gid: {"111": "蓝色测试鱼"})
    a._self_id = "111"

    def _at_event(gid, mid):
        return {"post_type": "message", "message_type": "group", "self_id": 111,
                "user_id": 222, "group_id": gid, "message_id": mid, "time": time.time(),
                "message": [{"type": "at", "data": {"qq": 111}},
                            {"type": "text", "data": {"text": "在吗"}}]}

    a._handle_event(_at_event(444, 840), got.append)
    assert got                                     # allow_groups 空=不限：@ 仍应答
    assert not a.presence._buffers.get("444")      # 但不进旁听缓冲（T1 白名单外）
    a._handle_event(_at_event(333, 841), got.append)
    assert a.presence._buffers.get("333")          # 启用群：点名入缓冲+派发
    assert len(got) == 2


def test_named_reply_quotes_trigger(adapter, monkeypatch):
    """回应点名带引用段（多鱼同群标明在回谁）：文本/语音都挂；波次/cron 不挂。"""
    a, got = adapter
    calls = []

    def fake_api(action, payload):
        calls.append((action, payload))
        return {"ok": True, "data": {"message_id": 1}}

    monkeypatch.setattr(a, "_api", fake_api)
    monkeypatch.setattr("omni_core.tools.capability.get_capability",
                        lambda name: (lambda t: b"RIFFwav") if name == "voice_tts" else None)
    monkeypatch.setattr(a, "_member_names", lambda gid: {"111": "蓝色测试鱼"})
    a._self_id = "111"
    a._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 834, "time": time.time(),
        "message": [{"type": "at", "data": {"qq": 111}},
                    {"type": "text", "data": {"text": "在吗"}}],
    }, got.append)
    assert got and a._wake_origin["group:333"][0] == "named"
    # 文本回复：首条带引用段，id=触发消息
    a.send_text("group:333", "来了")
    segs = calls[-1][1]["message"]
    assert segs[0] == {"type": "reply", "data": {"id": "834"}}
    assert segs[1] == {"type": "text", "data": {"text": "来了"}}
    # 语音不带引用段（QQ「引用+语音」显示 bug/风控风险）：只有 record
    assert a.send_voice("group:333", "语音不引用")["ok"]
    vsegs = calls[-1][1]["message"]
    assert [s["type"] for s in vsegs] == ["record"]
    # 波次升格来源 → 不带引用
    a._wake_origin["group:333"] = ("wave", time.time(), "")
    a.send_text("group:333", "主动插话")
    assert calls[-1][1]["message"][0]["type"] == "text"
    # 点名来源过期（>10min）→ 也不带
    a._wake_origin["group:333"] = ("named", time.time() - 700.0, "834")
    a.send_text("group:333", "迟到发言")
    assert calls[-1][1]["message"][0]["type"] == "text"


def test_member_names_cached(adapter, monkeypatch):
    """成员名片：card 优先昵称兜底；TTL 内只拉一次。"""
    a, _ = adapter
    calls = []

    def fake_api(action, payload):
        calls.append(action)
        return {"ok": True, "data": [{"user_id": 111, "card": "蓝色测试鱼"},
                                     {"user_id": 999, "nickname": "香辣测试鱼"}]}

    monkeypatch.setattr(a, "_api", fake_api)
    n1 = a._member_names("333")
    n2 = a._member_names("333")
    assert n1["111"] == "蓝色测试鱼" and n1["999"] == "香辣测试鱼"
    assert calls.count("get_group_member_list") == 1
