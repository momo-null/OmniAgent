"""出站守则离线测试。

只验证 gateway/outbound 的归一/机械闸/场合自述逻辑与 msg_send_* 接线点；
不连任何通道、不发真实请求；本模块不触碰任何记忆接口。
"""
import pytest

from gateway.outbound import (context_key_of, enforce, flatten, note,
                              resolve, split_paragraphs, voice_error)


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
    ob = {"identity": "构造身份A", "style": "短句", "max_chars": 10,
          "unknown_key": "丢弃", "by_context": {"group": {"max_chars": 5}}}
    flat = flatten(ob, "group")
    assert flat == {"identity": "构造身份A", "style": "短句", "max_chars": 5}
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


# === 空行分条（模型显式写出的「分几条说」意图）===

# 构造值：三段、段内含逗号/省略号/多句，模拟模型用空行写出「分几条说」
_PARA_TEXT = ("第一段先说这个。\n\n"
              "……第二段换个意思。\n\n"
              "第三段把细节补上，别急着下结论。收个尾。")


def test_split_paragraphs_keeps_inner_single_newline():
    assert split_paragraphs("甲\n\n乙") == ["甲", "乙"]
    assert split_paragraphs("甲\n乙") == ["甲\n乙"]
    assert split_paragraphs("甲\n \n\n乙\n") == ["甲", "乙"]
    assert split_paragraphs("甲\r\n\r\n乙") == ["甲", "乙"]
    assert split_paragraphs("") == []


def test_enforce_blank_lines_split_without_any_config():
    """回归：真机通道没配 outbound 也要按空行分成三条，不能整串一条发出。"""
    chunks, err = enforce({}, _PARA_TEXT)
    assert err == "" and len(chunks) == 3
    assert chunks[0] == "第一段先说这个。"
    assert chunks[1] == "……第二段换个意思。"
    assert chunks[2] == "第三段把细节补上，别急着下结论。收个尾。"


def test_enforce_blank_lines_split_with_group_config():
    """回归：带群聊上限时也不能把前两段拼回一条（旧行为会丢空行边界）。"""
    chunks, err = enforce({"max_chars": 60, "max_sentences": 2}, _PARA_TEXT)
    assert err == "" and len(chunks) == 3
    assert chunks[0] == "第一段先说这个。" and chunks[1] == "……第二段换个意思。"


def test_enforce_paragraph_boundary_is_hard_even_within_limits():
    """两段加起来也没超限，仍算两条——空行是意图，不是排版。"""
    chunks, err = enforce({"max_chars": 200, "max_sentences": 10}, "甲。\n\n乙。")
    assert err == "" and chunks == ["甲。", "乙。"]


def test_enforce_single_newline_inside_paragraph_is_not_a_split():
    chunks, err = enforce({}, "哼，谁是大笨鱼啊\n再乱叫咬你哦")
    assert err == "" and chunks == ["哼，谁是大笨鱼啊\n再乱叫咬你哦"]


def test_enforce_long_paragraph_splits_inside_paragraph_only():
    """某一段自身超限 → 只在该段内按句续拆，别的段与段界不受影响。"""
    chunks, err = enforce({"max_sentences": 1}, "甲。乙。\n\n丙。")
    assert err == "" and chunks == ["甲。", "乙。", "丙。"]


def test_enforce_rejects_multiple_paragraphs_when_multi_disallowed():
    """allow_multi=false 不静默合并吞掉分条意图，明确要求模型合成一条。"""
    chunks, err = enforce({"allow_multi": False}, _PARA_TEXT)
    assert chunks == [] and "合成一条" in err


def test_enforce_no_blank_line_split_opt_out():
    """图片说明这类只该一条的调用点可关掉分条。"""
    chunks, err = enforce({}, "甲。\n\n乙。", split_blank_lines=False)
    assert err == "" and chunks == ["甲。\n\n乙。"]


# === 一轮条数上限（不说长篇大论）===

def test_enforce_max_chunks_rejects_overflow():
    """拆完仍超条数 → 拒发让模型删减，不截断也不静默丢内容。"""
    chunks, err = enforce({"max_chunks": 3}, "甲。\n\n乙。\n\n丙。\n\n丁。")
    assert chunks == [] and "最多说 3 条" in err and "4 条" in err


def test_enforce_max_chunks_allows_exact_and_below():
    assert enforce({"max_chunks": 3}, "甲。\n\n乙。\n\n丙。")[0] == ["甲。", "乙。", "丙。"]
    assert enforce({"max_chunks": 3}, "甲。\n\n乙。")[0] == ["甲。", "乙。"]


def test_enforce_max_chunks_counts_splits_inside_paragraph():
    """段内按句续拆出来的条数也算进一轮总数。"""
    chunks, err = enforce({"max_sentences": 1, "max_chunks": 2}, "甲。乙。\n\n丙。")
    assert chunks == [] and "最多说 2 条" in err


def test_flatten_keeps_max_chunks_and_by_context_overlay():
    flat = flatten({"max_chunks": 3, "by_context": {"group": {"max_chunks": 2}}}, "group")
    assert flat["max_chunks"] == 2
    assert flatten({"max_chunks": 3}, "private")["max_chunks"] == 3


def test_note_includes_max_chunks():
    text = note({"max_chars": 40, "max_sentences": 1, "max_chunks": 3})
    assert "单条≤40字" in text and "≤1句" in text and "一轮最多3条" in text


# === 语音上限 ===

def test_voice_error_reject_only():
    assert voice_error({"max_chars": 100}, "短句") == ""
    err = voice_error({"max_chars": 5}, "这句话太长了放不下")
    assert "不拆条" in err
    err2 = voice_error({"max_sentences": 1}, "一句。两句。")
    assert "不拆条" in err2


# === 场合自述 note ===

def test_note_composition():
    flat = {"identity": "构造身份A", "style": "短句为主",
            "max_chars": 60, "max_sentences": 2, "allow_multi": True}
    text = note(flat)
    assert text.startswith("（对外守则：")
    assert "「构造身份A」" in text
    assert "短句为主" in text
    assert "≤60字" in text and "≤2句" in text
    assert "可拆成多条发送" in text


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


def test_blank_lines_become_three_real_messages_without_config(stub_channel, monkeypatch):
    """回归：真机没配 outbound 时，一次 msg_send_text 也要真的发出三条。"""
    from plugins.messaging import plugin as msg

    monkeypatch.setattr("gateway.channel_gateway.channel_config", lambda name: {})
    chunks, err = msg._outbound_chunks(stub_channel, "group:333", _PARA_TEXT)
    assert err == "" and len(chunks) == 3
    reply = stub_channel.reply_to("group:333")
    result = msg._do_send_text(reply, chunks, None)
    assert result["ok"] is True and "拆为 3 条" in result["note"]
    assert reply.sent == chunks


def test_image_caption_opt_out_keeps_all_content(stub_channel, monkeypatch):
    """图片说明只取 chunks[0]，关掉分条才不会丢掉空行后的内容。"""
    from plugins.messaging import plugin as msg

    monkeypatch.setattr("gateway.channel_gateway.channel_config", lambda name: {})
    text = "甲。\n\n乙。"
    chunks, err = msg._outbound_chunks(stub_channel, "group:333", text,
                                       split_blank_lines=False)
    assert err == "" and chunks == [text]
