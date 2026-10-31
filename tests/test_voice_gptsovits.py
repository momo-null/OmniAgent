"""GPT-SoVITS 引擎适配单测：全 mock，不触真实权重、不需要 torch。

覆盖：引擎模型缺件报错、voices 扫描与回退、合成块拼接（tuple / ndarray 两种
产出格式）、plugin 侧路径解析（模型目录 / 解释器 / 权重默认与覆盖）、tts() 分发
与上限、worker 协议参数校验。前置：sys.path 指向 plugins/voice。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugins", "voice"))

from plugins.voice import plugin as voice_plugin  # noqa: E402
from gptsovits_rt.engine import GptSovitsEngine  # noqa: E402


@pytest.fixture
def model_bundle(tmp_path, monkeypatch):
    """假模型目录：pretrained_models 两件套 + weights 两件 + 一个音色。"""
    d = tmp_path / "gptsovits"
    for sub in ("pretrained_models/chinese-hubert-base",
                "pretrained_models/chinese-roberta-wwm-ext-large"):
        (d / sub).mkdir(parents=True)
    (d / "weights").mkdir()
    (d / "weights" / "doro_e12_s288.pth").write_bytes(b"v")
    (d / "weights" / "doro-e30.ckpt").write_bytes(b"t")
    v = d / "voices"
    v.mkdir()
    (v / "duorou.wav").write_bytes(b"wav")
    (v / "duorou.txt").write_text("在你耳边说话。", encoding="utf-8")
    monkeypatch.setattr(voice_plugin, "_cfg", lambda: {})
    return d


def make_engine(gs_dir, bundle_dir, voices_dir):
    """不触模型加载的引擎壳：只测音色扫描与合成块拼装。"""
    eng = GptSovitsEngine.__new__(GptSovitsEngine)
    eng._voices_dir = str(voices_dir)
    eng._tts = None
    return eng


# ------------------------------------------------ 引擎：模型缺件

class TestMissingModel:
    def test_missing_weights_lists_paths(self, tmp_path):
        gs = tmp_path / "gs"
        gs.mkdir()
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(FileNotFoundError, match="模型缺失"):
            GptSovitsEngine(str(gs), str(d), str(d / "voices"))

    def test_missing_repo_gives_clear_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="GPT-SoVITS 仓库不存在"):
            GptSovitsEngine(str(tmp_path / "nope"), str(tmp_path), str(tmp_path))


# ------------------------------------------------ 引擎：音色与合成

class TestEngine:
    def test_list_voices_sorted(self, model_bundle):
        (model_bundle / "voices" / "aaa.wav").write_bytes(b"w")
        eng = make_engine(None, model_bundle, model_bundle / "voices")
        assert eng.list_voices() == ["aaa", "duorou"]

    def test_synth_joins_tuple_and_array_chunks(self, model_bundle):
        eng = make_engine(None, model_bundle, model_bundle / "voices")

        class FakeTts:
            def run(self, inputs):
                assert inputs["ref_audio_path"].endswith("duorou.wav")
                assert inputs["prompt_text"] == "在你耳边说话。"
                yield (32000, np.array([100, -200], dtype=np.int16))
                yield np.array([300], dtype=np.int16)

        eng._tts = FakeTts()
        samples, sr = eng.synthesize("你好", "duorou")
        assert sr == 32000
        np.testing.assert_allclose(samples, [100 / 32768, -200 / 32768, 300 / 32768],
                                   rtol=1e-6)

    def test_synth_falls_back_to_first_voice(self, model_bundle):
        eng = make_engine(None, model_bundle, model_bundle / "voices")
        seen = {}

        class FakeTts:
            def run(self, inputs):
                seen["ref"] = inputs["ref_audio_path"]
                yield (32000, np.zeros(4, dtype=np.int16))

        eng._tts = FakeTts()
        eng.synthesize("你好", "不存在")
        assert seen["ref"].endswith("duorou.wav")

    def test_synth_no_voices_raises(self, tmp_path):
        v = tmp_path / "voices"
        v.mkdir()
        eng = make_engine(None, tmp_path, v)
        with pytest.raises(RuntimeError, match="没有任何音色"):
            eng.synthesize("你好", "duorou")

    def test_synth_empty_output_raises(self, model_bundle):
        eng = make_engine(None, model_bundle, model_bundle / "voices")

        class FakeTts:
            def run(self, inputs):
                return iter(())

        eng._tts = FakeTts()
        with pytest.raises(RuntimeError, match="为空"):
            eng.synthesize("你好", "duorou")


# ------------------------------------------------ plugin：路径解析

class TestPaths:
    def test_default_dirs(self, monkeypatch):
        monkeypatch.setattr(voice_plugin, "_cfg", lambda: {})
        assert voice_plugin._gptsovits_dir().endswith(
            os.path.join(".omniagent", "models", "gptsovits"))
        assert voice_plugin._voices_dir().endswith(
            os.path.join("gptsovits", "voices"))

    def test_weights_default_pair(self, monkeypatch):
        monkeypatch.setattr(voice_plugin, "_cfg", lambda: {})
        vits, t2s = voice_plugin._tts_weights()
        assert vits.endswith(os.path.join("weights", "doro_e12_s288.pth"))
        assert t2s.endswith(os.path.join("weights", "doro-e30.ckpt"))

    def test_weights_config_override_relative(self, tmp_path, monkeypatch):
        monkeypatch.setattr(voice_plugin, "_cfg",
                            lambda: {"gptsovits_dir": str(tmp_path),
                                     "tts_vits": "weights/x.pth"})
        vits, _ = voice_plugin._tts_weights()
        assert vits == str(tmp_path / "weights" / "x.pth")

    def test_tts_python_falls_back_to_current(self, tmp_path, monkeypatch):
        monkeypatch.setattr(voice_plugin, "_cfg", lambda: {"gptsovits_repo": str(tmp_path)})
        assert voice_plugin._tts_python() == sys.executable

    def test_tts_python_prefers_venv(self, tmp_path, monkeypatch):
        venv = tmp_path / ".venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_bytes(b"")
        monkeypatch.setattr(voice_plugin, "_cfg",
                            lambda: {"gptsovits_repo": str(tmp_path)})
        assert voice_plugin._tts_python() == str(venv / "python.exe")


# ------------------------------------------------ tts() 分发与上限

class TestTtsDispatch:
    def test_tts_dispatches_to_worker(self, monkeypatch):
        sent = {}

        def fake_synth(text, voice, eq, default_voice):
            sent["args"] = (text, voice, eq, default_voice)
            return b"WAV"

        monkeypatch.setattr(voice_plugin._WORKER, "synthesize", fake_synth)
        monkeypatch.setattr(voice_plugin, "_cfg", lambda: {"tts_voice": "duorou"})
        assert voice_plugin.tts("你好") == b"WAV"
        assert sent["args"] == ("你好", "", False, "duorou")

    def test_tts_rejects_empty_and_long(self, monkeypatch):
        monkeypatch.setattr(voice_plugin, "_cfg", lambda: {})
        with pytest.raises(ValueError):
            voice_plugin.tts("  ")
        with pytest.raises(ValueError, match="上限"):
            voice_plugin.tts("字" * 501)


# ------------------------------------------------ worker：协议参数校验

class TestWorkerProtocol:
    def test_usage_requires_four_args(self, capsys):
        import importlib

        from plugins.voice import tts_worker

        importlib.reload(tts_worker)
        sys.argv = ["tts_worker.py", "a", "b"]
        assert tts_worker.main() == 2
        assert "usage" in capsys.readouterr().err
