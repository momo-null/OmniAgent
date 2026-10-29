"""语音插件：本地 STT / TTS（SenseVoice + Kokoro，ONNXRuntime）。

形态与 ocr 同款——**独立可插拔、懒加载、无状态**：内核与前端只经两个
HTTP 端点触达（``/api/audio/transcribe`` / ``/api/audio/tts``，由
``backend/api/routers/audio_api.py`` 薄壳转发），本插件不注册任何 agent
工具，内核（omni_core）对它零 import——语音对大脑不可见（单链路红线）。

能力（capability 注册制，插件未启用 = 能力不存在 = 端点 404）：
- ``voice_transcribe(wav_bytes) -> str``：16kHz 单声道 WAV → 中/英文文本；
- ``voice_tts(text, voice="") -> bytes``：文本 → 16 位 PCM WAV（24kHz）。

懒加载：sherpa-onnx / kokoro-onnx / 模型权重只在**首次调用**时构造
（import 本包零开销）；``enabled=false``（默认）时进程内无 ONNXRuntime。

模型（约 1GB，放 ``~/.omniagent/models/``，路径可用 plugins/voice.yaml 覆盖）：
- SenseVoice：``models/sense-voice/{model.int8.onnx, tokens.txt}``
  （sherpa-onnx releases 的 sense-voice int8 包，中英日韩粤）
- Kokoro：``models/kokoro/{kokoro-v1.0.onnx, voices.bin}``（HuggingFace kokoro-onnx）
"""
from __future__ import annotations

import io
import os
import re
from typing import Any, Dict

from utils import get_logger

logger = get_logger("plugins.voice")

_STT = None
_TTS: Dict[str, Any] = {}

_TTS_MAX_CHARS = 500
_STT_MAX_SECONDS = 60


def _cfg() -> Dict[str, Any]:
    try:
        from omni_core.tools.loader import plugin_config

        return plugin_config("voice") or {}
    except Exception:
        return {}


def _default_model(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".omniagent", "models", *parts)


def _get_stt():
    """SenseVoice 识别器单例（首次调用才构造；缺模型给可执行提示）。"""
    global _STT
    if _STT is None:
        model_dir = str(_cfg().get("stt_model_dir") or _default_model("sense-voice"))
        model = os.path.join(model_dir, "model.int8.onnx")
        tokens = os.path.join(model_dir, "tokens.txt")
        missing = [p for p in (model, tokens) if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(
                "SenseVoice 模型缺失: " + ", ".join(missing)
                + "（下载见 doc/plans/voice-m3-2026-10-29.md §模型）")
        import sherpa_onnx

        _STT = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model, tokens=tokens, use_itn=True, num_threads=2)
    return _STT


