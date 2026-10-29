"""QQ 语音消息链路离线测试：入站语音转写进会话、出站 TTS 语音回发与降级。

不连真实 NapCat、不加载真模型——capability 用注册表里的 fake。
"""
import io
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway import dispatch as gw_dispatch  # noqa: E402
from gateway.channels import napcat as mod  # noqa: E402
from gateway.channels.napcat import NapcatAdapter  # noqa: E402
from omni_core.tools.capability import (  # noqa: E402
    get_capability,
    register_capability,
    unregister_capability,
)


@pytest.fixture
def adapter():
    return NapcatAdapter({"enabled": True})


def _wav_bytes(seconds=0.2, sr=16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * seconds))
    return buf.getvalue()


@pytest.fixture
def fake_voice_caps():
    """注册 fake STT/TTS capability，测试完注销。"""
    register_capability("voice_transcribe", lambda b: "帮我查下明天天气")
    register_capability("voice_tts", lambda text, voice="": b"RIFFfake-wav")
    yield
    unregister_capability("voice_transcribe")
    unregister_capability("voice_tts")


def _voice_event(message_id=9001, file_ref="record-abc.amr"):
    return {
        "post_type": "message", "message_type": "private", "self_id": 111,
        "user_id": 222, "message_id": message_id,
        "message": [{"type": "record", "data": {"file": file_ref}}],
    }


# === 入站：语音转写 ===

def test_voice_message_transcribed_into_text(adapter, fake_voice_caps, monkeypatch, tmp_path):
    """语音段 → get_record 转码取回 → STT 转写替换 [语音] 占位 + voice=True。"""
    audio = tmp_path / "out.mp3"
    audio.write_bytes(_wav_bytes())
    monkeypatch.setattr(adapter, "_api",
                        lambda action, payload: {"ok": True, "data": {"file": str(audio)}})
    got, emit = [], lambda m: got.append(m)
    adapter._handle_event(_voice_event(), emit)
    assert len(got) == 1
    assert "帮我查下明天天气" in got[0].text
    # 媒介标记要保留：模型得知道「对方是语音说的」，才能自己决定回语音还是文字
    assert "[语音]" in got[0].text
    assert got[0].voice is True


def test_voice_plugin_missing_keeps_placeholder(adapter, monkeypatch):
    """voice 插件未启用（capability 缺席）→ 保留 [语音] 占位，照常派发。"""
    got, emit = [], lambda m: got.append(m)
    adapter._handle_event(_voice_event(), emit)
    assert len(got) == 1
    assert "[语音]" in got[0].text
    assert got[0].voice is False


def test_transcribe_failure_degrades_to_placeholder(adapter, monkeypatch, tmp_path):
    """STT 抛错 → 不炸 WS 线程，降级 [语音] 占位。"""
    register_capability("voice_transcribe",
                        lambda b: (_ for _ in ()).throw(RuntimeError("模型缺失")))
    try:
        audio = tmp_path / "out.mp3"
        audio.write_bytes(_wav_bytes())
        monkeypatch.setattr(adapter, "_api",
                            lambda action, payload: {"ok": True, "data": {"file": str(audio)}})
        got, emit = [], lambda m: got.append(m)
        adapter._handle_event(_voice_event(), emit)
        assert "[语音]" in got[0].text and got[0].voice is False
    finally:
        unregister_capability("voice_transcribe")


def test_record_local_file_direct_read(adapter, monkeypatch, tmp_path):
    """file 引用是本地 mp3/wav 时直读，不走 get_record。"""
    audio = tmp_path / "x.mp3"
    audio.write_bytes(b"ID3fake-mp3")
    called = {"api": False}

    def _no_api(action, payload):
        called["api"] = True
        return {"ok": False, "error": "should not call"}

    monkeypatch.setattr(adapter, "_api", _no_api)
    data = mod.fetch_record_audio_bytes(adapter, {"file": str(audio)})
    assert data == b"ID3fake-mp3"
    assert called["api"] is False


