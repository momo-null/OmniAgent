# -*- coding: utf-8 -*-
"""唱歌转换子进程（RVC / 独立 venv）。

协议（stdin/stdout，一行一个 JSON；stderr 留给日志），与 tts_worker.py 同形态：
- 启动即发 {"ready": true} 或 {"ready": false, "error": ...}
- 请求 {"id": ..., "action": "sing", "input": <wav路径>, "key": <半音>, "seed": <int>,
        "index_rate": <0..1>, "protect": <0..1>, "out": <wav路径>}
- 响应 {"id": ..., "ok": true, "out": ..., "sr": ..., "seconds": ...} / {"ok": false, "error": ...}
- stdin 关闭 = 退出（父进程 idle-kill 或崩溃连带，无需额外信号）

用法: <RVC venv python> sing_worker.py <rvc_repo> <model_pth> <index_pth>
模型经 infer.vc.modules.VC.get_vc 加载（weight_root 指向模型所在目录）。
"""
import json
import os
import sys
import time


def _emit(obj) -> None:
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    repo, model_pth, index_pth = sys.argv[1], sys.argv[2], sys.argv[3]
    os.environ["weight_root"] = os.path.dirname(os.path.abspath(model_pth))
    os.environ.setdefault("rmvpe_root", os.path.join(repo, "assets", "rmvpe"))
    sys.path.insert(0, repo)

    try:
        import numpy as np  # noqa: F401  —— 提前 import，失败也算启动失败
        import soundfile as sf  # noqa: F401
        import torch
        from configs.config import Config
        from infer.vc.modules import VC
    except Exception as e:  # noqa: BLE001
        _emit({"ready": False, "error": f"RVC 环境加载失败: {e}"})
        return 1

    try:
        # Config() 内部会跑自己的 argparse（--port/--pycmd…），暂清 argv 防止它吃掉 worker 参数
        _saved_argv = sys.argv
        sys.argv = [sys.argv[0]]
        try:
            config = Config()
        finally:
            sys.argv = _saved_argv
        vc = VC(config)
        vc.get_vc(os.path.basename(model_pth))
    except Exception as e:  # noqa: BLE001
        _emit({"ready": False, "error": f"声库加载失败: {e}"})
        return 1

    _emit({"ready": True, "tgt_sr": int(vc.tgt_sr)})

    #: RMVPE 懒加载（只有 f0_key 动作用）
    _rmvpe = None

    def _f0_key(input_wav: str, band_lo: float, band_hi: float) -> dict:
        """副歌切段 → F0 分位数 + 使带宽外占比最小的移调 key（plan §3.3 门槛 1）。"""
        nonlocal _rmvpe
        import numpy as np

        data, sr = sf.read(input_wav, dtype="float32", always_2d=True)
        x = data.mean(axis=1)
        x16 = torch.from_numpy(x)
        if sr != 16000:
            import torchaudio.functional as AF

            x16 = AF.resample(x16, sr, 16000)
        x16 = x16.unsqueeze(0).to(config.device)
        if _rmvpe is None:
            from infer.rmvpe import RMVPE

            _rmvpe = RMVPE(
                os.path.join(repo, "assets", "rmvpe", "rmvpe.pt"),
                is_half=False, device=config.device,
            )
        f0 = _rmvpe.infer_from_audio(x16, thred=0.03)
        v = f0[(f0 > 50) & (f0 < 1100)]
        if len(v) == 0:
            raise RuntimeError("无有效人声帧，无法定调")
        p = {f"p{i}": float(np.percentile(v, i)) for i in (5, 25, 50, 75, 95)}
        best_key, best_frac = 0, 1.1
        for k in range(-12, 13):
            s = 2 ** (k / 12.0)
            frac = (max(0.0, (band_lo - p["p5"] * s)) / band_lo
                    + max(0.0, (p["p95"] * s - band_hi)) / band_hi)
            if frac < best_frac - 1e-9 or (abs(frac - best_frac) < 1e-9 and abs(k) < abs(best_key)):
                best_key, best_frac = k, frac
        return {**p, "voiced": int(len(v)), "key": int(best_key),
                "out_frac": round(best_frac, 3)}

    def _cut_chorus(input_wav: str, out_wav: str, seconds: float) -> dict:
        """副歌启发式切取：0.5s 帧 RMS + 5s 平滑，能量最大窗口（中后部 1.15x 偏置）。"""
        import numpy as np

        data, sr = sf.read(input_wav, dtype="float32", always_2d=True)
        x = data.mean(axis=1)
        frame = int(0.5 * sr)
        n = len(x) // frame
        if n == 0:
            raise RuntimeError("音频过短")
        rms = np.sqrt((x[: n * frame].reshape(n, frame) ** 2).mean(axis=1) + 1e-12)
        w = max(1, int(seconds / 0.5))
        if n <= w:
            start_f = 0
        else:
            smooth = np.convolve(rms, np.ones(w) / w, mode="valid")
            bias = np.where(np.linspace(0, 1, len(smooth)) > 0.35, 1.15, 1.0)
            start_f = int(np.argmax(smooth * bias))
        seg = x[start_f * frame: start_f * frame + w * frame]
        peak = float(np.abs(seg).max())
        if peak > 0.98:
            seg = seg * (0.98 / peak)
        os.makedirs(os.path.dirname(os.path.abspath(out_wav)), exist_ok=True)
        sf.write(out_wav, seg, sr)
        return {"out": out_wav, "start_s": round(start_f * 0.5, 1),
                "dur": round(len(seg) / sr, 1)}

    for raw in sys.stdin.buffer:
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            _emit({"id": None, "ok": False, "error": f"bad request: {e}"})
            continue
        rid = req.get("id")
        action = str(req.get("action") or "sing")
        try:
            if action == "cut_chorus":
                out = _cut_chorus(str(req["input"]), str(req["out"]),
                                  float(req.get("seconds", 50)))
                _emit({"id": rid, "ok": True, **out})
                continue
            if action == "f0_key":
                out = _f0_key(str(req["input"]),
                              float(req.get("band_lo", 210)), float(req.get("band_hi", 490)))
                _emit({"id": rid, "ok": True, **out})
                continue
            if action != "sing":
                raise ValueError(f"unknown action: {action}")
            inp = str(req.get("input") or "")
            out = str(req.get("out") or "")
            if not inp or not out:
                raise ValueError("input/out 不能为空")
            t0 = time.time()
            torch.manual_seed(int(req.get("seed") or 0))
            _, (sr, audio) = vc.vc_single(
                0,
                inp,
                int(req.get("key") or 0),
                "rmvpe",
                index_pth,
                float(req.get("index_rate", 0.66)),
                0,  # resample_sr：保持模型采样率
                0.25,  # rms_mix_rate
                float(req.get("protect", 0.33)),
            )
            if audio is None:
                raise RuntimeError("转换失败（空输出）")
            audio = np.asarray(audio)
            sr = int(sr)
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            sf.write(out, audio, sr)
            _emit({
                "id": rid, "ok": True, "out": out, "sr": sr,
                "seconds": round(time.time() - t0, 2),
                "dur": round(len(audio) / sr, 1),
            })
        except Exception as e:  # noqa: BLE001
            _emit({"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