def _decode_wav_16k_mono(wav_bytes: bytes):
    """WAV bytes → 16kHz float32 单声道。浏览器一般已给 16k；采样率不符时兜底重采样
    （整倍数走平均抽取，否则线性插值——语音识别对它不敏感）。"""
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(wav_bytes), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    target = 16000
    if sr != target and sr > 0:
        if sr % target == 0:
            n = sr // target
            mono = mono[: len(mono) // n * n].reshape(-1, n).mean(axis=1)
        else:
            idx = np.linspace(0.0, max(len(mono) - 1, 0.0),
                              int(len(mono) * target / sr))
            mono = np.interp(idx, np.arange(len(mono)), mono)
    cap = target * _STT_MAX_SECONDS
    return mono[:cap] if len(mono) > cap else mono


def transcribe(wav_bytes: bytes) -> str:
    """WAV（16k 单声道为宜）→ 文本。空音频返回空串；模型/依赖缺失抛 RuntimeError。"""
    if not wav_bytes:
        return ""
    samples = _decode_wav_16k_mono(wav_bytes)
    if samples.size == 0:
        return ""
    recognizer = _get_stt()
    stream = recognizer.create_stream()
    stream.accept_waveform(16000, samples)
    recognizer.decode_stream(stream)
    return str(getattr(stream.result, "text", "") or "").strip()


def _voices_beside(model: str, base: str) -> str:
    """没配音色包时按模型名猜配套的 voices 文件（v1.1-zh 与 v1.0 音色不通用）。"""
    d = os.path.dirname(model) or base
    guesses = (["voices-v1.1-zh.bin", "voices.bin"]
               if "v1.1-zh" in os.path.basename(model).lower() else ["voices.bin"])
    for name in guesses:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return os.path.join(d, guesses[0])


def _model_files(cfg: Dict[str, Any]) -> tuple:
    """(onnx, voices) 路径：显式配置优先，否则按目录里**已存在**的模型自动选。

    中文模型与英文模型是两套权重（v1.1-zh 官方中文模型，中英混读；v1.0 英文模型
    念中文会带浓重洋腔）。目录里放了哪套就用哪套，避免把模型名写死在代码里。
    """
    base = str(cfg.get("model_dir") or _default_model("kokoro"))
    model = str(cfg.get("tts_model") or "")
    voices = str(cfg.get("tts_voices") or "")
    if not model:
        zh_model = os.path.join(base, "model-v1.1-zh.onnx")
        model = zh_model if os.path.isfile(zh_model) else os.path.join(base, "kokoro-v1.0.onnx")
    return model, voices or _voices_beside(model, base)


def _get_tts():
    """Kokoro 合成器单例（首次调用才构造）。"""
    if _TTS.get("obj") is None:
        model, voices = _model_files(_cfg())
        missing = [p for p in (model, voices) if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(
                "Kokoro 模型缺失: " + ", ".join(missing)
                + "（下载见 doc/plans/voice-m3-2026-10-29.md §模型）")
        from kokoro_onnx import Kokoro

        _TTS["obj"] = Kokoro(model, voices)
    return _TTS["obj"]


#: CJK 统一表意文字（只做脚本判别，不含任何词表/场景假设）
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _espeak_lang(text: str) -> str:
    """文本 → espeak 语言码（``plugins/voice.yaml`` 的 ``tts_language`` 可覆盖）。

    注意 espeak 的中文码是 ``cmn`` 而不是 ``zh``——传 ``zh``/``zh-cn`` 会直接抛
    ``language "zh" is not supported by the espeak backend``；传错的码不会报错
    （如英文码喂中文）而是逐字念 "Chinese letter" 的怪音，故必须按文本选码。
    只做字符集判别：含 CJK → ``cmn``，否则 ``en-us``。配置写死某个码时以配置为准。
    """
    configured = str(_cfg().get("tts_language") or "").strip()
    if configured and configured.lower() != "auto":
        return configured
    return "cmn" if _CJK_RE.search(text) else "en-us"


#: 拉丁字母（中英混排要走 espeak 整句音素化，misaki 会把英文原样留下念字母）
_LATIN_RE = re.compile(r"[A-Za-z]")

_ZH_G2P: Any = None


def _default_voice(synth: Any, lang: str) -> str:
    """未配置音色时挑一个：中文优先 zf_/zm_ 前缀，否则取排序第一个。"""
    names = sorted(str(n) for n in (getattr(synth, "voices", {}) or {}))
    if not names:
        return ""
    if lang == "cmn":
        zh = [n for n in names if n.startswith(("zf_", "zm_"))]
        if zh:
            return zh[0]
    return names[0]


def _zh_phonemes(text: str, synth: Any) -> str:
    """中文文本 → IPA 音素串（pypinyin 直转，不走 espeak）。

    为什么不用 ``ZHG2P(version="1.1")``：那一版输出注音（bopomofo），而 Kokoro 的
    onnx 词表是 IPA，不匹配时 token 几乎全被丢弃，合成出来是 0.3s 噪音。legacy
    模式（``ZHG2P()``）才是 pypinyin→IPA，与词表一致。
    仍做命中率校验（<60% 视为不兼容 → 空串，调用方退回 espeak）。
    """
    global _ZH_G2P
    if _ZH_G2P is False:
        return ""
    if _ZH_G2P is None:
        try:
            from misaki import zh

            _ZH_G2P = zh.ZHG2P()
        except Exception as e:  # noqa: BLE001 —— 缺包/缺词典都只是降级理由
            logger.warning("misaki 中文 G2P 不可用（退回 espeak）: %s: %s",
                           type(e).__name__, e)
            _ZH_G2P = False
            return ""
    try:
        phonemes = str(_ZH_G2P(text)[0]).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("中文音素转换失败（退回 espeak）: %s: %s", type(e).__name__, e)
        return ""
    vocab = getattr(getattr(synth, "tokenizer", None), "vocab", None) or {}
    body = phonemes.replace(" ", "")
    if not vocab or not body:
        return ""
    hit = sum(1 for c in body if c in vocab)
    if hit < 0.6 * len(body):
        logger.warning("中文音素与模型词表不匹配（%d/%d），退回 espeak", hit, len(body))
        return ""
    return phonemes


def tts(text: str, voice: str = "") -> bytes:
    """文本 → 16 位 PCM WAV bytes。空文本抛 ValueError；上限 500 字。

    失败即抛（调用方降级纯文本）——绝不返回"能播但听不懂"的怪音：语言码传错时
    kokoro 会用英文规则念中文，现象是每个汉字读成 "Chinese letter"，音频非空、
    时长正常，事后无法从波形分辨。

    纯中文优先走 misaki 的 IPA 音素（中文模型发音准）；含拉丁字母的混排句、
    或 misaki 不可用/词表不匹配时退回 espeak 整句音素化（``lang="cmn"`` 中英混读）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("text 为空")
    if len(text) > _TTS_MAX_CHARS:
        raise ValueError(f"文本过长（上限 {_TTS_MAX_CHARS} 字）")
    lang = _espeak_lang(text)
    synth = _get_tts()
    name = voice or str(_cfg().get("tts_voice") or "") or _default_voice(synth, lang)

    samples = None
    sr = 0
    if lang == "cmn" and not _LATIN_RE.search(text):
        phonemes = _zh_phonemes(text, synth)
        if phonemes:
            samples, sr = synth.create(phonemes, voice=name, speed=1.0, is_phonemes=True)
    if samples is None:
        try:
            samples, sr = synth.create(text, voice=name, speed=1.0, lang=lang)
        except TypeError as e:
            # 旧版 kokoro-onnx 的 create() 只有 (text, voice, speed)，没有 lang：
            # 补不了语言参数就等于没有中文发音能力 → 明确失败，别念怪音。
            if lang != "en-us":
                raise RuntimeError(
                    f"当前 kokoro-onnx 不支持指定语言（{lang}），该文本无法合成") from e
            samples, sr = synth.create(text, voice=name, speed=1.0)
    if samples is None or len(samples) == 0:
        raise RuntimeError("语音合成结果为空")
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def startup(ctx) -> None:
    """注册 capability（audio_api 薄壳按名转发；未启用 = 端点 404）。"""
    from omni_core.tools.capability import register_capability

    register_capability("voice_transcribe", transcribe)
    register_capability("voice_tts", tts)
    logger.info("语音插件已启用（STT/TTS 将在首次调用时懒加载）")


def shutdown() -> None:
    """注销能力并丢弃引擎单例（释放模型内存）。"""
    from omni_core.tools.capability import unregister_capability

    unregister_capability("voice_transcribe")
    unregister_capability("voice_tts")
    global _STT
    _STT = None
    _TTS.clear()
