"""TTS 引擎子进程（plugins/voice 的 out-of-process 合成器）。

为什么单独一个进程：CosyVoice2 加载后常驻约 5GB 内存 / 2.7GB 显存，而**同进程
卸载回收不了内存**（torch 的 CPU 分配器 + glibc 碎片 + ONNX arena 不会还给系统，
实测 WS 从 5148MB 只降到 4591MB）。杀进程是唯一能把内存 100% 归还的手段，
所以引擎放子进程、空闲即被父进程杀掉 —— 纯文字对话时语音开销为零。

协议（stdin/stdout，一行一个 JSON；stderr 留给日志）：
- 启动完成：``{"ready": true, "voices": [...]}``
- 请求：``{"id": 1, "text": "...", "voice": "name", "eq": true}``
- 成功：``{"id": 1, "ok": true, "wav": "<base64 wav>"}``
- 失败：``{"id": 1, "ok": false, "error": "..."}``

stdin 关闭（父进程退出或主动回收）= 子进程退出，不需要额外信号。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys


def _emit(obj) -> None:
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: tts_worker.py <model_dir> <voices_dir>", file=sys.stderr)
        return 2
    model_dir, voices_dir = sys.argv[1], sys.argv[2]

    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    from eq import equalize
    import soundfile as sf
    from cosyvoice2_rt import CosyVoice2Engine

    try:
        engine = CosyVoice2Engine(model_dir, voices_dir)  # 4GB 权重在这里进内存
    except Exception as e:  # noqa: BLE001 —— 构造失败要让父进程看到原因
        _emit({"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _emit({"ready": True, "voices": engine.list_voices()})

    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            _emit({"id": None, "ok": False, "error": f"bad request: {e}"})
            continue
        rid = req.get("id")
        try:
            text = str(req.get("text") or "").strip()
            if not text:
                raise ValueError("text 为空")
            name = str(req.get("voice") or "")
            if not name:
                # 缺省音色名可能是陈旧配置（改过引擎/删过音色），只警告不挡合成
                default = str(req.get("default") or "")
                if default and default in engine.list_voices():
                    name = default
                elif default:
                    print(f"tts_voice={default} 不是 voices/ 下的音色名，改用第一个",
                          file=sys.stderr)
            samples, sr = engine.synthesize(text, name)
            if req.get("eq"):
                samples = equalize(samples, sr)
            buf = io.BytesIO()
            sf.write(buf, samples, sr, format="WAV", subtype="PCM_16")
            _emit({"id": rid, "ok": True,
                   "wav": base64.b64encode(buf.getvalue()).decode("ascii")})
        except Exception as e:  # noqa: BLE001 —— 单次失败不拖垮进程
            _emit({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
