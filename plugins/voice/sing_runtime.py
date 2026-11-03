# -*- coding: utf-8 -*-
"""SVC（唱歌）运行时：voice 配置 + 声库路径 + RVC 子进程单例。

**为什么这些状态不住在 ``plugin.py`` 里**：loader 是按文件路径把插件装载成
``_omni_plugin_voice`` 的（``omni_core/tools/loader.py`` ``_MODULE_PREFIX``），而
``sing_acquire`` / ``song_source`` 这类同目录兄弟模块是按包名
``from plugins.voice import plugin`` 取用的——那是**另一个模块对象**，同一份文件
exec 两遍，模块级单例就变成两份。真踩过：``_SING_WORKER`` 两个实例 → 两个 RVC 子进程
同时驻留（显存/内存双份、各自一把串行锁 = "同一时刻只有一路 GPU 唱歌"的不变式失效、
两个空闲回收线程各杀各的）。2026-11-03 日志实证：10:06:20 起 pid 44632（转换那份），
10:14:53 又起 pid 9104（切副歌/F0 那份），共存到 10:20:24 与 10:20:29 才被相隔 5s 的
两条"空闲回收"日志各自收掉。

所以规矩是：**跨模块共享的状态一律放本模块**——它只会被按包名 import 一次，永远不
经过 loader。依赖方向单向 ``plugin.py → sing_runtime``（反向 import 会把二次装载又
引回来）。voice 配置读取（``voice_cfg``）也住这里：跨模块共享，两份实现不如一份。
"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

from utils import get_logger

logger = get_logger("plugins.voice")


def voice_cfg() -> dict:
    """voice 插件自有配置（``~/.omniagent/plugins/voice.yaml``）；读不到 = 空 dict。

    全项目唯一实现：``plugin.py`` 的 ``_cfg`` 只是转发到这里（STT/TTS 也走这把）。
    配置属于跨模块共享状态，所以住这里而不是 plugin.py——原因见模块 docstring。
    """
    try:
        from omni_core.tools.loader import plugin_config

        return plugin_config("voice") or {}
    except Exception:
        return {}



# ── 唱歌（SVC）：RVC 独立 venv 子进程，形态照抄 plugin.py 的 _TtsWorker ──
# 唱歌与说话是两条独立管线（RVC 子进程 vs GS 子进程），互不复用引擎；
# GPU 与 GS 可共存（RVC 推理轻，见 doc/plans/sing-2026-11-02.md §5.2），故不抑制 TTS 预热。
_SING_BOOT_TIMEOUT = 120.0  # RVC 冷启动（加载声库 + index）
_SING_CALL_TIMEOUT = 300.0  # 50s 副歌转换；实测 RTF 合计 ≈0.05，余量给首次加载
_SING_MONITOR_SEC = 30.0
_SING_IDLE_SEC = 300.0  # 唱歌比说话低频，比 tts_idle_sec 更短（plan §5.2）
_SING_DIR = os.path.join(os.path.expanduser("~"), ".omniagent", "models", "sing")


def _sing_repo_dir() -> str:
    """RVC 仓库根（external/，gitignore 内；可用配置 sing_repo 覆盖）。"""
    d = str(voice_cfg().get("sing_repo") or "")
    if d and os.path.isdir(d):
        return d
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "external", "RVC-WebUI")


def _sing_python() -> str:
    """worker 用的解释器：RVC 独立 venv（torch 2.7.1+cu128，与主/GS venv 互斥）。"""
    p = str(voice_cfg().get("sing_python") or "")
    if p and os.path.isfile(p):
        return p
    guess = os.path.join(_sing_repo_dir(), ".venv", "Scripts", "python.exe")
    if os.path.isfile(guess):
        return guess
    return sys.executable


def _sing_model_path() -> str:
    return str(voice_cfg().get("sing_model") or os.path.join(_SING_DIR, "model", "current.pth"))


def _sing_index_path() -> str:
    return str(voice_cfg().get("sing_index") or os.path.join(_SING_DIR, "model", "current.index"))


def _sing_idle_sec() -> float:
    return max(0.0, float(voice_cfg().get("sing_idle_sec", _SING_IDLE_SEC)))


class _SingWorker:
    """RVC 声库子进程持有者：懒启动 + 串行转换 + 空闲回收，协议同 ``_TtsWorker``。

    文件进文件出（不回传 bytes）：转换产物直接落盘，发送方（QQ 通道）读文件走
    record 段，避免大 payload 过协议。配置缺失（声库未部署）= 永久不可用，调用方拿到
    明确错误后走降级（"这首我还不会唱"）。

    **单例只在模块级建立一次**（本文件末尾）：`_lock` 是"全进程只有一路 GPU 唱歌"的
    唯一执行者，多实例就等于把这个不变式拆了——见模块 docstring 的事故记录。
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._seq = 0
        self._monitor: Optional[threading.Thread] = None
        self._monitor_stop = threading.Event()

    def _start_locked(self) -> None:
        model, index = _sing_model_path(), _sing_index_path()
        missing = [p for p in (model, index) if not os.path.isfile(p)]
        if missing:
            raise RuntimeError("声库未部署: " + ", ".join(missing))
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sing_worker.py")
        env = dict(os.environ)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.Popen(
            [_sing_python(), worker, _sing_repo_dir(), model, index],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            # cwd 必须是 RVC 仓库根：它的 i18n 用 `./i18n/locale/<lang>.json` 相对路径
            # 读文案（i18n.py:8），cwd 不对 → import infer.vc.modules 直接炸。
            # tts_worker 用插件目录没问题，GS 不吃相对路径；RVC 吃，别照抄。
            cwd=_sing_repo_dir(), env=env,
        )
        self._proc = proc
        self._queue = queue.Queue()
        reader = threading.Thread(target=self._read_loop, args=(proc, self._queue),
                                  name="sing-worker-reader", daemon=True)
        reader.start()
        try:
            msg = self._queue.get(timeout=_SING_BOOT_TIMEOUT)
        except queue.Empty:
            self._kill_locked()
            raise RuntimeError(f"唱歌引擎启动超时（{_SING_BOOT_TIMEOUT:.0f}s）")
        if not isinstance(msg, dict):
            self._kill_locked()
            raise RuntimeError("唱歌引擎启动失败（子进程退出）")
        if not msg.get("ready"):
            self._kill_locked()
            raise RuntimeError(f"唱歌引擎加载失败: {msg.get('error')}")
        logger.info("唱歌引擎子进程就绪（pid=%s，tgt_sr=%s）", proc.pid, msg.get("tgt_sr"))

    @staticmethod
    def _read_loop(proc, q) -> None:
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    q.put(json.loads(raw.decode("utf-8")))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        finally:
            q.put(None)

    def _kill_locked(self) -> None:
        proc, self._proc = self._proc, None
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
        logger.info("唱歌引擎子进程已回收")

    def _ensure_locked(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            if self._proc is not None:
                logger.warning("唱歌引擎子进程已退出（code=%s），重启", self._proc.poll())
                self._kill_locked()
            self._start_locked()

    def _request(self, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        """通用请求（调用方须持锁）：写一行 JSON → 等同 id 响应。失败一律抛 RuntimeError。"""
        self._seq += 1
        rid = self._seq
        payload = {"id": rid, **payload}
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("唱歌引擎不可用")
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            proc.stdin.flush()
        except Exception as e:  # noqa: BLE001
            self._kill_locked()
            raise RuntimeError(f"唱歌引擎写入失败: {e}") from e
        try:
            resp = self._queue.get(timeout=timeout)
        except queue.Empty:
            self._kill_locked()
            raise RuntimeError(f"唱歌引擎响应超时（{timeout:.0f}s）")
        self._last_used = time.time()
        if not isinstance(resp, dict):
            self._kill_locked()
            raise RuntimeError("唱歌引擎进程异常退出")
        if not resp.get("ok"):
            raise RuntimeError(str(resp.get("error") or "唱歌引擎请求失败"))
        return resp

    def convert(self, input_wav: str, out_wav: str, key: int, seed: int,
                index_rate: float) -> Dict[str, Any]:
        """干声 wav → 角色清唱 wav。启动/转换/协议任一环节失败都抛 RuntimeError。"""
        with self._lock:
            self._ensure_locked()
            resp = self._request({"action": "sing", "input": input_wav, "out": out_wav,
                                  "key": int(key), "seed": int(seed),
                                  "index_rate": float(index_rate)},
                                 timeout=_SING_CALL_TIMEOUT)
            return {"out": resp["out"], "sr": resp.get("sr"),
                    "seconds": resp.get("seconds"), "dur": resp.get("dur")}

    def cut_chorus(self, input_wav: str, out_wav: str, seconds: float) -> Dict[str, Any]:
        """副歌启发式切取（在线曲库管线用，GPU 边界收在 worker）。"""
        with self._lock:
            self._ensure_locked()
            resp = self._request({"action": "cut_chorus", "input": input_wav,
                                  "out": out_wav, "seconds": float(seconds)},
                                 timeout=_SING_CALL_TIMEOUT)
            return {"out": resp["out"], "start_s": resp.get("start_s"), "dur": resp.get("dur")}

    def f0_key(self, input_wav: str, band_lo: float, band_hi: float) -> Dict[str, Any]:
        """F0 分位数 + 自动移调 key（在线曲库管线用）。"""
        with self._lock:
            self._ensure_locked()
            resp = self._request({"action": "f0_key", "input": input_wav,
                                  "band_lo": float(band_lo), "band_hi": float(band_hi)},
                                 timeout=_SING_CALL_TIMEOUT)
            return {k: resp[k] for k in resp if k not in ("id", "ok")}

    def _start_monitor(self) -> None:
        if self._monitor is not None or _sing_idle_sec() <= 0:
            return
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name="sing-worker-monitor", daemon=True)
        self._monitor.start()

    def _monitor_loop(self) -> None:
        """空闲超过阈值（``sing_idle_sec``，缺省 300s）→ 杀子进程回收显存内存。"""
        while not self._monitor_stop.wait(_SING_MONITOR_SEC):
            idle = _sing_idle_sec()
            if self._proc is None or idle <= 0:
                continue
            if time.time() - self._last_used < idle:
                continue
            if not self._lock.acquire(blocking=False):
                continue
            try:
                if self._proc is not None and time.time() - self._last_used >= idle:
                    logger.info("唱歌引擎空闲 %.0fs，回收子进程", time.time() - self._last_used)
                    self._kill_locked()
            finally:
                self._lock.release()

    def shutdown(self) -> None:
        self._monitor_stop.set()
        with self._lock:
            self._kill_locked()


#: 全进程唯一的 RVC 子进程持有者（本模块只按包名 import 一次 ⇒ 只有一份）
_SING_WORKER = _SingWorker()


def sing(input_wav: str, out_wav: str = "", key: int = 0, seed: int = 1234,
         index_rate: float = 0.66) -> Dict[str, Any]:
    """干声 wav → 角色音色清唱 wav（文件对文件）。

    Args:
        input_wav: 源干声（曲库副歌切段）路径。
        out_wav: 输出路径；空则取 ``<SING_DIR>/outputs/<sha1>.wav``。
        key: 移调半音数（±12 内，按声库音域适配，plan §3.3 门槛 1）。
        seed: 采样种子（换 seed = 换一版唱法细节）。
        index_rate: 检索混合率（音色相似度 vs 音质的两难旋钮，plan §3.1(2)）。

    失败即抛（调用方降级：曲库外的歌回复"这首我还不会唱"）。
    """
    if not input_wav or not os.path.isfile(input_wav):
        raise ValueError(f"input_wav 不存在: {input_wav!r}")
    if abs(int(key)) > 12:
        raise ValueError("key 超出 ±12 半音（移调不免费，plan §3.3）")
    if not out_wav:
        import hashlib

        h = hashlib.sha1(f"{input_wav}|{key}|{seed}|{index_rate}".encode("utf-8")).hexdigest()[:16]
        out_wav = os.path.join(_SING_DIR, "outputs", f"{h}.wav")
    meta = _SING_WORKER.convert(input_wav, out_wav, int(key), int(seed), float(index_rate))
    _SING_WORKER._start_monitor()  # 首次用后启动空闲回收监视
    return meta


def sing_cut_chorus(input_wav: str, out_wav: str, seconds: float = 50.0) -> Dict[str, Any]:
    """副歌启发式切取（在线曲库管线用；GPU 边界在 sing worker，见 ``sing_acquire``）。"""
    if not input_wav or not os.path.isfile(input_wav):
        raise ValueError(f"input_wav 不存在: {input_wav!r}")
    meta = _SING_WORKER.cut_chorus(input_wav, out_wav, float(seconds))
    _SING_WORKER._start_monitor()
    return meta


def sing_f0_key(input_wav: str, band_lo: float = 210.0, band_hi: float = 490.0) -> Dict[str, Any]:
    """F0 分位数 + 自动移调 key（在线曲库管线用；声库舒适带缺省 210–490Hz）。"""
    if not input_wav or not os.path.isfile(input_wav):
        raise ValueError(f"input_wav 不存在: {input_wav!r}")
    meta = _SING_WORKER.f0_key(input_wav, float(band_lo), float(band_hi))
    _SING_WORKER._start_monitor()
    return meta
