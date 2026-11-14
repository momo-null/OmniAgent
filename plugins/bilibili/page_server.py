"""B站出站审批页的插页服务（插件自带页面，阶段 6）。

分工：
- 页面本体 = ``page/index.html``（纯静态、零构建），由本进程出；
- 数据 = 本进程反代到主后端（``/bilibili/outbox*``）。插件页只调自己的服务：
  ``/api/outbox*`` → ``/bilibili/outbox*``；反代是唯一干净通路；
- 端口由宿主生命周期注入（manifest ``lifecycle.spawn`` → ServiceManager），
  本进程不读任何端口配置；``--port`` 缺失/<=0 直接报错退出（不做静态配置双源）。

启动（宿主代劳；手工联调同一命令）：
    python -m plugins.bilibili.page_server --port 18751
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
#: 主后端地址：本机回环；换端口时用环境变量覆盖（部署事实，不写进仓库默认）
BACKEND = (os.environ.get("OMNI_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")

#: 反代允许的路径前缀（只放审批面，其他一律 404）
API_PREFIX = "/api/outbox"
BACKEND_PREFIX = "/bilibili/outbox"


def backend_path(path: str) -> Optional[str]:
    """插件页公开路径 → 主后端路径。

    只放 ``/api/outbox`` 这一棵子树，避免变成任意后端反代：
    ``/api/outbox`` → ``/bilibili/outbox``，
    ``/api/outbox/1/approve`` → ``/bilibili/outbox/1/approve``。
    """
    if path == API_PREFIX:
        return BACKEND_PREFIX
    if path.startswith(API_PREFIX + "/"):
        return BACKEND_PREFIX + path[len(API_PREFIX):]
    return None


def _proxy(path: str, method: str, body: Optional[bytes] = None) -> tuple:
    req = urllib.request.Request(BACKEND + path, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(getattr(r, "status", 200)), r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read(), e.headers.get("Content-Type", "application/json")
    except Exception as e:  # noqa: BLE001 —— 后端没起时页面要看到原因，不是裸 500
        return 502, json.dumps({"ok": False, "error": f"backend unreachable: {e}"}).encode(), "application/json"


class Handler(BaseHTTPRequestHandler):
    server_version = "OmniAgentPluginPage/1.0"

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

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            target = backend_path(path)
            if target is None:
                self._send(404, b'{"ok":false,"error":"not allowed"}')
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, payload, ctype = _proxy(target, "POST", body)
            self._send(status, payload, str(ctype or "application/json"))
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默：日志由宿主重定向落盘
        pass


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="B站审批页插页服务（插件自带）")
    ap.add_argument("--port", type=int, required=True, help="监听端口（由宿主生命周期注入）")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    if args.port <= 0:
        print("[page_server] 端口未注入（manifest lifecycle.spawn.port 为空）", file=sys.stderr)
        return 2
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[page_server] listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
