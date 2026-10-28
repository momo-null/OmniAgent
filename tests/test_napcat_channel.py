"""QQ（NapCat / OneBot 11）通道适配器离线测试。

不连真实 NapCat：只验证事件归一、触发规则、去重、分条与出站请求体构造。
"""
import pytest

from gateway.channels import napcat as mod
from gateway.channels.napcat import NapcatAdapter, NapcatReply


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    return NapcatAdapter({"enabled": True})


def _collector():
    got = []
    return got, got.append


# === 事件归一 ===

def test_parse_array_segments():
    text, ats = mod.parse_message({
        "self_id": 111,
        "message": [
            {"type": "at", "data": {"qq": "111"}},
            {"type": "text", "data": {"text": " 今天几号"}},
            {"type": "image", "data": {"file": "a.jpg"}},
        ],
    })
    assert ats == {"111"}
    assert text.strip() == "今天几号[图片]"


def test_parse_cq_string():
    text, ats = mod.parse_message({
        "raw_message": "[CQ:at,qq=111] 看看这个 [CQ:image,file=b.png]",
        "message": "[CQ:at,qq=111] 看看这个 [CQ:image,file=b.png]",
    })
    assert ats == {"111"}
    assert text.strip() == "看看这个 [图片]"


def test_parse_at_all_kept_in_text():
    text, ats = mod.parse_message({"message": [{"type": "at", "data": {"qq": "all"}}]})
    assert ats == {"all"}
    assert "@全体成员" in text


# === 触发规则 ===

def test_group_without_at_ignored(adapter):
    got, emit = _collector()
    adapter._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 1,
        "message": [{"type": "text", "data": {"text": "随便聊聊"}}],
    }, emit)
    assert got == []


def test_group_with_at_dispatched(adapter):
    got, emit = _collector()
    adapter._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 2,
        "message": [{"type": "at", "data": {"qq": "111"}},
                    {"type": "text", "data": {"text": " 今天几号"}}],
    }, emit)
    assert len(got) == 1
    msg = got[0]
    assert msg.channel == "napcat"
    assert msg.chat_id == "group:333"
    # 正文前带通道场合自述（模型必须知道"这是你的 QQ / 谁在说 / 回复直达"）
    assert "今天几号" in msg.text
    assert "这是你的 QQ" in msg.text
    assert "群 333" in msg.text
    assert "「" in msg.text  # 群聊必须带说话人
    assert "第一人称" in msg.text
    assert isinstance(msg.reply, NapcatReply)


def test_private_dispatched(adapter):
    got, emit = _collector()
    adapter._handle_event({
        "post_type": "message", "message_type": "private", "self_id": 111,
        "user_id": 222, "message_id": 3,
        "message": [{"type": "text", "data": {"text": "在吗"}}],
    }, emit)
    assert len(got) == 1
    assert got[0].chat_id == "private:222"
    assert "在吗" in got[0].text
    assert "这是你的 QQ" in got[0].text
    # 输出语义（回复直达对方）与代答边界必须写给模型
    assert "回复会直接发给对方" in got[0].text
    assert "我确认下再回你" in got[0].text


def test_message_sent_ignored(adapter):
    """自己发出去的消息（message_sent）不能回流成新输入。"""
    got, emit = _collector()
    adapter._handle_event({
        "post_type": "message_sent", "message_type": "group", "self_id": 111,
        "user_id": 111, "group_id": 333, "message_id": 4,
        "message": [{"type": "text", "data": {"text": "我自己的回复"}}],
    }, emit)
    assert got == []


def test_duplicate_message_id_ignored(adapter):
    got, emit = _collector()
    event = {
        "post_type": "message", "message_type": "private", "self_id": 111,
        "user_id": 222, "message_id": 5,
        "message": [{"type": "text", "data": {"text": "同一事件重推"}}],
    }
    adapter._handle_event(event, emit)
    adapter._handle_event(event, emit)
    assert len(got) == 1


