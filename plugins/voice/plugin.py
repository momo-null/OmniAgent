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

import base64
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import List, Optional

from utils import get_logger

logger = get_logger("plugins.voice")

#: STT 留在本进程（SenseVoice ONNX，200-300MB，转写要即时）；TTS 引擎在子进程
#: （CosyVoice2 常驻 ~5GB 内存 / 2.7GB 显存，见 ``tts_worker.py`` 的说明）
_STT = None

_TTS_MAX_CHARS = 500
_STT_MAX_SECONDS = 60

#: TTS 子进程空闲多久后回收（秒）。杀进程是唯一能把 torch 的 5GB 内存还给
#: 系统的手段；0 = 不回收（常驻）。纯文字对话时子进程不存在，开销为零。
_TTS_IDLE_SEC = 600.0
#: 冷启动上限（加载 ~4GB 权重，慢盘可能 60s+）
_TTS_BOOT_TIMEOUT = 300.0
#: 单次合成上限（长文本分段合成，CPU 上可能数十秒）
_TTS_CALL_TIMEOUT = 300.0
#: 空闲监视线程间隔
_TTS_MONITOR_SEC = 30.0


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
    text = str(getattr(stream.result, "text", "") or "").strip()
    if text:
        # 收到语音 = 模型大概率也会用语音回（msg_send_voice）。趁它在想回复内容的
        # 十几秒里把 4GB 引擎子进程拉起来，用户感知不到冷启动。
        _WORKER.prewarm()
    return text


def _cosyvoice2_dir() -> str:
    return str((_cfg().get("cosyvoice2_dir") or _default_model("cosyvoice2")))


def _voices_dir() -> str:
    return str(_cfg().get("voices_dir") or os.path.join(_cosyvoice2_dir(), "voices"))


def _idle_sec() -> float:
    """TTS 子进程空闲回收阈值（配置 ``tts_idle_sec``；0 = 常驻不回收）。"""
    try:
        return max(0.0, float(_cfg().get("tts_idle_sec", _TTS_IDLE_SEC)))
    except (TypeError, ValueError):
        return _TTS_IDLE_SEC


