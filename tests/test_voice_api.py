"""语音端点（/api/audio/*）与 voice 插件单测：capability 转发、错误形态、
WAV 解码兜底重采样。全部 mock，不触真实模型。
"""
import io
import os
import sys
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api.routers.audio_api as audio_api  # noqa: E402
import backend.server as server_mod  # noqa: E402
from plugins.voice import plugin as voice_plugin  # noqa: E402


@pytest.fixture
def client():
    with TestClient(server_mod.app) as c:
        yield c


def _wav_bytes(samples, sr=16000) -> bytes:
    """标准库生成单声道 16 位 PCM WAV（不经 soundfile，保证测试独立性）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


# ------------------------------------------------ 端点（capability 薄壳转发）

class TestAudioEndpoints:
    def test_transcribe_404_when_plugin_disabled(self, client, monkeypatch):
        monkeypatch.setattr(audio_api, "get_capability", lambda name: None)
        r = client.post("/api/audio/transcribe", content=b"xx")
        assert r.status_code == 404
        assert "未启用" in r.json()["error"]

    def test_tts_404_when_plugin_disabled(self, client, monkeypatch):
        monkeypatch.setattr(audio_api, "get_capability", lambda name: None)
        r = client.post("/api/audio/tts", json={"text": "hi"})
        assert r.status_code == 404

    def test_transcribe_ok(self, client, monkeypatch):
        seen = {}

        def fake(wav_bytes):
            seen["len"] = len(wav_bytes)
            return "你好世界"

        monkeypatch.setattr(audio_api, "get_capability",
                            lambda name: fake if name == "voice_transcribe" else None)
        r = client.post("/api/audio/transcribe", content=b"RIFFfake",
                        headers={"Content-Type": "audio/wav"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "text": "你好世界"}
        assert seen["len"] == 8

    def test_transcribe_empty_audio_400(self, client, monkeypatch):
        monkeypatch.setattr(audio_api, "get_capability", lambda name: (lambda b: "x"))
        r = client.post("/api/audio/transcribe", content=b"")
        assert r.status_code == 400

    def test_transcribe_oversize_413(self, client, monkeypatch):
        monkeypatch.setattr(audio_api, "get_capability", lambda name: (lambda b: "x"))
        r = client.post("/api/audio/transcribe", content=b"x" * (10 * 1024 * 1024 + 1))
        assert r.status_code == 413

    def test_transcribe_engine_error_503(self, client, monkeypatch):
        def boom(_b):
            raise RuntimeError("SenseVoice 模型缺失: model.int8.onnx")

        monkeypatch.setattr(audio_api, "get_capability", lambda name: boom)
        r = client.post("/api/audio/transcribe", content=b"RIFF")
        assert r.status_code == 503
        assert "模型缺失" in r.json()["error"]

    def test_tts_ok_wav_media(self, client, monkeypatch):
        monkeypatch.setattr(
            audio_api, "get_capability",
            lambda name: (lambda t, v: b"WAVDATA") if name == "voice_tts" else None)
        r = client.post("/api/audio/tts", json={"text": "你好"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.content == b"WAVDATA"

    def test_tts_empty_and_too_long_400(self, client, monkeypatch):
        monkeypatch.setattr(
            audio_api, "get_capability",
            lambda name: (lambda t, v: b"W") if name == "voice_tts" else None)
        assert client.post("/api/audio/tts", json={"text": "  "}).status_code == 400
        assert client.post("/api/audio/tts", json={"text": "长" * 501}).status_code == 400


# ------------------------------------------------ 插件引擎（懒加载 / 解码 / 上限）

class TestVoicePluginEngine:
    def test_capability_lifecycle(self):
        from omni_core.tools.capability import get_capability

        voice_plugin.startup(None)
        try:
            assert callable(get_capability("voice_transcribe"))
            assert callable(get_capability("voice_tts"))
        finally:
            voice_plugin.shutdown()
        assert get_capability("voice_transcribe") is None
        assert get_capability("voice_tts") is None

    def test_transcribe_empty_returns_empty_without_engine(self, monkeypatch):
        def _no_engine():
            raise AssertionError("空音频不该构造引擎")

        monkeypatch.setattr(voice_plugin, "_get_stt", _no_engine)
        assert voice_plugin.transcribe(b"") == ""

    def test_transcribe_resamples_48k_to_16k(self, monkeypatch):
        captured = {}

        class FakeStream:
            result = type("R", (), {"text": "识别结果"})()

            def accept_waveform(self, sr, samples):
                captured["sr"] = sr
                captured["samples"] = samples

        class FakeRec:
            def create_stream(self):
                s = FakeStream()
                captured["stream"] = s
                return s

            def decode_stream(self, s):
                captured["called"] = True

        monkeypatch.setattr(voice_plugin, "_get_stt", lambda: FakeRec())
        # 480 样本 @48kHz = 10ms → 平均抽取为 160 样本 @16kHz
        wav = _wav_bytes(np.sin(np.linspace(0.0, 100.0, 480)), sr=48000)
        assert voice_plugin.transcribe(wav) == "识别结果"
        assert captured["called"] is True
        assert captured["sr"] == 16000
        assert captured["samples"].shape == (160,), "48k 必须被抽取成 16k"

    def test_transcribe_caps_60s(self, monkeypatch):
        class FakeStream:
            result = type("R", (), {"text": ""})()

            def accept_waveform(self, sr, samples):
                FakeStream.got = samples

        class FakeRec:
            def create_stream(self):
                return FakeStream()

            def decode_stream(self, s):
                pass

        monkeypatch.setattr(voice_plugin, "_get_stt", lambda: FakeRec())
        wav = _wav_bytes(np.zeros(16000 * 61, dtype="float32"), sr=16000)
        assert voice_plugin.transcribe(wav) == ""
        assert len(FakeStream.got) == 16000 * 60, "61s 音频必须截断到 60s"

    def test_tts_produces_wav_and_enforces_caps(self, monkeypatch):
        monkeypatch.setattr(voice_plugin, "_tts_dispatch",
                            lambda text, voice: b"RIFF" + b"\0" * 4)
        out = voice_plugin.tts("你好")
        assert out[:4] == b"RIFF"
        with pytest.raises(ValueError):
            voice_plugin.tts("x" * 501)
        with pytest.raises(ValueError):
            voice_plugin.tts("   ")
