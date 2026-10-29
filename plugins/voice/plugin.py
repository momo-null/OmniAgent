"""语音插件：本地 STT / TTS（SenseVoice + CosyVoice2，懒加载）。

形态与 ocr 同款——**独立可插拔、懒加载、无状态**：内核与前端只经两个
HTTP 端点触达（``/api/audio/transcribe`` / ``/api/audio/tts``，由
``backend/api/routers/audio_api.py`` 薄壳转发），本插件不注册任何 agent
工具，内核（omni_core）对它零 import——语音对大脑不可见（单链路红线）。

能力（capability 注册制，插件未启用 = 能力不存在 = 端点 404）：
- ``voice_transcribe(wav_bytes) -> str``：16kHz 单声道 WAV → 中/英文文本；
- ``voice_tts(text, voice="") -> bytes``：文本 → 16 位 PCM WAV（24kHz），
  ``voice`` = ``voices/`` 下的音色名（一段参考音频 = 一个音色）。

TTS = CosyVoice2 零样本音色克隆（单引擎）。推理代码在 ``cosyvoice2_rt/``
（vendored，来源与许可见其 ``__init__.py``）；模型布局、transformers 4.4x
钉子、BlankEN merges 词表等必读事项见
``doc/plans/voice-m3-2026-10-29.md`` §10。

懒加载：sherpa-onnx / torch / 模型权重只在**首次调用**时构造（import 本包
零开销）；``enabled=false``（默认）时进程内无 ONNXRuntime/torch。

模型（放 ``~/.omniagent/models/``，路径可用 plugins/voice.yaml 覆盖）：
- SenseVoice：``models/sense-voice/{model.int8.onnx, tokens.txt}``
  （sherpa-onnx releases 的 sense-voice int8 包，中英日韩粤）
- CosyVoice2：``models/cosyvoice2/``——五件套（llm.pt / flow.pt / hift.pt /
  speech_tokenizer_v2.onnx / campplus.onnx）+ ``tokenizer/``（词表必须用
  模型仓库内 CosyVoice-BlankEN 那份）+ ``voices/``（参考音色 wav）
"""
from __future__ import annotations

import io
import os
import sys

from utils import get_logger

logger = get_logger("plugins.voice")

_STT = None
_COSYVOICE2 = None

_TTS_MAX_CHARS = 500
_STT_MAX_SECONDS = 60


def _cfg() -> dict:
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


def _cosyvoice2_dir() -> str:
    return str((_cfg().get("cosyvoice2_dir") or _default_model("cosyvoice2")))


def _voices_dir() -> str:
    return str(_cfg().get("voices_dir") or os.path.join(_cosyvoice2_dir(), "voices"))


def _get_cosyvoice2():
    """CosyVoice2 引擎单例（首次调用才构造，torch/CUDA 也在此刻才进进程）。

    缺权重/缺依赖在这里抛可执行错误（调用方降级纯文本），绝不带病合成。
    """
    global _COSYVOICE2
    if _COSYVOICE2 is None:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from cosyvoice2_rt import CosyVoice2Engine

        _COSYVOICE2 = CosyVoice2Engine(_cosyvoice2_dir(), _voices_dir(), logger=logger)
    return _COSYVOICE2


def _tts_cosyvoice2(text: str, voice: str) -> bytes:
    engine = _get_cosyvoice2()
    name = voice  # 调用方显式指定的音色名：不存在就报错
    if not name:
        default = str(_cfg().get("tts_voice") or "")
        if default and default in engine.list_voices():
            name = default
        elif default:
            # 陈旧的默认音色名只警告，不挡合成
            logger.warning("tts_voice=%s 不是 voices/ 下的音色名，改用第一个可用音色", default)
    samples, sr = engine.synthesize(text, name)
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def tts(text: str, voice: str = "") -> bytes:
    """文本 → 16 位 PCM WAV bytes（24kHz）。空文本抛 ValueError；上限 500 字。

    失败即抛（调用方降级纯文本）——绝不返回"能播但听不懂"的怪音。
    ``voice`` = ``voices/`` 下的音色名；空取 ``tts_voice`` 配置，再空取第一个。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("text 为空")
    if len(text) > _TTS_MAX_CHARS:
        raise ValueError(f"文本过长（上限 {_TTS_MAX_CHARS} 字）")
    return _tts_cosyvoice2(text, voice)


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
    global _STT, _COSYVOICE2
    _STT = None
    _COSYVOICE2 = None