def test_record_silk_url_skipped_for_get_record(adapter, monkeypatch, tmp_path):
    """http 直链若是 silk/amr 后缀 → 跳过下载，走 get_record 转码。"""
    audio = tmp_path / "conv.mp3"
    audio.write_bytes(_wav_bytes())
    seen = []

    def fake_api(action, payload):
        seen.append((action, payload))
        return {"ok": True, "data": {"file": str(audio)}}

    monkeypatch.setattr(adapter, "_api", fake_api)
    data = mod.fetch_record_audio_bytes(
        adapter, {"file": "rec1", "url": "http://127.0.0.1:3000/rec1.silk"})
    assert data.startswith(b"RIFF") or data == _wav_bytes()
    assert seen and seen[0][0] == "get_record"
    assert seen[0][1]["out_format"] == "mp3"


def test_get_record_preferred_even_with_plain_url(adapter, monkeypatch, tmp_path):
    """带 url 也先走 get_record：QQ 语音直链常是不带扩展名的 silk 内容。"""
    audio = tmp_path / "conv.mp3"
    audio.write_bytes(b"ID3-converted")
    seen = []

    def fake_api(action, payload):
        seen.append(action)
        return {"ok": True, "data": {"file": str(audio)}}

    def _no_download(*a, **kw):
        raise AssertionError("不应下载直链")

    monkeypatch.setattr(adapter, "_api", fake_api)
    monkeypatch.setattr("requests.get", _no_download)
    data = mod.fetch_record_audio_bytes(
        adapter, {"file": "rec1", "url": "https://gchat.qpic.cn/x?term=2"})
    assert data == b"ID3-converted"
    assert seen == ["get_record"]


def test_get_record_failure_falls_back_to_url(adapter, monkeypatch):
    """get_record 失败（napcat 无 ffmpeg）→ 直链下载兜底。"""
    monkeypatch.setattr(adapter, "_api", lambda a, p: {"ok": False, "error": "no ffmpeg"})

    class _Resp:
        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield b"ID3-direct"

    monkeypatch.setattr("requests.get", lambda *a, **kw: _Resp())
    data = mod.fetch_record_audio_bytes(
        adapter, {"file": "rec1", "url": "http://host/rec1.mp3"})
    assert data == b"ID3-direct"


def test_local_path_used_when_get_record_fails(adapter, monkeypatch, tmp_path):
    """get_record 失败 → 段内 path（本地原件）兜底，交给上游处理。"""
    amr = tmp_path / "x.amr"
    amr.write_bytes(b"#!AMR-fake")
    monkeypatch.setattr(adapter, "_api", lambda a, p: {"ok": False, "error": "no ffmpeg"})
    data = mod.fetch_record_audio_bytes(
        adapter, {"file": "rec1", "path": str(amr)})
    assert data == b"#!AMR-fake"


def test_local_decodable_path_skips_roundtrip(adapter, monkeypatch, tmp_path):
    """接收段自带本地 mp3（path 字段）→ 直读，不调 get_record。"""
    mp3 = tmp_path / "x.mp3"
    mp3.write_bytes(b"ID3-local")

    def _no_api(action, payload):
        raise AssertionError("不应调 get_record")

    monkeypatch.setattr(adapter, "_api", _no_api)
    data = mod.fetch_record_audio_bytes(adapter, {"file": "rec1", "path": str(mp3)})
    assert data == b"ID3-local"


# === TTS 语言码：espeak 中文是 cmn，误用英文码会念成 "Chinese letter" ===

def test_espeak_lang_picks_cmn_for_cjk(monkeypatch):
    from plugins.voice import plugin as vp

    monkeypatch.setattr(vp, "_cfg", lambda: {})
    assert vp._espeak_lang("明天天气怎么样") == "cmn"
    assert vp._espeak_lang("hello world") == "en-us"


def test_espeak_lang_config_override(monkeypatch):
    from plugins.voice import plugin as vp

    monkeypatch.setattr(vp, "_cfg", lambda: {"tts_language": "en-us"})
    assert vp._espeak_lang("明天天气") == "en-us"
    monkeypatch.setattr(vp, "_cfg", lambda: {"tts_language": "auto"})
    assert vp._espeak_lang("明天天气") == "cmn"


