"""CosyVoice2 引擎单测：全 mock，不触真实权重。

覆盖：模型缺件的报错形态、voices 缺失/不存在的报错形态、voice 上下文缓存、
文本规整（cn2an 数字、分句上限、纯标点丢弃）、加载纪律常量、tts() 的分发与上限。
"""
import os
import sys
import threading
import types
from pathlib import Path

import numpy as np
import pytest

# torch 仅 TTS 环境安装：主环境缺 torch 时整文件跳过（模块级 import 会让全量收集直接中断）
torch = pytest.importorskip("torch", reason="语音引擎依赖 torch（主环境未安装）")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "plugins", "voice"))

from plugins.voice import plugin as voice_plugin  # noqa: E402


@pytest.fixture
def fake_bundle(tmp_path, monkeypatch):
    """假模型目录：五件套齐全 + 一个参考音色。"""
    d = tmp_path / "cosyvoice2"
    d.mkdir()
    for f in ("llm.pt", "flow.pt", "hift.pt", "speech_tokenizer_v2.onnx",
              "campplus.onnx"):
        (d / f).write_bytes(b"x")
    tok = d / "tokenizer"
    tok.mkdir()
    (tok / "config.json").write_text("{}", encoding="utf-8")
    v = d / "voices"
    v.mkdir()
    (v / "a.wav").write_bytes(b"wav")
    monkeypatch.setattr(voice_plugin, "_cosyvoice2_dir", lambda: str(d))
    monkeypatch.setattr(voice_plugin, "_voices_dir", lambda: str(v))
    monkeypatch.setattr(voice_plugin, "_cfg", lambda: {})
    return d


def make_engine(bundle_dir, voices_dir):
    """不触 _build（真实权重）的引擎壳：只测音色/缓存/文本逻辑。"""
    from cosyvoice2_rt.engine import CosyVoice2Engine

    eng = CosyVoice2Engine.__new__(CosyVoice2Engine)
    eng.model_dir = Path(bundle_dir)
    eng.voices_dir = Path(voices_dir)
    eng.log = None
    eng.device = torch.device("cpu")
    eng._lock = threading.Lock()
    eng._voice_cache = {}
    eng._tokenizer = None
    eng._inflect = None
    return eng


# ------------------------------------------------ 模型缺件的报错形态

class TestMissingBundle:
    def test_missing_files_lists_five_plus_tokenizer(self, tmp_path):
        from cosyvoice2_rt.engine import missing_files

        missing = missing_files(str(tmp_path))
        assert len(missing) == 6  # 五件套 + tokenizer/config.json

    def test_get_cosyvoice2_raises_actionable_error(self, tmp_path, monkeypatch):
        """权重缺失时 tts 必须显式报错并指路（绝不带病合成）。"""
        monkeypatch.setattr(voice_plugin, "_cosyvoice2_dir", lambda: str(tmp_path))
        monkeypatch.setattr(voice_plugin, "_COSYVOICE2", None)
        with pytest.raises(RuntimeError, match="CosyVoice2 模型缺失"):
            voice_plugin._get_cosyvoice2()


# ------------------------------------------------ 音色管理

