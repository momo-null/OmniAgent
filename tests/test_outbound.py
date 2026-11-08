"""出站守则（§8 马甲统一注入契约）离线测试。

只验证 gateway/outbound 的归一/机械闸/场合自述逻辑与 msg_send_* 接线点；
不连任何通道、不发真实请求。马甲不是人格：identity 只是自称，测试同时守住
「不产生第二份人格/记忆」的契约边界（本模块不触碰任何记忆接口）。
"""
import pytest

from gateway.outbound import (context_key_of, enforce, flatten, note,
                              resolve, voice_error)


# === context_key ===

def test_context_key_of():
    assert context_key_of("group:333") == "group"
    assert context_key_of("private:10001") == "private"
    assert context_key_of("comment:10001:1:20001:20001") == "comment"
    assert context_key_of("dynamic") == "dynamic"
    assert context_key_of("chat:abc123") == "chat"
    assert context_key_of("") == ""


# === flatten ===

def test_flatten_overlay_and_whitelist():
    ob = {"identity": "大肥鱼", "style": "短句", "max_chars": 10,
          "unknown_key": "丢弃", "by_context": {"group": {"max_chars": 5}}}
    flat = flatten(ob, "group")
    assert flat == {"identity": "大肥鱼", "style": "短句", "max_chars": 5}
    flat_priv = flatten(ob, "private")
    assert flat_priv["max_chars"] == 10  # 未命中 by_context 用基础值
    assert "unknown_key" not in flat_priv


def test_flatten_empty_and_bad_input():
    assert flatten({}, "group") == {}
    assert flatten(None, "group") == {}
    assert flatten({"outbound": 1}, "group") == {}  # 非字典 by_context 忽略


def test_resolve_reads_channel_config(monkeypatch):
    monkeypatch.setattr("gateway.channel_gateway.channel_config",
                        lambda name: ({"outbound": {"max_chars": 60,
                                                    "by_context": {"private": {"max_chars": 200}}}}
                                      if name == "stub" else {}))
    assert resolve("stub", "group:333")["max_chars"] == 60
    assert resolve("stub", "private:1")["max_chars"] == 200
    assert resolve("nope", "group:1") == {}


# === 机械闸 enforce ===

def test_enforce_passthrough_within_limits():
    chunks, err = enforce({"max_chars": 100, "max_sentences": 5}, "一句。两句。")
    assert err == "" and chunks == ["一句。两句。"]


def test_enforce_no_config_passthrough():
    chunks, err = enforce({}, "很长很长很长很长很长很长很长很长很长很长很长很长很长很长")
    assert err == "" and len(chunks) == 1


def test_enforce_splits_by_sentences():
    flat = {"max_chars": 60, "max_sentences": 1}
    text = "第一条说这个。第二条说那个。第三条补充一下细节。"
    chunks, err = enforce(flat, text)
    assert err == "" and len(chunks) == 3
    assert chunks[0] == "第一条说这个。"
    assert all(len(c) <= 60 for c in chunks)


def test_enforce_packs_to_both_limits():
    flat = {"max_chars": 60, "max_sentences": 2}
    text = "甲。" * 5  # 每句 2 字，5 句
    chunks, err = enforce(flat, text)
    assert err == "" and len(chunks) == 3
    assert chunks[0] == "甲。甲。" and chunks[-1] == "甲。"


def test_enforce_rejects_when_multi_not_allowed():
    chunks, err = enforce({"max_chars": 5, "allow_multi": False}, "这句话太长了放不下")
    assert chunks == [] and "不允许拆条" in err


def test_enforce_rejects_oversized_single_sentence():
    chunks, err = enforce({"max_chars": 5, "allow_multi": True}, "一个句子超长没有句读无法拆")
    assert chunks == [] and "精简" in err


def test_enforce_empty_text():
    chunks, err = enforce({}, "   ")
    assert chunks == [] and "空文本" in err