# === 出站：TTS 语音回发 ===

def test_send_voice_uses_record_segment(adapter, fake_voice_caps, monkeypatch):
    """本机 napcat：首选带 .wav 扩展名的临时文件 file URI（napcat 靠扩展名转 silk）。"""
    sent = []

    def fake_api(action, payload):
        sent.append((action, payload))
        return {"ok": True, "data": {"message_id": 1}}

    monkeypatch.setattr(adapter, "_api", fake_api)
    r = adapter.send_voice("private:222", "好的，明天多云，记得带伞。")
    assert r["ok"] is True
    action, payload = sent[0]
    assert action == "send_private_msg"
    seg = payload["message"][0]
    assert seg["type"] == "record"
    assert seg["data"]["file"].startswith("file:///")
    assert seg["data"]["file"].lower().endswith(".wav"), "必须带 .wav 扩展名供 napcat 转码"


def test_send_voice_remote_napcat_uses_base64(adapter, fake_voice_caps, monkeypatch):
    """远端 napcat（无文件系统共享）：base64:// 直传。"""
    sent = []
    adapter.http_url = "http://10.0.0.8:3000"

    def fake_api(action, payload):
        sent.append((action, payload))
        return {"ok": True, "data": {}}

    monkeypatch.setattr(adapter, "_api", fake_api)
    r = adapter.send_voice("private:222", "你好")
    assert r["ok"] is True
    assert sent[0][1]["message"][0]["data"]["file"].startswith("base64://")


def test_send_voice_local_fallback_to_base64(adapter, fake_voice_caps, monkeypatch):
    """本机 file URI 被拒 → base64 兜底一次。"""
    calls = []

    def fake_api(action, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"ok": False, "error": "file not supported"}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(adapter, "_api", fake_api)
    r = adapter.send_voice("private:222", "你好")
    assert r["ok"] is True
    assert len(calls) == 2
    assert calls[1]["message"][0]["data"]["file"].startswith("base64://")


def test_send_voice_without_plugin_degrades(adapter):
    """voice 插件未启用 → ok=False（dispatch 层降级纯文本）。"""
    r = adapter.send_voice("private:222", "你好")
    assert r["ok"] is False and "未启用" in r["error"]


# === dispatch：语音回发与降级 ===

class _VoiceReply:
    def __init__(self, fail=False):
        self.fail = fail
        self.texts = []
        self.voices = []

    def send_text(self, text):
        self.texts.append(text)
        return {"ok": True}

    def send_voice(self, text):
        if self.fail:
            return {"ok": False, "error": "TTS 失败"}
        self.voices.append(text)
        return {"ok": True}


class _PlainReply:
    def __init__(self):
        self.texts = []

    def send_text(self, text):
        self.texts.append(text)
        return {"ok": True}


def test_dispatch_voice_reply_uses_send_voice():
    reply = _VoiceReply()
    gw_dispatch._send_voice_reply(reply, "回答内容", wanted=True)
    assert reply.voices == ["回答内容"] and reply.texts == []


def test_dispatch_voice_reply_failure_falls_back_to_text():
    reply = _VoiceReply(fail=True)
    gw_dispatch._send_voice_reply(reply, "回答内容", wanted=True)
    assert reply.voices == [] and reply.texts == ["回答内容"]


def test_dispatch_plain_reply_ignores_voice_flag():
    reply = _PlainReply()
    gw_dispatch._send_voice_reply(reply, "回答内容", wanted=True)
    assert reply.texts == ["回答内容"]


def test_dispatch_wanted_false_sends_text():
    reply = _VoiceReply()
    gw_dispatch._send_voice_reply(reply, "回答内容", wanted=False)
    assert reply.voices == [] and reply.texts == ["回答内容"]


def test_capability_helpers_exported():
    """注册表助手存在性（napcat 转写/回发依赖的接口面）。"""
    assert callable(get_capability)
    register_capability("voice_transcribe", lambda b: "x")
    try:
        assert get_capability("voice_transcribe") is not None
    finally:
        unregister_capability("voice_transcribe")
