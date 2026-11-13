"""语音插件：本地 STT / TTS（SenseVoice + GPT-SoVITS 微调，懒加载）。

形态与 ocr 同款——**独立可插拔、懒加载、无状态**：内核与前端只经两个
HTTP 端点触达（``/api/audio/transcribe`` / ``/api/audio/tts``，由
``backend/api/routers/audio_api.py`` 薄壳转发），本插件不注册任何 agent
工具，内核（omni_core）对它零 import——语音对大脑不可见（单链路红线）。

**职责边界（M2.5a 服务化）**：本插件是**调用端**——``transcribe``/``tts`` 只把
请求发到 OpenAI 音频兼容端点（本机常驻服务或在线厂商），端点地址/密钥在插件配置
``~/.omniagent/plugins/voice.yaml``。引擎本身（模型路径/音色/空闲回收/端口）住在
``engine_service.py`` 常驻进程 + 服务配置 ``~/.omniagent/voice_service.yaml``；
``transcribe_local``/``tts_local`` 是引擎实现，只有服务进程会调（否则会把请求
按端点配置再转发回自己）。


能力（capability 注册制，插件未启用 = 能力不存在 = 端点 404）：
- ``voice_transcribe(wav_bytes) -> str``：16kHz 单声道 WAV → 中/英文文本；
- ``voice_tts(text, voice="") -> bytes``：文本 → 16 位 PCM WAV（32kHz），
  ``voice`` = ``voices/`` 下的音色名（一段参考音频 + 同名 .txt 韵律文本 = 一个音色）。

TTS = GPT-SoVITS v2 微调引擎（CosyVoice2 zero-shot 已退役，原因与验收数据见
``doc/plans/voice-tone-2026-10-31.md`` §9）。推理代码不在仓库，直接用
``external/GPT-SoVITS`` 上游 clone 的 TTS_infer_pack，本插件只做
``gptsovits_rt/`` 薄适配。worker 子进程必须用 GS 独立 venv 的 python 拉起
（配置 ``tts_python``，默认 ``external/GPT-SoVITS/.venv``）。

懒加载：sherpa-onnx / torch / 模型权重只在**首次调用**时构造（import 本包
零开销）；``enabled=false``（默认）时进程内无 ONNXRuntime/torch。

模型（放 ``~/.omniagent/models/``，路径可用 voice_service.yaml 覆盖）：
- SenseVoice：``models/sense-voice/{model.int8.onnx, tokens.txt}``
  （sherpa-onnx releases 的 sense-voice int8 包，中英日韩粤）
- GPT-SoVITS：``models/gptsovits/``——``pretrained_models/``（v2 底模：
  chinese-hubert-base / chinese-roberta-wwm-ext-large / gsv-v2final-pretrained）
  + ``weights/``（微调权重，默认 doro_e12_s288.pth + doro-e30.ckpt）
  + ``voices/``（参考音色 wav + 同名 txt）
  上游 clone 在 ``external/GPT-SoVITS``（text/G2PWModel 文本前端随仓库）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import queue
import subprocess
import sys
import urllib.request
import threading
import time
from typing import Any, Dict, List, Optional

from utils import get_logger

# 唱歌（SVC）的运行时状态（RVC 子进程单例 + voice 配置）住在 sing_runtime，不在这里。
# 原因：loader 按文件路径把本文件装载成 `_omni_plugin_voice`，而 sing_acquire /
# song_source 按包名 import 的是另一个模块对象——同一份文件 exec 两遍，写在这里的
# 单例会变成两份（两个 RVC 子进程、两把串行锁、两个回收线程，2026-11-03 实测踩过）。
# 依赖方向必须单向：plugin → sing_runtime（反向会把二次装载又引回来）。
from plugins.voice.sing_runtime import _SING_WORKER, sing, voice_cfg

#: 本文件 STT/TTS 段沿用的旧名字：**同一个函数**，不是第二份实现
_cfg = voice_cfg

logger = get_logger("plugins.voice")

#: STT 留在本进程（SenseVoice ONNX，200-300MB，转写要即时）；TTS 引擎在子进程
#: （GPT-SoVITS 权重 2GB 档 + 运行时数 GB，见 ``tts_worker.py`` 的说明）
_STT = None

_TTS_MAX_CHARS = 500
_STT_MAX_SECONDS = 60

#: TTS 子进程空闲多久后回收（秒）。杀进程是唯一能把 torch 的 5GB 内存还给
#: 系统的手段；0 = 不回收（常驻）。纯文字对话时子进程不存在，开销为零。
_TTS_IDLE_SEC = 600.0
#: 冷启动上限（加载 ~2GB 微调权重 + 底模，慢盘可能 60s+）
_TTS_BOOT_TIMEOUT = 300.0
#: 单次合成上限（长文本分段合成，CPU 上可能数十秒）
_TTS_CALL_TIMEOUT = 300.0
#: 空闲监视线程间隔
_TTS_MONITOR_SEC = 30.0


def _default_model(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), ".omniagent", "models", *parts)


def _get_stt():
    """STT 识别器单例（首次调用才构造；缺模型给可执行提示）。

    模型选型走 voice_service.yaml 的 ``stt_model``（2026-11-06）：``sense-voice``
    （多语言小模型，原缺省）/ ``paraformer``（阿里 FunASR 中文专用 int8，生活化
    口语与标点更强——中文场景主推，接口不变只换 recognizer 构造）。
    目录：``stt_model_dir`` 显式配置优先，否则 ``~/.omniagent/models/<stt_model>/``
    （paraformer 包解包后可能带一层版本目录，glob 兜底找 model.int8.onnx）。
    """
    global _STT
    if _STT is None:
        import sherpa_onnx

        kind = str(_cfg().get("stt_model") or "sense-voice").strip().lower()
        model_dir = str(_cfg().get("stt_model_dir") or "") or _default_model(kind)
        model = os.path.join(model_dir, "model.int8.onnx")
        if kind == "paraformer" and not os.path.isfile(model):
            import glob as _glob

            hits = _glob.glob(os.path.join(model_dir, "*", "model.int8.onnx"))
            if hits:
                model = hits[0]
        tokens = os.path.join(os.path.dirname(model), "tokens.txt")
        missing = [p for p in (model, tokens) if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(
                f"{kind} STT 模型缺失: " + ", ".join(missing)
                + "（下载见 doc/plans/implemented/voice-2026-10-31.md §4）")
        if kind == "paraformer":
            _STT = sherpa_onnx.OfflineRecognizer.from_paraformer(
                model=model, tokens=tokens, num_threads=2)
        else:
            _STT = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model, tokens=tokens, use_itn=True, num_threads=2)
    return _STT


# 转写串行锁（2026-11-04）：懒加载窗口期/流构造并非并发安全——手机免提的陈旧请求
# 与 Web 按住说话同时到达会把首次加载打挂（实测 503 三连）。单进程内排队即可，
# 转写本身亚秒级，不构成吞吐瓶颈。
_STT_LOCK = threading.Lock()


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


# ── 调用端点（M2.5a 服务化：插件只做接口调用）─────────────────────────────
# 配置（~/.omniagent/plugins/voice.yaml，纯客户端配置）：
#   base_url: http://127.0.0.1:18742/v1     # 本机常驻服务，或任意在线厂商
#   api_key / stt_model / tts_model / voice
# 引擎配置（模型路径/音色/空闲回收/端口）在 ~/.omniagent/voice_service.yaml，
# 由 engine_service 与本文件的本机引擎段读取（见 service_config.py）。


def _client_cfg() -> Dict[str, Any]:
    """调用端配置（插件目录里的那份）。"""
    try:
        from omni_core.tools.loader import plugin_config

        return plugin_config("voice") or {}
    except Exception:  # noqa: BLE001 —— 配置读不到 = 未配置端点
        return {}


def _openai_audio_cfg() -> tuple:
    cfg = _client_cfg()
    base = str(cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "语音端点未配置（plugins/voice.yaml 的 base_url）——"
            "本机服务填 http://127.0.0.1:<service_port>/v1，或填在线厂商地址")
    return cfg, base


def _service_root(base: str) -> str:
    """端点 → 服务根（``.../v1`` 去掉）——/healthz 挂在根上。"""
    return base[:-3] if base.endswith("/v1") else base


def _openai_request(req, base: str, tail: str) -> bytes:
    """发 HTTP 并把「端点没起来」翻译成可执行的提示（不是裸 URLError）。"""
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"语音端点不可用（{base}{tail}）: {e}。"
            "本地服务请确认语音引擎已由后端服务管理器拉起；"
            "手动：python -m model_hub.servers.voice_engine）") from e


def _openai_transcribe(wav_bytes: bytes) -> str:
    """OpenAI 音频兼容 STT：multipart POST /audio/transcriptions → {"text": ...}。"""
    import uuid
    import urllib.request
    cfg, base = _openai_audio_cfg()
    boundary = "----hachifivoice" + uuid.uuid4().hex
    model = str(cfg.get("stt_model") or "whisper-1")
    crlf = "\r\n"
    parts = [
        (f'--{boundary}{crlf}Content-Disposition: form-data; name="model"'
         f'{crlf}{crlf}{model}{crlf}').encode(),
        (f'--{boundary}{crlf}Content-Disposition: form-data; name="file"; '
         f'filename="audio.wav"{crlf}Content-Type: audio/wav{crlf}{crlf}').encode()
        + wav_bytes + crlf.encode(),
        f"--{boundary}--{crlf}".encode(),
    ]
    req = urllib.request.Request(
        base + "/audio/transcriptions", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Authorization": "Bearer " + str(cfg.get("api_key") or "")})
    data = json.loads(_openai_request(req, base, "/audio/transcriptions").decode("utf-8"))
    return str(data.get("text") or "").strip()


def _openai_tts(text: str, voice: str = "") -> bytes:
    """OpenAI 音频兼容 TTS：POST /audio/speech {model,input,voice} → 音频 bytes。

    ``voice`` 显式给就优先，否则用插件配置的 voice（本机服务会把它当音色名，
    不认识时自动回退默认音色）。
    """
    import urllib.request
    cfg, base = _openai_audio_cfg()
    body = json.dumps({
        "model": str(cfg.get("tts_model") or "tts-1"),
        "input": text,
        "voice": str(voice or cfg.get("voice") or "alloy"),
        "response_format": "wav",
    }).encode()
    req = urllib.request.Request(
        base + "/audio/speech", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + str(cfg.get("api_key") or "")})
    return _openai_request(req, base, "/audio/speech")


def _stt_idle_sec() -> float:
    """进程内 STT 空闲卸载阈值（配置 ``stt_idle_sec``；0 = 常驻不卸）。"""
    try:
        return max(0.0, float(_cfg().get("stt_idle_sec", 900)))
    except (TypeError, ValueError):
        return 900.0


_STT_LAST_USED = 0.0
_STT_MONITOR: Optional[threading.Thread] = None
_STT_MONITOR_LOCK = threading.Lock()


def _start_stt_monitor() -> None:
    global _STT_MONITOR
    if _stt_idle_sec() <= 0:
        return
    with _STT_MONITOR_LOCK:
        if _STT_MONITOR is not None and _STT_MONITOR.is_alive():
            return
        _STT_MONITOR = threading.Thread(target=_stt_monitor_loop,
                                        name="stt-idle-monitor", daemon=True)
        _STT_MONITOR.start()


def _stt_monitor_loop() -> None:
    """空闲超过 ``stt_idle_sec`` 丢掉识别器（ONNX 会话几百 MB，不用就别留着）。

    与 TTS 子进程回收同构：只是回收力度不同——STT 没有子进程可杀，只能丢引用
    （onnxruntime arena 未必立刻还给系统，但下一次懒加载会重建）。
    """
    global _STT
    while True:
        time.sleep(_TTS_MONITOR_SEC)
        idle = _stt_idle_sec()
        if idle <= 0 or _STT is None:
            continue
        if time.time() - _STT_LAST_USED < idle:
            continue
        if not _STT_LOCK.acquire(blocking=False):  # 正在转写，跳过这一轮
            continue
        try:
            if _STT is not None and time.time() - _STT_LAST_USED >= idle:
                _STT = None
                logger.info("STT 空闲 %.0fs，卸载识别器", time.time() - _STT_LAST_USED)
        finally:
            _STT_LOCK.release()


def transcribe_local(wav_bytes: bytes) -> str:
    """本机引擎转写（进程内）。**只有 engine_service 调它**——插件侧只发 HTTP，
    服务端调这个入口才不会把请求按端点配置转发回自己。"""
    global _STT_LAST_USED
    samples = _decode_wav_16k_mono(wav_bytes)
    if samples.size == 0:
        return ""
    recognizer = _get_stt()
    with _STT_LOCK:
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        recognizer.decode_stream(stream)
        text = str(getattr(stream.result, "text", "") or "").strip()
    _STT_LAST_USED = time.time()
    _start_stt_monitor()
    if text:
        # 收到语音 = 模型大概率也会用语音回（msg_send_voice）。趁它在想回复内容的
        # 十几秒里把 GS 引擎子进程拉起来，用户感知不到冷启动。
        _WORKER.prewarm()
    return text


# ── 语义收口（2026-11-06）：Smart Turn v3（pipecat-ai 开源，MIT）────────────────
# 能量 VAD 的静音 ≠ 说完了：「我再想想……」这类思考性停顿会被挂起窗收口成一句，
# AI 立刻接话就是抢话。这里用音频原生的 end-of-turn 判断补一道：喂尾部 8 秒
# 16k PCM，返回「这轮说完没有」。8MB int8 ONNX、CPU 推理 ~12ms，对链路无感。
# 预处理逐行对齐官方 inference.py（WhisperFeatureExtractor(chunk_length=8)）。

_TURN = None
_TURN_LOCK = threading.Lock()
_TURN_FE = None


def _get_turn():
    """Smart Turn ONNX 会话单例（模型缺失抛 RuntimeError，服务端 500 → 壳侧 fail-open）。"""
    global _TURN, _TURN_FE
    if _TURN is None:
        import onnxruntime as ort

        path = os.path.join(_default_model("smart-turn"), "smart-turn-v3.2-cpu.onnx")
        if not os.path.isfile(path):
            raise RuntimeError("Smart Turn 模型缺失: " + path)
        so = ort.SessionOptions()
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _TURN = ort.InferenceSession(path, sess_options=so)
        from transformers import WhisperFeatureExtractor

        _TURN_FE = WhisperFeatureExtractor(chunk_length=8)
    return _TURN


def turn_check_local(wav_bytes: bytes) -> Dict[str, Any]:
    """一段话音频 → {completed, probability}。completed=false = 用户话没说完
    （调用方该继续听，不是接话）。空音频按完结返回（壳侧本来就会丢空文本）。"""
    import numpy as np

    samples = _decode_wav_16k_mono(wav_bytes)
    if samples.size == 0:
        return {"completed": True, "probability": 1.0}
    session = _get_turn()
    with _TURN_LOCK:
        keep = 8 * 16000
        seg = (samples[-keep:].astype(np.float32) if samples.size >= keep else
               np.pad(samples.astype(np.float32), (keep - samples.size, 0)))
        inputs = _TURN_FE(seg, sampling_rate=16000, return_tensors="np",
                          padding="max_length", max_length=keep, truncation=True,
                          do_normalize=True)
        feats = inputs.input_features.squeeze(0).astype(np.float32)[None, ...]
        prob = float(session.run(None, {"input_features": feats})[0][0].item())
    _STT_LAST_USED = time.time()
    return {"completed": prob > 0.5, "probability": round(prob, 4)}


def transcribe(wav_bytes: bytes) -> str:
    """WAV（16k 单声道为宜）→ 文本。空音频返回空串；端点不可用抛 RuntimeError。

    插件侧唯一实现：把请求发给配置的 OpenAI 音频兼容端点（本机服务/在线厂商）。
    """
    if not wav_bytes:
        return ""
    return _openai_transcribe(wav_bytes)


def _gptsovits_dir() -> str:
    """GPT-SoVITS 模型目录（weights/ + voices/ + pretrained_models/）。"""
    return str((_cfg().get("gptsovits_dir") or _default_model("gptsovits")))


def _gs_repo_dir() -> str:
    """上游 GPT-SoVITS clone（external/，gitignore 内；可用配置覆盖）。"""
    d = str(_cfg().get("gptsovits_repo") or "")
    if d and os.path.isdir(d):
        return d
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "external", "GPT-SoVITS")


def _tts_python() -> str:
    """worker 用的解释器：GS 独立 venv（依赖与主 .venv 隔离），缺失才退回当前解释器。"""
    p = str(_cfg().get("tts_python") or "")
    if p and os.path.isfile(p):
        return p
    guess = os.path.join(_gs_repo_dir(), ".venv", "Scripts", "python.exe")
    if os.path.isfile(guess):
        return guess
    guess = os.path.join(_gs_repo_dir(), ".venv", "bin", "python")
    if os.path.isfile(guess):
        return guess
    return sys.executable


def _voices_dir() -> str:
    return str(_cfg().get("voices_dir") or os.path.join(_gptsovits_dir(), "voices"))


def _tts_weights() -> tuple:
    """（vits, t2s）微调权重路径；配置可覆盖，默认 s2-e12 + s1-e30 验收组合。"""
    model_dir = _gptsovits_dir()
    vits = str(_cfg().get("tts_vits") or os.path.join(model_dir, "weights", "doro_e12_s288.pth"))
    t2s = str(_cfg().get("tts_t2s") or os.path.join(model_dir, "weights", "doro-e30.ckpt"))
    if vits and not os.path.isabs(vits):
        vits = os.path.join(model_dir, vits)
    if t2s and not os.path.isabs(t2s):
        t2s = os.path.join(model_dir, t2s)
    return os.path.normpath(vits), os.path.normpath(t2s)


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
        vits, t2s = _tts_weights()
        proc = subprocess.Popen(
            [_tts_python(), worker, _gs_repo_dir(), _gptsovits_dir(), _voices_dir(),
             vits, t2s],
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

    def release(self) -> bool:
        """杀掉引擎子进程归还内存/显存，**保留本对象与监视线程**（空闲回收用）。

        与 ``shutdown`` 的区别：再调用 ``synthesize`` 会自动重新懒启动，
        所以常驻服务可以在空闲时把权重全部还掉而不退出进程。
        """
        with self._lock:
            had = self._proc is not None
            self._kill_locked()
            return had

    def snapshot(self) -> Dict[str, Any]:
        """运行态快照（不触发任何加载，供状态接口用）。"""
        proc = self._proc
        alive = proc is not None and proc.poll() is None
        return {
            "running": alive,
            "pid": proc.pid if (alive and proc is not None) else None,
            "voices": list(self._voices) if alive else [],
            "idle_sec": (round(time.time() - self._last_used, 1)
                         if alive and self._last_used else None),
        }

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

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


def tts_local(text: str, voice: str = "") -> bytes:
    """本机引擎合成（进程内）。只有 engine_service 调它（理由同 transcribe_local）。"""
    return _tts_dispatch(text, voice)


def tts(text: str, voice: str = "") -> bytes:
    """文本 → 16 位 PCM WAV bytes（32kHz）。空文本抛 ValueError；上限 500 字。

    失败即抛（调用方降级纯文本）——绝不返回"能播但听不懂"的怪音。
    插件侧唯一实现：发给配置的 OpenAI 音频兼容端点（本机服务/在线厂商）。
    ``voice`` 透传给端点（本机服务认 voices/ 下的音色名，不认识就用默认音色）。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("text 为空")
    if len(text) > _TTS_MAX_CHARS:
        raise ValueError(f"文本过长（上限 {_TTS_MAX_CHARS} 字）")
    return _openai_tts(text, voice)