def test_private_whitelist(monkeypatch):
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    adapter = NapcatAdapter({"enabled": True, "allow_users": ["999"]})
    got, emit = _collector()
    adapter._handle_event({
        "post_type": "message", "message_type": "private", "self_id": 111,
        "user_id": 222, "message_id": 6,
        "message": [{"type": "text", "data": {"text": "非白名单"}}],
    }, emit)
    assert got == []


def test_group_whitelist(monkeypatch):
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    adapter = NapcatAdapter({"enabled": True, "allow_groups": ["888"]})
    got, emit = _collector()
    adapter._handle_event({
        "post_type": "message", "message_type": "group", "self_id": 111,
        "user_id": 222, "group_id": 333, "message_id": 7,
        "message": [{"type": "at", "data": {"qq": "111"}},
                    {"type": "text", "data": {"text": "hi"}}],
    }, emit)
    assert got == []


def test_image_only_message_gets_placeholder(adapter):
    got, emit = _collector()
    adapter._handle_event({
        "post_type": "message", "message_type": "private", "self_id": 111,
        "user_id": 222, "message_id": 8,
        "message": [{"type": "image", "data": {"file": "c.jpg"}}],
    }, emit)
    assert "[图片]" in got[0].text


# === 出站 ===

def test_send_text_group_payload(adapter, monkeypatch):
    calls = []

    def fake_api(action, payload):
        calls.append((action, payload))
        return {"ok": True, "data": {"message_id": 42}}

    monkeypatch.setattr(adapter, "_api", fake_api)
    result = adapter.send_text("group:333", "你好")
    assert result["ok"] is True
    assert calls[0][0] == "send_group_msg"
    assert calls[0][1]["group_id"] == 333
    assert calls[0][1]["message"] == [{"type": "text", "data": {"text": "你好"}}]


def test_send_text_private_payload(adapter, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "_api",
                        lambda action, payload: calls.append((action, payload)) or
                        {"ok": True, "data": {"message_id": 43}})
    adapter.send_text("private:222", "在")
    assert calls[0][0] == "send_private_msg"
    assert calls[0][1]["user_id"] == 222


def test_send_text_rejects_bad_chat_id(adapter):
    assert adapter.send_text("333", "x")["ok"] is False
    assert adapter.send_text("", "x")["ok"] is False


def test_send_text_splits_long_text(adapter, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "_api",
                        lambda action, payload: calls.append(payload) or
                        {"ok": True, "data": {"message_id": 1}})
    result = adapter.send_text("private:222", "长" * 2500)
    assert result["ok"] is True
    assert len(calls) == 3
    assert all(len(c[ "message"][0]["data"]["text"]) <= 1200 for c in calls)
    assert "".join(c["message"][0]["data"]["text"] for c in calls) == "长" * 2500


def test_send_text_reports_api_failure(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_api", lambda action, payload: {"ok": False, "error": "retcode=100"})
    result = adapter.send_text("private:222", "x")
    assert result["ok"] is False
    assert "retcode=100" in result["error"]


def test_send_card_unsupported(adapter):
    result = NapcatReply(adapter, "private:222").send_card({"card": {}})
    assert result["ok"] is False
    assert "msg_send_text" in result["error"]


def test_chunk_text_edges():
    assert mod.chunk_text("") == []
    assert mod.chunk_text("短") == ["短"]
    assert len(mod.chunk_text("a\n" * 900, limit=1200)) >= 2


# === 前缀契约：msg_recent 依赖它剥场合说明（改文案会在这里暴露） ===

def _load_messaging_plugin():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "plugins" / "messaging" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_msg_plugin_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_msg_recent_strips_channel_ctx_and_parses_peer():
    plugin = _load_messaging_plugin()
    got = []

    def emit(m):
        got.append(m)

    adapter = NapcatAdapter({"enabled": True})
    adapter._handle_event({
        "post_type": "message", "message_type": "private", "self_id": 111,
        "user_id": 222, "message_id": 99,
        "message": [{"type": "text", "data": {"text": "你好啊"}}],
        "sender": {"nickname": "阿伟"},
    }, emit)
    body, peer = plugin._strip_ctx(got[0].text)
    assert body == "你好啊"
    assert peer == "阿伟(222)"
