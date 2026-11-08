"""小红书通道适配器离线测试（M4，fake MCP transport，零网络）。

只验证四道闸、目标解析、MCP 工具映射与入站去重；全部合成 ID。
"""
import json

import pytest

from gateway.channels import xiaohongshu as mod
from gateway.channels.xiaohongshu import XhsAdapter, build


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "xhs-state"


@pytest.fixture
def adapter(state_dir, monkeypatch):
    a = XhsAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir),
                    "quotas": {"comments_per_day": 2, "notes_per_day": 1}})
    monkeypatch.setattr(a, "_login_status", lambda force=False: True)  # 联调用例里默认已登录
    return a


# === 工厂与模式 ===

def test_build_disabled_returns_none():
    assert build({"enabled": False}) is None


def test_build_invalid_mode_raises(state_dir):
    with pytest.raises(ValueError):
        XhsAdapter({"enabled": True, "mode": "yolo", "state_dir": str(state_dir)})


# === 目标解析 ===

def test_send_note_requires_image(adapter):
    ret = adapter.send_text("note", "纯文字想发笔记")
    assert ret["ok"] is False and "必须有图" in ret["error"]


def test_send_text_comment_mapping(adapter, monkeypatch):
    captured = {}

    def fake_call(tool, args=None):
        captured["tool"], captured["args"] = tool, args
        return {"ok": True, "data": {"comment_id": "c1"}}

    monkeypatch.setattr(adapter, "_mcp_call", fake_call)
    adapter.mode = "live"
    ret = adapter.send_text("comment:feed123:xsec_abc", "好可爱的笔记！")
    assert ret["ok"] is True
    assert captured["tool"] == mod._XHS_TOOLS["comment"]
    assert captured["args"] == {"feed_id": "feed123", "xsec_token": "xsec_abc",
                                "content": "好可爱的笔记！"}
    assert ret.get("comment_id") == "c1"


def test_send_text_rejects_unknown_target(adapter):
    ret = adapter.send_text("group:333", "走错通道了")
    assert ret["ok"] is False and "未知目标" in ret["error"]


# === 四道闸 ===

def test_blacklist_rejects(adapter, monkeypatch):
    adapter.mode = "live"
    adapter.blacklist = ["敏感词"]
    monkeypatch.setattr(adapter, "_mcp_call", lambda t, a=None: {"ok": True, "data": {}})
    ret = adapter.send_text("comment:f1:t1", "聊聊敏感词话题")
    assert ret["ok"] is False and "黑名单" in ret["error"]


def test_quota_exhausted(tmp_path, monkeypatch):
    a = XhsAdapter({"enabled": True, "mode": "live", "state_dir": str(tmp_path / "s"),
                    "quotas": {"comments_per_day": 1, "notes_per_day": 1}})
    monkeypatch.setattr(a, "_login_status", lambda force=False: True)
    monkeypatch.setattr(a, "_mcp_call", lambda t, a2=None: {"ok": True, "data": {}})
    assert a.send_text("comment:f1:t1", "第一条")["ok"] is True
    assert "配额" in a.send_text("comment:f2:t2", "第二条")["error"]


def test_dry_run_does_not_consume(adapter):
    for _ in range(3):
        assert adapter.send_text("comment:f1:t1", "演练")["ok"] is True
    st = adapter._load_state()
    assert not st.get("comment")


def test_risk_pause(tmp_path, monkeypatch):
    a = XhsAdapter({"enabled": True, "mode": "live", "state_dir": str(tmp_path / "s")})
    monkeypatch.setattr(a, "_login_status", lambda force=False: True)

    def fail_call(tool, args=None):
        return {"ok": False, "error": "操作频繁，出现风控验证"}

    monkeypatch.setattr(a, "_mcp_call", fail_call)
    ret = a.send_text("comment:f1:t1", "撞风控")
    assert ret["ok"] is False and "暂停" in ret["error"]
    assert a._gate_paused()  # 暂停生效
    a.mode = "dry_run"
    assert "暂停" in a.send_text("comment:f1:t1", "暂停中的演练")["error"]


# === send_note（图文笔记，标题 ≤20 字） ===

