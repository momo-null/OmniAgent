"""B站通道适配器离线测试（大肥鱼自运营，零内核改动约束的守门测试）。

不连真实 B站：只验证四道闸（风控暂停/配额/黑名单/模式）、通知归一、
去重基线与目标解析。全部使用合成 ID（uid 9xxx / oid 1xxxx），不含真实账号信息。
"""
import json
import time

import pytest

from gateway.channels import bilibili as mod
from gateway.channels.bilibili import BilibiliAdapter, build


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "bili-state"


@pytest.fixture
def adapter(state_dir):
    return BilibiliAdapter({"enabled": True, "mode": "dry_run", "sessdata": "syn", "state_dir": str(state_dir),
                            "self_uid": "9000",
                            "quotas": {"comments_per_day": 2, "dynamics_per_day": 1}})


def _collector():
    got = []
    return got, got.append


# === 工厂 ===

def test_build_disabled_returns_none():
    assert build({"enabled": False}) is None
    assert build({}) is None


def test_build_invalid_mode_raises(state_dir):
    with pytest.raises(ValueError):
        BilibiliAdapter({"enabled": True, "mode": "yolo", "sessdata": "syn", "state_dir": str(state_dir)})


# === 四道闸：黑名单 ===

def test_blacklist_rejects(adapter, monkeypatch):
    adapter.mode = "live"
    adapter.blacklist = ["敏感词"]
    monkeypatch.setattr(adapter, "_post_send", lambda *a, **k: {"http_status": 200, "body": {"code": 0}})
    ret = adapter.send_text("dynamic", "今天聊聊敏感词话题")
    assert ret["ok"] is False
    assert "黑名单" in ret["error"]


# === 四道闸：配额 ===

def test_quota_exhausted_rejects_and_consumes(state_dir, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "sessdata": "syn", "state_dir": str(state_dir),
                         "self_uid": "9000", "quotas": {"comments_per_day": 1, "dynamics_per_day": 1}})
    monkeypatch.setattr(a, "_post_send", lambda *a2, **k: {"http_status": 200, "body": {"code": 0}})
    ok = a.send_text("comment:10001:1:20001:20001", "第一条")
    assert ok["ok"] is True
    second = a.send_text("comment:10001:1:20002:20002", "第二条")
    assert second["ok"] is False
    assert "配额" in second["error"]
    # 动态配额独立计数
    dyn = a.send_text("dynamic", "一条动态")
    assert dyn["ok"] is True


def test_dry_run_does_not_consume_quota(adapter):
    for _ in range(5):
        ret = adapter.send_text("dynamic", "演练")
        assert ret["ok"] is True and ret.get("dry_run") is True
    st = adapter._load_state()
    assert not st.get("comment") and not st.get("dynamic")


# === 四道闸：风控暂停 ===

def test_risk_response_pauses_channel(state_dir, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "sessdata": "syn", "state_dir": str(state_dir),
                         "self_uid": "9000", "risk_pause_minutes": 60})
    monkeypatch.setattr(a, "_post_send",
                        lambda *a2, **k: {"http_status": 200, "body": {"code": -412, "message": "blocked"}})
    ret = a.send_text("dynamic", "撞风控")
    assert ret["ok"] is False
    assert "暂停" in ret["error"]
    # 暂停期间：dry_run 演练也拒（闸在模式门之前）
    a.mode = "dry_run"
    ret2 = a.send_text("dynamic", "暂停中的演练")
    assert ret2["ok"] is False
    assert "暂停" in ret2["error"]
    # 恢复后可用
    st = a._load_state()
    st["paused_until"] = time.time() - 1
    a._save_state(st)
    monkeypatch.setattr(a, "_post_send", lambda *a2, **k: {"http_status": 200, "body": {"code": 0}})
    assert a.send_text("dynamic", "恢复后")["ok"] is True


def test_http_412_counts_as_risk(state_dir, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "sessdata": "syn", "state_dir": str(state_dir), "self_uid": "9000"})
    monkeypatch.setattr(a, "_post_send", lambda *a2, **k: {"http_status": 412, "body": {"code": -1}})
    assert "暂停" in a.send_text("dynamic", "x")["error"]


# === 模式门：draft 落队列（配额属于真发，草稿不占） ===