# ── 空闲回收 / 状态快照（常驻服务与设置页共用；不加载任何模型）──────────────

def release_engines() -> Dict[str, Any]:
    """释放重型引擎但保留进程与能力（空闲回收入口；下次调用自动懒加载）。"""
    global _STT
    with _STT_LOCK:
        had_stt = _STT is not None
        _STT = None
    had_tts = _WORKER.release()
    if had_stt or had_tts:
        logger.info("语音引擎已释放（STT=%s, TTS=%s）", had_stt, had_tts)
    return {"stt_released": had_stt, "tts_released": had_tts}


def engines_loaded() -> bool:
    """是否还有引擎驻留（服务看门狗据此决定要不要释放）。"""
    with _STT_LOCK:
        loaded = _STT is not None
    return loaded or _WORKER.is_running()


def _voice_names() -> List[str]:
    """``voices/`` 下的音色名（*.wav 文件名，不含扩展名；目录缺失 = 空）。"""
    try:
        return sorted(os.path.splitext(f)[0] for f in os.listdir(_voices_dir())
                      if f.lower().endswith(".wav"))
    except OSError:
        return []


def status() -> Dict[str, Any]:
    """**引擎侧**快照（只读、零加载）——只有 engine_service 进程里才有意义。

    它在服务进程内读 ``voice_service.yaml`` 与本进程的 STT/TTS 状态；后端进程
    用它没有意义（模型不在那个进程里），后端只调下面的 ``client_status``。
    """
    cfg = _cfg()
    stt_dir = str(cfg.get("stt_model_dir") or _default_model("sense-voice"))
    stt_files = [os.path.join(stt_dir, n) for n in ("model.int8.onnx", "tokens.txt")]
    vits, t2s = _tts_weights()
    tts = _WORKER.snapshot()
    if not tts["voices"]:
        tts["voices"] = _voice_names()  # 引擎未起时退化读目录，仍然是真实可用音色
    return {
        "stt": {
            "loaded": _STT is not None,
            "model_dir": stt_dir,
            "ready": all(os.path.isfile(p) for p in stt_files),
            "missing": [p for p in stt_files if not os.path.isfile(p)],
            "idle_sec": _stt_idle_sec(),
        },
        "tts": {
            **tts,
            "voice_default": str(cfg.get("tts_voice") or ""),
            "eq": bool(cfg.get("tts_eq", False)),
            "idle_sec": _idle_sec(),
            "weights": [vits, t2s],
            "weights_ready": all(os.path.isfile(p) for p in (vits, t2s)),
            "voices_dir": _voices_dir(),
            "repo": _gs_repo_dir(),
        },
        "service": {
            "port": int(cfg.get("service_port") or 18742),
            "idle_sec": float(cfg.get("service_idle_sec") or 600),
        },
    }