class _TtsWorker:
    """TTS 引擎子进程持有者：懒启动 + 串行合成 + 空闲自杀 + 崩溃自动重启。

    串行是刻意的：单张卡、单模型，并发只会互相抢显存。调用方（QQ 通道 / Web）
    本来就是低频请求，排队没有任何实际损失。
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._seq = 0
        self._voices: List[str] = []
        self._monitor: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()

    # ── 生命周期（内部方法都在 self._lock 内调用） ──
    def _start_locked(self) -> None:
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_worker.py")
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.Popen(
            [sys.executable, worker, _cosyvoice2_dir(), _voices_dir()],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(worker), env=env,
        )
        self._proc = proc
        self._queue = queue.Queue()
        reader = threading.Thread(target=self._read_loop, args=(proc, self._queue),
                                  name="tts-worker-reader", daemon=True)
        reader.start()
        try:
            msg = self._queue.get(timeout=_TTS_BOOT_TIMEOUT)
        except queue.Empty:
            self._kill_locked()
            raise RuntimeError(f"TTS 引擎启动超时（{_TTS_BOOT_TIMEOUT:.0f}s）")
        if not isinstance(msg, dict):
            self._kill_locked()
            raise RuntimeError("TTS 引擎启动失败（子进程退出）")
        if not msg.get("ready"):
            self._kill_locked()
            raise RuntimeError(f"TTS 引擎加载失败: {msg.get('error')}")
        self._voices = [str(v) for v in (msg.get("voices") or [])]
        logger.info("TTS 引擎子进程就绪（pid=%s，音色 %d 个）", proc.pid, len(self._voices))

    @staticmethod
    def _read_loop(proc, q) -> None:
        """把子进程 stdout 的每一行 JSON 推进队列；EOF 推 None 表示进程结束。"""
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    q.put(json.loads(raw.decode("utf-8")))
                except Exception:  # noqa: BLE001 —— 协议噪声不该 kill 读线程
                    continue
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)

    def _kill_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._voices = []
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:  # noqa: BLE001
            pass
        logger.info("TTS 引擎子进程已回收")

    def _ensure_locked(self) -> List[str]:
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                logger.warning("TTS 引擎子进程已退出（code=%s），重启", self._proc.poll())
                self._kill_locked()
            self._start_locked()
        return self._voices

    # ── 对外 ──
    def synthesize(self, text: str, voice: str, eq: bool, default_voice: str = "") -> bytes:
        """文本 → WAV bytes。启动/合成/协议任一环节失败都抛 RuntimeError。

        ``voice`` 为空时子进程按 ``default_voice``（配置 tts_voice）挑音色，
        名字失效则退回第一个可用音色。
        """
        with self._lock:
            self._ensure_locked()
            self._seq += 1
            rid = self._seq
            payload = json.dumps({"id": rid, "text": text, "voice": voice,
                                  "default": default_voice, "eq": bool(eq)},
                                 ensure_ascii=False).encode("utf-8") + b"\n"
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise RuntimeError("TTS 引擎不可用")
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except Exception as e:  # noqa: BLE001
                self._kill_locked()
                raise RuntimeError(f"TTS 引擎写入失败: {e}") from e
            try:
                resp = self._queue.get(timeout=_TTS_CALL_TIMEOUT)
            except queue.Empty:
                self._kill_locked()
                raise RuntimeError(f"TTS 合成超时（{_TTS_CALL_TIMEOUT:.0f}s）")
            self._last_used = time.time()
            if not isinstance(resp, dict):
                self._kill_locked()
                raise RuntimeError("TTS 引擎进程异常退出")
            if not resp.get("ok"):
                raise RuntimeError(str(resp.get("error") or "TTS 合成失败"))
            return base64.b64decode(resp["wav"])

    def prewarm(self) -> None:
        """后台预热（不阻塞调用方）：收到语音消息时提前把引擎拉起来。"""
        def _boot() -> None:
            try:
                with self._lock:
                    self._ensure_locked()
                    self._last_used = time.time()
            except Exception as e:  # noqa: BLE001 —— 预热失败不影响主链路
                logger.warning("TTS 预热失败（不影响文字回复）: %s: %s", type(e).__name__, e)

        threading.Thread(target=_boot, name="tts-prewarm", daemon=True).start()
        self._start_monitor()

    def _start_monitor(self) -> None:
        if self._monitor is not None or _idle_sec() <= 0:
            return
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name="tts-worker-monitor", daemon=True)
        self._monitor.start()

    def _monitor_loop(self) -> None:
        """空闲超过阈值（``tts_idle_sec``，缺省 600s）→ 杀子进程回收内存。"""
        while not self._monitor_stop.wait(_TTS_MONITOR_SEC):
            idle = _idle_sec()
            if self._proc is None or idle <= 0:
                continue
            if time.time() - self._last_used < idle:
                continue
            if not self._lock.acquire(blocking=False):  # 正在合成，跳过这一轮
                continue
            try:
                if self._proc is not None and time.time() - self._last_used >= idle:
                    logger.info("TTS 引擎空闲 %.0fs，回收子进程", time.time() - self._last_used)
                    self._kill_locked()
            finally:
                self._lock.release()

    def shutdown(self) -> None:
        self._monitor_stop.set()
        with self._lock:
            self._kill_locked()


_WORKER = _TtsWorker()


#: TTS 输出频响补偿在 ``eq.py``（与引擎无关的通用后处理），合成在子进程完成。
def _tts_dispatch(text: str, voice: str) -> bytes:
    """文本 → WAV bytes（实际合成在 TTS 子进程里，见 ``tts_worker.py``）。"""
    wav = _WORKER.synthesize(text, voice, bool(_cfg().get("tts_eq", False)),
                             str(_cfg().get("tts_voice") or ""))
    _WORKER._start_monitor()  # 首次用后启动空闲回收监视
    return wav


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
    return _tts_dispatch(text, voice)


def startup(ctx) -> None:
    """注册 capability（audio_api 薄壳按名转发；未启用 = 端点 404）。"""
    from omni_core.tools.capability import register_capability

    register_capability("voice_transcribe", transcribe)
    register_capability("voice_tts", tts)
    logger.info("语音插件已启用（STT/TTS 将在首次调用时懒加载）")


def shutdown() -> None:
    """注销能力并回收 TTS 子进程（STT 模型随进程退出释放）。"""
    from omni_core.tools.capability import unregister_capability

    unregister_capability("voice_transcribe")
    unregister_capability("voice_tts")
    global _STT
    _STT = None
    _WORKER.shutdown()