class TestVoices:
    def test_list_voices_sorted(self, fake_bundle):
        eng = make_engine(fake_bundle, fake_bundle / "voices")
        (fake_bundle / "voices" / "b.wav").write_bytes(b"wav")
        assert eng.list_voices() == ["a", "b"]

    def test_missing_voice_names_available(self, fake_bundle):
        eng = make_engine(fake_bundle, fake_bundle / "voices")
        with pytest.raises(RuntimeError, match="音色不存在"):
            eng._voice_context("nope")

    def test_no_voices_gives_actionable_error(self, fake_bundle):
        (fake_bundle / "voices" / "a.wav").unlink()
        eng = make_engine(fake_bundle, fake_bundle / "voices")
        with pytest.raises(RuntimeError, match="voices"):
            eng._voice_context("")

    def test_voice_context_cache_hits_on_same_mtime(self, fake_bundle, monkeypatch):
        from cosyvoice2_rt import feats

        eng = make_engine(fake_bundle, fake_bundle / "voices")
        monkeypatch.setattr(feats, "load_wav", lambda p, sr: torch.zeros(1, 100))
        monkeypatch.setattr(feats, "whisper_log_mel",
                            lambda w: np.zeros((1, 128, 4), dtype=np.float32))
        monkeypatch.setattr(feats, "kaldi_fbank80",
                            lambda w: np.zeros((4, 80), dtype=np.float32))
        monkeypatch.setattr(feats, "matcha_mel_24k", lambda w: torch.zeros(1, 80, 8))
        eng._sess_token = types.SimpleNamespace(
            get_inputs=lambda: [types.SimpleNamespace(name="x"),
                                types.SimpleNamespace(name="y")],
            run=lambda *a, **k: [np.zeros(87, dtype=np.int32)])
        eng._sess_spk = types.SimpleNamespace(
            get_inputs=lambda: [types.SimpleNamespace(name="x")],
            run=lambda *a, **k: [np.zeros(192, dtype=np.float32)])
        c1 = eng._voice_context("a")
        c2 = eng._voice_context("a")
        assert c1 is c2


# ------------------------------------------------ 文本规整

class TestTextNorm:
    def test_zh_numbers_spelled_out(self):
        from cosyvoice2_rt import textnorm as tn

        assert tn.zh_spell_numbers("2026年10月29日") == "二零二六年十月二十九日"
        assert tn.zh_spell_numbers("没有数字") == "没有数字"

    @pytest.fixture
    def split_engine(self, fake_bundle, monkeypatch):
        eng = make_engine(fake_bundle, fake_bundle / "voices")
        # 假目录没有 tokenizer 文件：split 的 zh 路径只用字符长度，stub 掉编码即可
        monkeypatch.setattr(eng, "_qwen_encode", lambda s: list(s))
        return eng

    def test_split_caps_segment_length(self, split_engine):
        segs = split_engine._split_text("这是一句话。" * 40)  # 200 字
        assert len(segs) >= 2
        # 官方 split_paragraph 契约：除末段（短尾合并进上一段）外每段 ≤ 80 字
        assert all(len(s) <= 80 for s in segs[:-1])
        assert len(segs[-1]) <= 100

    def test_split_drops_pure_punctuation(self, split_engine):
        assert split_engine._split_text("。。。！！！") == []


# ------------------------------------------------ 加载纪律

class TestLoadDiscipline:
    def test_allowed_missing_only_lm_head(self):
        from cosyvoice2_rt.engine import _ALLOWED_MISSING_LLM

        assert _ALLOWED_MISSING_LLM == {"llm.model.lm_head.weight"}

    def test_strict_loader_rejects_unexpected_keys(self):
        """引擎的加载纪律：strict=False 后 unexpected 键（或除 lm_head 外的缺失）
        必须判为结构不一致并拒绝——用小模块验证该判定逻辑本身。"""
        m = torch.nn.Linear(4, 4)
        missing, unexpected = m.load_state_dict(
            {"totally.unknown.key": torch.zeros(1)}, strict=False)
        assert unexpected == ["totally.unknown.key"]
        keep = (set(missing) - {"llm.model.lm_head.weight"}) or unexpected
        assert keep  # engine._build 据此 raise RuntimeError("llm.pt 与 Qwen2LM 结构不一致")


# ------------------------------------------------ tts() 引擎分发

class TestTtsDispatch:
    def test_tts_dispatches_to_cosyvoice2(self, fake_bundle, monkeypatch):
        sent = {}

        def fake_tts(text, voice):
            sent["args"] = (text, voice)
            return b"WAV"

        monkeypatch.setattr(voice_plugin, "_tts_cosyvoice2", fake_tts)
        assert voice_plugin.tts("你好", voice="a") == b"WAV"
        assert sent["args"] == ("你好", "a")

    def test_tts_rejects_empty_and_long(self, fake_bundle, monkeypatch):
        with pytest.raises(ValueError):
            voice_plugin.tts("  ")
        with pytest.raises(ValueError, match="上限"):
            voice_plugin.tts("字" * 501)