def client_status() -> Dict[str, Any]:
    """**调用侧**快照：端点配置 + 服务探活（设置页/外部客户端看的就是这份）。

    探测用服务根的 ``/healthz``（``.../v1`` 去掉）。在线厂商没有这个端点时
    ``reachable=False`` 只是「探不到」，不代表端点不可用——UI 需区分措辞。
    """
    cfg = _client_cfg()
    base = str(cfg.get("base_url") or "").rstrip("/")
    out: Dict[str, Any] = {
        "endpoint": {
            "base_url": base,
            "api_key_set": bool(str(cfg.get("api_key") or "")),
            "stt_model": str(cfg.get("stt_model") or ""),
            "tts_model": str(cfg.get("tts_model") or ""),
            "voice": str(cfg.get("voice") or ""),
        },
        "service": {"reachable": False, "url": "", "detail": None, "error": ""},
    }
    if not base:
        out["service"]["error"] = "端点未配置"
        return out
    url = _service_root(base) + "/healthz"
    out["service"]["url"] = url
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            out["service"]["detail"] = json.loads(resp.read().decode("utf-8"))
        out["service"]["reachable"] = True
    except Exception as e:  # noqa: BLE001 —— 探活失败只是"没起来/不是本机服务"
        out["service"]["error"] = f"{type(e).__name__}: {e}"
    return out


# ── 唱歌（SVC）：实现整体在 sing_runtime（worker 单例 / sing / sing_cut_chorus /
# sing_f0_key / 声库与解释器路径）。本文件只保留两处装配：startup 把 sing 注册成
# voice_sing 能力、shutdown 回收那**唯一**的子进程。搬走的原因见文件头 import 注释。


def startup(ctx) -> None:
    """注册 capability（audio_api 薄壳按名转发；未启用 = 端点 404）。"""
    from omni_core.tools.capability import register_capability

    register_capability("voice_transcribe", transcribe)
    register_capability("voice_tts", tts)
    register_capability("voice_sing", sing)
    register_capability("voice_status", client_status)
    logger.info("语音插件已启用（STT/TTS 将在首次调用时懒加载；SVC 声库缺失时报明确错误）")


def shutdown() -> None:
    """注销能力并回收 TTS/SVC 子进程（STT 模型随进程退出释放）。"""
    from omni_core.tools.capability import unregister_capability

    unregister_capability("voice_transcribe")
    unregister_capability("voice_tts")
    unregister_capability("voice_sing")
    unregister_capability("voice_status")
    global _STT
    _STT = None
    _WORKER.shutdown()
    _SING_WORKER.shutdown()