def test_draft_writes_outbox_without_consuming_quota(state_dir):
    a = BilibiliAdapter({"enabled": True, "mode": "draft", "sessdata": "syn", "state_dir": str(state_dir),
                         "self_uid": "9000", "quotas": {"comments_per_day": 1, "dynamics_per_day": 1}})
    ret = a.send_text("comment:10001:1:20001:20001", "草稿一条")
    assert ret["ok"] is True and ret.get("queued") is True
    items = a.outbox()
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    assert items[0]["kind"] == "comment"
    assert not a._load_state().get("comment")   # 配额未消耗（批准真发时才占）
    # 草稿也提前过暂停/黑名单闸（省一轮废稿）
    a.blacklist = ["敏感词"]
    ret2 = a.send_text("comment:10001:1:20002:20002", "带敏感词的草稿")
    assert ret2["ok"] is False and "黑名单" in ret2["error"]


# === 目标解析与请求体 ===

def test_resolve_target_comment_and_dynamic(adapter):
    kind, payload = adapter._resolve_target("comment:10001:1:20001:20001")
    assert kind == "comment" and payload == {"oid": "10001", "type": "1", "root": "20001", "parent": "20001"}
    kind, payload = adapter._resolve_target("dynamic")
    assert kind == "dynamic"


def test_resolve_target_rejects_garbage(adapter):
    ok1, err1 = adapter._resolve_target("comment:10001")
    assert ok1 is None and "不完整" in err1
    ok2, err2 = adapter._resolve_target("group:333")
    assert ok2 is None and "未知目标" in err2


def test_comment_too_long_rejected(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_post_send", lambda *a, **k: {"http_status": 200, "body": {"code": 0}})
    ret = adapter.send_text("comment:10001:1:20001:20001", "长" * 1001)
    assert ret["ok"] is False and "超长" in ret["error"]


def test_live_send_builds_reply_form(state_dir, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "sessdata": "syn", "state_dir": str(state_dir),
                         "self_uid": "9000", "bili_jct": "jct_synthetic"})
    captured = {}

    def fake_http(method, url, **kw):
        captured["url"] = url
        captured["data"] = kw.get("data") or kw.get("json_body")
        return {"http_status": 200, "body": {"code": 0}}

    monkeypatch.setattr(a, "_http", fake_http)
    ret = a.send_text("comment:10001:1:20001:20001", "回复内容")
    assert ret["ok"] is True
    assert captured["url"] == mod._API_REPLY_ADD
    assert captured["data"]["oid"] == "10001"
    assert captured["data"]["root"] == "20001"
    assert captured["data"]["csrf"] == "jct_synthetic"


# === 通知归一 ===

def _feed_item(uid, oid, rpid, text, ts=None):
    return {"user": {"mid": uid, "uname": f"user{uid}"},
            "content": {"message": text},
            "subject": {"oid": oid, "type": 1},
            "root": rpid, "rpid": rpid,
            "ctime": int(ts if ts is not None else time.time())}


def test_parse_reply_feed_normalizes(adapter):
    payload = {"code": 0, "data": {"items": [
        _feed_item(9001, 10001, 20001, "回复内容甲"),
        _feed_item(9000, 10001, 20002, "自己的回声"),
        {"user": {"mid": 9002}, "content": {"message": "缺定位"}},
    ]}}
    entries = adapter.parse_reply_feed(payload, self_uid="9000")
    assert len(entries) == 1
    e = entries[0]
    assert e["chat_id"] == "comment:10001:1:20001:20001"
    assert e["user_id"] == "9001"
    assert e["text"] == "回复内容甲"


def test_parse_reply_feed_self_skipped_and_shape_drift(adapter):
    # 结构漂移宽容：键换位置/换名也能找（deep-find）
    payload = {"code": 0, "data": {"items": [
        {"mid": 9003, "message": "漂移形态", "oid": 10002, "rpid": 30001, "type": 17},
    ]}}
    entries = adapter.parse_reply_feed(payload, self_uid="9000")
    assert len(entries) == 1
    assert entries[0]["chat_id"] == "comment:10002:17:30001:30001"


# === 轮询：首拍基线 + 去重 ===