def test_send_note_validates(tmp_path, adapter):
    ret = adapter.send_note(str(tmp_path / "nope.png"), "正文", "标题")
    assert ret["ok"] is False and "不存在" in ret["error"]
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    ret2 = adapter.send_note(str(img), "", "标题")
    assert ret2["ok"] is False and "不能为空" in ret2["error"]


def test_send_note_draft_queues_and_truncates_title(tmp_path, adapter):
    adapter.mode = "draft"
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    long_title = "这是一个超过二十个字符的标题用来测试截断逻辑是否生效"  # >20 字
    ret = adapter.send_note(str(img), "正文内容", long_title)
    assert ret["ok"] is True and ret.get("queued") is True
    items = adapter.outbox()
    assert len(items) == 1 and items[0]["kind"] == "note"
    assert len(items[0]["text"].split("｜")[0]) == 20  # 标题被截到 20 字


# === ready 缓存与未登录拒发 ===

def test_ready_cache(state_dir, monkeypatch):
    a = XhsAdapter({"enabled": True, "mode": "dry_run", "state_dir": str(state_dir)})
    calls = {"n": 0}

    def fake_call(tool, args=None):
        if tool == mod._XHS_TOOLS["login"]:
            calls["n"] += 1
            return {"ok": True, "data": {"logged_in": True}}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(a, "_mcp_call", fake_call)
    assert a.ready() is True and a.ready() is True
    assert calls["n"] == 1  # 5 分钟缓存：两次 ready 只打一次 MCP

    a2 = XhsAdapter({"enabled": True, "mode": "live", "state_dir": str(state_dir)})
    monkeypatch.setattr(a2, "_login_status", lambda force=False: False)
    ret = a2.send_text("comment:f1:t1", "没登录")
    assert ret["ok"] is False and "未登录" in ret["error"]


# === 入站：自己笔记的评论区轮询去重 ===

def test_poll_once_dedups_comments(adapter):
    st = {"notes": [{"ts": 1.0, "title": "测试", "feed_id": "f1", "xsec_token": "t1"}]}
    adapter._save_state(st)
    comments = {"ok": True, "data": {"comments": [
        {"comment_id": "c1", "content": "第一条评论", "user_id": "u1", "nickname": "路人"},
        {"comment_id": "c1", "content": "重复", "user_id": "u1"},
        {"comment_id": "c2", "content": "第二条评论", "user_id": "u2", "nickname": "路人乙"},
    ]}}
    adapter._mcp_call = lambda t, a=None: comments  # type: ignore
    got, emit = [], lambda m: got.append(m)
    first = adapter.poll_once(emit)
    assert len(first) == 2
    assert first[0].channel == "xiaohongshu"
    assert first[0].reply_mode == "explicit"
    assert first[0].chat_id == "comment:f1:t1"
    second = adapter.poll_once(emit)
    assert second == []  # seen_comments 去重
    st2 = adapter._load_state()
    assert set(st2["seen_comments"]) == {"c1", "c2"}


# === outbox CLI 数据面 ===

def test_outbox_roundtrip(adapter):
    adapter.mode = "draft"
    adapter.send_text("comment:f1:t1", "草稿甲")
    adapter.send_text("comment:f2:t2", "草稿乙")
    items = adapter.outbox()
    assert [x["text"] for x in items] == ["草稿甲", "草稿乙"]


def test_approve_and_discard_item(adapter, monkeypatch):
    adapter.mode = "draft"
    adapter.send_text("comment:f1:t1", "待批准")
    monkeypatch.setattr(adapter, "_mcp_call", lambda t, a=None: {"ok": True, "data": {"comment_id": "c9"}})
    ret = adapter.approve_item(1)
    assert ret["ok"] is True
    assert adapter.outbox()[0]["status"] == "sent"
    adapter.send_text("comment:f2:t2", "要作废")
    ret2 = adapter.discard_item(2)
    assert ret2["ok"] is True
    assert adapter.outbox()[1]["status"] == "discarded"
    assert adapter.approve_item(2)["ok"] is False
    assert adapter.approve_item(99)["ok"] is False
