"""语音插件自带管理页（右侧 dock 注入，参照 B 站出站审批页）。

分工：
- 页面本体 = ``page/index.html``（纯静态、零构建），由本进程出；
- 数据 = 本进程反代到主后端：只放本插件需要的四个端点，不做任意反代；
- 端口由宿主生命周期注入（manifest ``lifecycle.spawn`` → ServiceManager），
  本进程不读任何端口配置；``--port`` 缺失/<=0 直接报错退出。

启动（宿主代劳；手工联调同一命令）：
    python -m plugins.voice.page_server --port 18752
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

PAGE_DIR = Path(__file__).resolve().parent / "page"
BACKEND = (os.environ.get("OMNI_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")

#: 插件页公开路径 → 主后端路径。只放语音插件自己的配置面和试听，未知路径一律 404。
API_TARGETS: Dict[str, str] = {
    "/api/config": "/api/runtime/tools/plugins/voice/config?scope=plugin",
    "/api/voices": "/api/models/services",
    "/api/tts": "/api/audio/tts",
}


def backend_path(path: str) -> Optional[str]:
    """插件页公开路径 → 主后端路径；不在白名单返回 None。"""
    return API_TARGETS.get(path)


def _proxy(path: str, method: str, body: Optional[bytes] = None) -> tuple:
    req = urllib.request.Request(BACKEND + path, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return int(getattr(r, "status", 200)), r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read(), e.headers.get("Content-Type", "application/json")
    except Exception as e:  # noqa: BLE001 —— 后端没起时页面要看到原因，不是裸 500
        return 502, json.dumps({"ok": False, "error": f"backend unreachable: {e}"}).encode(), "application/json"


class Handler(BaseHTTPRequestHandler):
    server_version = "OmniAgentVoicePage/1.0"

    def _send(self, status: int, payload: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _static(self, name: str) -> None:
        f = (PAGE_DIR / name).resolve()
        if not f.is_file() or PAGE_DIR.resolve() not in f.parents:
            self._send(404, b"not found", "text/plain")
            return
        ctype = "text/html" if f.suffix == ".html" else "text/plain"
        self._send(200, f.read_bytes(), ctype + "; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, b'{"ok":true}')
            return
        if path in ("/", "/index.html"):
            self._static("index.html")
            return
        if path.startswith("/api/"):
            target = backend_path(path)
            if target is None:
                self._send(404, b'{"ok":false,"error":"not allowed"}')
                return
            status, payload, ctype = _proxy(target, "GET")
            self._send(status, payload, str(ctype or "application/json"))
            return
        self._send(404, b"not found", "text/plain")

    def do_PUT(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        target = backend_path(path) if path.startswith("/api/") else None
        if target is None:
            self._send(404, b'{"ok":false,"error":"not allowed"}')
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload, ctype = _proxy(target, "PUT", body)
        self._send(status, payload, str(ctype or "application/json"))

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        target = backend_path(path) if path == "/api/tts" else None
        if target is None:
            self._send(404, b'{"ok":false,"error":"not allowed"}')
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, payload, ctype = _proxy(target, "POST", body)
        self._send(status, payload, str(ctype or "application/json"))

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默：日志由宿主重定向落盘
        pass


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="语音插件管理页（插件自带）")
    ap.add_argument("--port", type=int, required=True, help="监听端口（由宿主生命周期注入）")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    if args.port <= 0:
        print("[voice-page] 端口未注入（manifest lifecycle.spawn.port 为空）", file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[voice-page] listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