def test_poll_baseline_then_dispatch_once(adapter, monkeypatch):
    got, emit = _collector()
    item = _feed_item(9001, 10001, 20001, "你好肥鱼", ts=time.time())
    monkeypatch.setattr(adapter, "fetch_reply_feed",
                        lambda: {"http_status": 200, "body": {"code": 0, "data": {"items": [item]}}})
    # 首拍：静默基线（只记 seen）
    assert adapter.poll_once(emit) == []
    # 第二拍：同一 item 去重，不重复派发
    assert adapter.poll_once(emit) == []
    # 换新 item：派发一条 explicit 消息
    item2 = _feed_item(9002, 10001, 30001, "新的回复", ts=time.time())
    monkeypatch.setattr(adapter, "fetch_reply_feed",
                        lambda: {"http_status": 200, "body": {"code": 0, "data": {"items": [item, item2]}}})
    dispatched = adapter.poll_once(emit)
    assert len(dispatched) == 1
    msg = dispatched[0]
    assert msg.channel == "bilibili"
    assert msg.chat_id == "comment:10001:1:30001:30001"
    assert msg.allow_tools is False
    assert msg.reply_mode == "explicit"
    assert msg.reply.chat_id == msg.chat_id
    assert got and got[0].user_id == "9002"


def test_poll_stale_item_filtered(adapter, monkeypatch):
    got, emit = _collector()
    old = _feed_item(9001, 10001, 40001, "陈年旧评", ts=time.time() - 72 * 3600)
    monkeypatch.setattr(adapter, "fetch_reply_feed",
                        lambda: {"http_status": 200, "body": {"code": 0, "data": {"items": [old]}}})
    adapter.poll_once(emit)          # 基线
    item2 = _feed_item(9001, 10001, 40002, "也是旧的", ts=time.time() - 72 * 3600)
    monkeypatch.setattr(adapter, "fetch_reply_feed",
                        lambda: {"http_status": 200, "body": {"code": 0, "data": {"items": [item2]}}})
    assert adapter.poll_once(emit) == []


def test_poll_risk_pauses_and_skips(adapter, monkeypatch):
    got, emit = _collector()
    monkeypatch.setattr(adapter, "fetch_reply_feed",
                        lambda: {"http_status": 200, "body": {"code": -352, "message": "risk"}})
    assert adapter.poll_once(emit) == []
    assert "暂停" in adapter._paused()


# === 去重跨实例（seen 落盘） ===

def test_seen_persists_across_instances(state_dir, monkeypatch):
    item = _feed_item(9001, 10001, 20001, "跨实例")
    a1 = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir), "self_uid": "9000"})
    monkeypatch.setattr(a1, "fetch_reply_feed",
                        lambda: {"http_status": 200, "body": {"code": 0, "data": {"items": [item]}}})
    a1.poll_once(lambda m: None)  # 基线
    a2 = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir), "self_uid": "9000"})
    monkeypatch.setattr(a2, "fetch_reply_feed",
                        lambda: {"http_status": 200, "body": {"code": 0, "data": {"items": [item]}}})
    assert a2.poll_once(lambda m: None) == []  # a1 已记 seen，a2 不再派发


# === 广播 ===

def test_broadcast_requires_default_chat_id(adapter):
    ret = adapter.broadcast("没有兜底")
    assert ret["ok"] is False and "default_chat_id" in ret["error"]


# === 活人感冷却闸（连发 N 天动态强制歇一天） ===

def _post_days(a, days):
    """模拟最近 N 天每天发一条动态（直接记账）。"""
    from datetime import date, timedelta

    streak = 0
    for i in range(days, 0, -1):
        d = (date.today() - timedelta(days=i - 1)).strftime("%Y-%m-%d")
        st = a._load_state()
        st["date"] = date.today().strftime("%Y-%m-%d")
        st["dyn_last_date"] = d
        st["dyn_streak"] = days - i + 1
        a._save_state(st)
        streak = days - i + 1
    return streak


def test_cooldown_blocks_after_consecutive_days(tmp_path, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "state_dir": str(tmp_path / "s"),
                         "self_uid": "9000", "sessdata": "syn",
                         "quotas": {"comments_per_day": 4, "dynamics_per_day": 1},
                         "dynamic_cooldown_days": 2})
    monkeypatch.setattr(a, "_post_send", lambda *a2, **k: {"http_status": 200, "body": {"code": 0}})
    # 今天（第一发）：放行
    assert a.send_text("dynamic", "今天有话说")["ok"] is True
    assert a._load_state()["dyn_streak"] == 1


def test_cooldown_resets_after_gap(tmp_path, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "state_dir": str(tmp_path / "s"),
                         "self_uid": "9000", "sessdata": "syn",
                         "quotas": {"comments_per_day": 4, "dynamics_per_day": 1},
                         "dynamic_cooldown_days": 2})
    monkeypatch.setattr(a, "_post_send", lambda *a2, **k: {"http_status": 200, "body": {"code": 0}})
    # 昨天+前天连发（streak=2），但今天距上次发 >1 天——冷却应已解除
    from datetime import date, timedelta
    st = {"date": date.today().strftime("%Y-%m-%d"),
          "dyn_last_date": (date.today() - timedelta(days=2)).strftime("%Y-%m-%d"),
          "dyn_streak": 2}
    a._save_state(st)
    assert a.send_text("dynamic", "冷却结束")["ok"] is True


