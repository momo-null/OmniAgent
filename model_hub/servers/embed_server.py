"""嵌入 sidecar 服务（独立 venv 内运行；主 .venv 零改动）。

用法（venv 内）：
    python scripts/embed_server.py --model-dir <model_hub/bge-m3> \
        [--port 18743] [--device auto|gpu|cpu]

- 设备降级链：auto = CUDA 可用且空闲显存 ≥2GB → GPU(fp16)，否则 CPU；
  gpu 加载失败降 CPU；运行中 CUDA OOM → 自动落 CPU 并钉住。
- HTTP：GET /health（就绪与设备信息）；POST /embed {"texts":[...]} →
  {"vectors":[[...]]}（L2 归一化，余弦=点积）。全部 stdlib HTTP，无框架依赖。
- 本脚本不 import omni_core（在别人的 venv 里跑，零内核依赖）。
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def pick_device(pref: str) -> str:
    if pref == "cpu":
        return "cpu"
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        if pref == "gpu":
            return "cuda"
        free, _total = torch.cuda.mem_get_info()
        if free >= 2.0 * 1024 ** 3:  # 给语音引擎留共存空间
            return "cuda"
    except Exception:
        pass
    return "cpu"


ARGS = None
MODEL = None
DIM = 0
DEVICE = ""
_LOCK = threading.Lock()


def load_model() -> None:
    global MODEL, DIM, DEVICE
    from sentence_transformers import SentenceTransformer

    device = pick_device(ARGS.device)
    model = SentenceTransformer(ARGS.model_dir, device=device)
    MODEL, DEVICE = model, device
    DIM = int(model.get_sentence_embedding_dimension() or 0)
    print(f"[embed-svc] model={ARGS.model_dir} device={device} dim={DIM}", flush=True)


def encode_safe(texts):
    """OOM 自动落 CPU：CUDA OOM 时以 CPU 重载模型并重试（本进程内钉住 CPU）。"""
    global MODEL, DEVICE
    try:
        return MODEL.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    except RuntimeError as e:
        if "CUDA out of memory" not in str(e) or DEVICE == "cpu":
            raise
        print("[embed-svc] CUDA OOM → 降 CPU 重载", flush=True)
        import gc
        import torch
        del MODEL
        gc.collect()
        torch.cuda.empty_cache()
        MODEL = SentenceTransformer(ARGS.model_dir, device="cpu")
        DEVICE = "cpu"
        return MODEL.encode(texts, normalize_embeddings=True, show_progress_bar=False)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默逐请求日志（svc.log 只留关键事件）
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": MODEL is not None, "model": ARGS.model_dir,
                             "dim": DIM, "device": DEVICE})
        else:
            self._json(404, {"ok": False})

    def do_POST(self):
        if self.path != "/embed" or MODEL is None:
            self._json(503, {"ok": False})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            texts = json.loads(self.rfile.read(n).decode("utf-8")).get("texts") or []
            if not isinstance(texts, list) or not texts:
                self._json(400, {"ok": False, "error": "texts required"})
                return
            with _LOCK:
                vecs = encode_safe([str(t) for t in texts])
            self._json(200, {"ok": True, "vectors": [v.tolist() for v in vecs]})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def main() -> int:
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--port", type=int, default=18743)
    ap.add_argument("--device", default="auto")
    ARGS = ap.parse_args()
    load_model()
    ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