# === 语音上限 ===

def test_voice_error_reject_only():
    assert voice_error({"max_chars": 100}, "短句") == ""
    err = voice_error({"max_chars": 5}, "这句话太长了放不下")
    assert "不拆条" in err
    err2 = voice_error({"max_sentences": 1}, "一句。两句。")
    assert "不拆条" in err2


# === 场合自述 note ===

def test_note_composition():
    flat = {"identity": "大肥鱼", "style": "短句为主",
            "max_chars": 60, "max_sentences": 2, "allow_multi": True}
    text = note(flat)
    assert text.startswith("（对外守则：")
    assert "「大肥鱼」" in text
    assert "短句为主" in text
    assert "≤60字" in text and "≤2句" in text
    assert "拆成几条短发" in text


def test_note_empty_config_returns_empty():
    assert note({}) == ""
    assert note({"require_review": False}) == ""


def test_note_disallow_multi_wording():
    text = note({"max_chars": 10, "allow_multi": False})
    assert "不要拆条" in text


# === msg_send_* 接线点（fake adapter，不走 SDK 工具壳） ===

class _Reply:
    channel_name = "stub"

    def __init__(self):
        self.sent = []
        self.fail_on = None  # 第 n 条（1 起）失败

    def send_text(self, t):
        if self.fail_on and len(self.sent) + 1 >= self.fail_on:
            self.sent.append(t)
            return {"ok": False, "error": "模拟失败"}
        self.sent.append(t)
        return {"ok": True}


class _Adapter:
    name = "stub"

    def __init__(self):
        self.reply = _Reply()

    def reply_to(self, chat_id):
        return self.reply


@pytest.fixture
def stub_channel(monkeypatch):
    a = _Adapter()
    from gateway.channels import context as ctx

    ctx.register(a)
    monkeypatch.setattr("gateway.channel_gateway.channel_config",
                        lambda name: ({"outbound": {"max_chars": 10,
                                                    "by_context": {"group": {"max_chars": 20,
                                                                             "max_sentences": 1}}}}
                                      if name == "stub" else {}))
    yield a
    from gateway.channels import context as ctx2

    ctx2._REGISTRY.pop("stub", None)


def test_msg_send_text_splits_via_gate(stub_channel):
    from plugins.messaging import plugin as msg

    text = "第一条先说这个事情。第二条再说另一件事情。第三条把细节补上。"
    chunks, err = msg._outbound_chunks(stub_channel, "group:333", text)
    assert err == "" and len(chunks) == 3
    result = msg._do_send_text(stub_channel.reply_to("group:333"), chunks, None)
    assert result["ok"] is True
    assert "拆为 3 条" in result["note"]
    assert stub_channel.reply.sent == chunks


def test_msg_send_text_gate_rejects(stub_channel):
    from plugins.messaging import plugin as msg

    long_text = "这是一个非常非常非常长的句子中间没有任何标点符号所以拆不动"
    chunks, err = msg._outbound_chunks(stub_channel, "group:333", long_text)
    assert chunks == [] and "精简" in err


def test_do_send_text_midway_failure_reports_position(stub_channel):
    from plugins.messaging import plugin as msg

    reply = stub_channel.reply_to("group:333")
    reply.fail_on = 2
    result = msg._do_send_text(reply, ["甲。", "乙。", "丙。"], None)
    assert result["ok"] is False
    assert "第 2/3 条" in result["error"]
    assert len(reply.sent) == 2  # 第三条不再发


def test_outbound_flat_uses_adapter_name(stub_channel):
    from plugins.messaging import plugin as msg

    flat = msg._outbound_flat(stub_channel, "group:333")
    assert flat["max_chars"] == 20  # 来自 stub 通道的 by_context.group
    assert flat["max_sentences"] == 1
    assert msg._outbound_flat(stub_channel, "private:1")["max_chars"] == 10  # 未命中覆写用基础值