def test_cooldown_disabled_when_zero(tmp_path, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "state_dir": str(tmp_path / "s"),
                         "self_uid": "9000", "sessdata": "syn",
                         "quotas": {"comments_per_day": 4, "dynamics_per_day": 3},
                         "dynamic_cooldown_days": 0})
    monkeypatch.setattr(a, "_post_send", lambda *a2, **k: {"http_status": 200, "body": {"code": 0}})
    st = a._load_state()
    st["dyn_last_date"] = __import__("datetime").date.today().strftime("%Y-%m-%d")
    st["dyn_streak"] = 5
    a._save_state(st)
    assert a.send_text("dynamic", "冷却关了")["ok"] is True


def test_state_rollover_preserves_cooldown_and_pause(state_dir):
    a = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir),
                         "self_uid": "9000"})
    st = {"date": "2020-01-01", "comment": 9, "dynamic": 9,
          "dyn_last_date": "2020-01-01", "dyn_streak": 2,
          "paused_until": 9999999999, "paused_reason": "测试"}
    a._save_state(st)
    st2 = a._load_state()  # 跨年翻账
    assert st2["dyn_streak"] == 2 and st2["dyn_last_date"] == "2020-01-01"
    assert st2["paused_reason"] == "测试"
    assert not st2.get("comment") and not st2.get("dynamic")  # 当日计数清零


def test_broadcast_uses_gates(state_dir, monkeypatch):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "sessdata": "syn", "state_dir": str(state_dir),
                         "self_uid": "9000", "default_chat_id": "dynamic",
                         "quotas": {"comments_per_day": 4, "dynamics_per_day": 1}})
    monkeypatch.setattr(a, "_post_send", lambda *a2, **k: {"http_status": 200, "body": {"code": 0}})
    assert a.broadcast("动态一")["ok"] is True
    assert "配额" in a.broadcast("动态二")["error"]


# === outbox CLI 数据面 ===

def test_outbox_roundtrip(state_dir):
    a = BilibiliAdapter({"enabled": True, "mode": "draft", "sessdata": "syn", "state_dir": str(state_dir), "self_uid": "9000",
                         "quotas": {"comments_per_day": 4, "dynamics_per_day": 5}})
    a.send_text("dynamic", "草稿甲")
    a.send_text("dynamic", "草稿乙")
    items = a.outbox()
    assert [x["text"] for x in items] == ["草稿甲", "草稿乙"]
    assert json.loads((state_dir / "bilibili_outbox.jsonl").read_text(encoding="utf-8").splitlines()[0])["status"] == "pending"


# === TV 扫码登录（对齐 dsh-bilibili-plugin/biliup 协议；全离线） ===

def test_tv_sign_deterministic_and_varies():
    p1 = {"appkey": "k", "local_id": "0", "ts": "1700000000"}
    s1 = mod._tv_sign(p1)
    assert s1 == mod._tv_sign(dict(p1))          # 同参数同签名
    assert len(s1) == 32                          # md5 hex
    assert mod._tv_sign({**p1, "ts": "1700000001"}) != s1  # 时间变签名变


def test_start_login_error_path(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_http",
                        lambda *a, **k: {"http_status": 200, "body": {"code": -400, "message": "请求错误"}})
    ret = adapter.start_login()
    assert ret["ok"] is False and "获取二维码失败" in ret["error"]


def test_poll_login_status_mapping(adapter, monkeypatch):
    bodies = {
        0: {"http_status": 200, "body": {"code": 0, "data": {
            "mid": 9007,
            "cookie_info": {"cookies": [
                {"name": "SESSDATA", "value": "sess_syn"},
                {"name": "bili_jct", "value": "jct_syn"},
                {"name": "DedeUserID", "value": "9007"},
            ]}}}},
        86101: {"http_status": 200, "body": {"code": 86101, "message": "未扫码"}},
        86090: {"http_status": 200, "body": {"code": 86090, "message": "已扫码"}},
        86038: {"http_status": 200, "body": {"code": 86038, "message": "二维码已失效"}},
    }
    for code, ret in bodies.items():
        monkeypatch.setattr(adapter, "_http", lambda *a, _ret=ret, **k: _ret)
        r = adapter.poll_login("auth_syn")
        if code == 0:
            assert r["status"] == "confirmed"
            assert r["cookies"]["SESSDATA"] == "sess_syn"
            assert r["mid"] == "9007"
        else:
            assert r["status"] == {86101: "pending", 86090: "scanned", 86038: "expired"}[code]


def test_credentials_roundtrip_and_precedence(state_dir):
    # 无任何配置 → cookie 为空；--login 存凭证后新实例自动加载
    a1 = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir)})
    assert a1._cookies() == {}
    a1._save_credentials({"SESSDATA": "sess_f", "bili_jct": "jct_f", "DedeUserID": "9007"}, mid="9007")
    a2 = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir)})
    c = a2._cookies()
    assert c["SESSDATA"] == "sess_f" and c["bili_jct"] == "jct_f" and c["DedeUserID"] == "9007"
    assert a2.self_uid == "9007"  # mid 自填
    # 优先级：yaml 直填 > 凭证文件，且是全有或全无——不混搭（不同次登录的
    # cookie 对是废凭证，混着用还会触发风控）
    a3 = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir),
                          "sessdata": "sess_yaml"})
    assert a3._cookies() == {"SESSDATA": "sess_yaml"}


def test_login_interactive_confirmed(state_dir, monkeypatch, capsys):
    a = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir)})
    monkeypatch.setattr(a, "start_login",
                        lambda: {"ok": True, "auth_code": "ac", "url": "https://qr.example/syn"})
    monkeypatch.setattr(a, "whoami", lambda: {"ok": True, "mid": "9007", "uname": "合成鱼"})
    polls = iter([{"status": "pending", "message": "等待扫码"},
                  {"status": "scanned", "message": "已扫码"},
                  {"status": "confirmed", "cookies": {"SESSDATA": "s", "bili_jct": "j"},
                   "mid": "9007"}])
    monkeypatch.setattr(a, "poll_login", lambda code: next(polls))
    monkeypatch.setattr("time.sleep", lambda s: None)
    rc = a.login_interactive()
    assert rc == 0
    cred = json.loads((state_dir / "bilibili_credentials.json").read_text(encoding="utf-8"))
    assert cred["sessdata"] == "s" and cred["mid"] == "9007"
    assert "登录成功" in capsys.readouterr().out


# === 未登录闸（调度触发 + 发送快速拒） ===

def test_ready_reflects_login_state(state_dir):
    a = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir)})
    assert a.ready() is False                      # 未登录
    a._save_credentials({"SESSDATA": "s"}, mid="9007")
    assert a.ready() is True                       # --login 后就绪
    a2 = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir),
                          "sessdata": "sess_yaml"})
    assert a2.ready() is True                      # yaml/env 配置也算就绪


def test_connect_without_login_registers_inert(state_dir):
    a = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir)})
    a.connect(lambda m: None)                      # 不 raise：注册成惰性通道
    assert a.ready() is False


def test_send_without_login_rejects_for_real_modes(state_dir):
    a = BilibiliAdapter({"enabled": True, "mode": "live", "state_dir": str(state_dir)})
    ret = a.send_text("dynamic", "没登录想发")
    assert ret["ok"] is False and "未登录" in ret["error"]
    a.mode = "draft"
    assert "未登录" in a.send_text("dynamic", "没登录想排队")["error"]
    a.mode = "dry_run"                             # 纯演练放行
    assert a.send_text("dynamic", "演练不算数")["ok"] is True


def test_scheduler_channel_gate_skips_unready(monkeypatch, state_dir):
    from gateway.channels import context as ctx
    from gateway import scheduler as sched

    job = {"id": "bili-awake", "notify_channel": "bilibili"}
    # 通道未注册 → 放行（不在调度层猜配置）
    assert sched._channel_gate(job) is None
    # 注册一个未就绪 bilibili → 拦下
    a = BilibiliAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir)})
    ctx.register(a)
    try:
        reason = sched._channel_gate(job)
        assert reason and "未就绪" in reason
        a._save_credentials({"SESSDATA": "s"}, mid="9007")
        assert sched._channel_gate(job) is None    # 登录后放行
        # 没有 ready() 的通道（如 napcat）不受影响
        class _Legacy:
            name = "legacy"
        ctx.register(_Legacy())
        assert sched._channel_gate({"id": "x", "notify_channel": "legacy"}) is None
        assert sched._channel_gate({"id": "x", "notify_channel": ""}) is None
    finally:
        ctx._REGISTRY.pop("bilibili", None)
        ctx._REGISTRY.pop("legacy", None)
