"""voice engine service —— 本机语音引擎的常驻服务门面（M2.5a 语音解耦）。

把本插件的 STT（SenseVoice）/ TTS（GPT-SoVITS worker）以 **OpenAI 音频兼容**
形状暴露成独立 HTTP 服务。内核、Web、通道网关与手机壳只认这一种形状：

    GET  /healthz                   -> {"ok": true, "stt_loaded": false, ...}
    GET  /v1/models                 -> {"object": "list", "data": [...]}
    POST /v1/audio/transcriptions   multipart(file=wav, model) -> {"text": ...}
    POST /v1/audio/speech           json{model, input, voice}  -> audio/wav

内存特性（**常驻监听 + 懒加载 + 空闲只释放模型**）：
- 进程启动不加载任何模型（几十 MB，只有 HTTP 监听）；
- 首个请求才构造 STT / 拉起 TTS 子进程——**没有预热也能用，只是第一句慢一点**；
- 连续 ``idle_release_sec`` 无请求 → 释放引擎（STT 丢引用 + 杀 TTS 子进程），
  **进程不退**：监听一直在，下次请求重新懒加载。这和「空闲自杀」的区别很关键——
  自杀之后没人再拉起它，下一次请求就是 connection refused。

配置分两份（服务化后的边界）：
- 引擎（本文件）：``~/.omniagent/voice_service.yaml``——模型路径 / 音色 / 空闲回收 /
  监听端口（``service_port`` / ``service_idle_sec`` / ``service_host`` / ``service_token``）。
- 调用端（插件）：``~/.omniagent/plugins/voice.yaml``——只剩端点：
    base_url: http://127.0.0.1:18742/v1
    api_key: ""            # 本地服务不校验；启动时给了 --token 才校验
    stt_model: sense-voice
    tts_model: gpt-sovits
    voice: ""              # 空 = 用服务端 tts_voice 配置

启动（desktop 自动拉起；手动/服务器部署同一条命令）：
    python -m plugins.voice.engine_service          # 端口读 voice_service.yaml 的 service_port
    # 需要给局域网别的机器用时显式放开，并带 token（默认只听回环）
    python -m plugins.voice.engine_service --host 0.0.0.0 --token <secret>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_IDLE_RELEASE_SEC = 600.0
_TOKEN = ""
_last_request = time.time()


def _plugin():
    """延迟导入：进程启动只拉起监听，不碰 sherpa/torch 依赖链。"""
    from plugins.voice import plugin

    return plugin


def _touch() -> None:
    global _last_request
    _last_request = time.time()


def _transcribe_multipart(body: bytes, boundary: str) -> bytes:
    """multipart 请求体里抠出 file 字段的字节（stdlib 解析，零依赖）。"""
    delim = ("--" + boundary).encode()
    for part in body.split(delim):
        head, _, payload = part.partition(b"\r\n\r\n")
        if b'name="file"' in head and payload:
            return payload.rsplit(b"\r\n", 1)[0]
    raise ValueError("multipart 里没有 file 字段")


def _multipart_boundary(ctype: str) -> str:
    for seg in (ctype or "").split(";"):
        seg = seg.strip()
        if seg.startswith("boundary="):
            return seg.split("=", 1)[1].strip('" ')
    return ""


class _Server(ThreadingHTTPServer):
    """端口占用必须失败。

    ``allow_reuse_address`` 在 Windows 上允许多个进程绑同一端口（请求随机落到谁
    手里），对「本机唯一语音服务」是灾难——宁可启动失败让壳记日志。
    """

    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "OmniVoiceEngine/1.0"

    def log_message(self, fmt, *args):  # 静默默认访问日志（服务以安静为荣）
        pass

    # ── 鉴权（只影响非回环暴露场景；默认只监听回环，无 token 也能用）──
    def _authorized(self) -> bool:
        if not _TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {_TOKEN}"

    def do_GET(self):  # noqa: N802
        path = self.path.rstrip("/") or "/"
        if path in ("/healthz", "/v1/healthz"):
            _touch()
            return self._reply_json(self._health())
        if path == "/v1/models":
            if not self._authorized():
                return self._reply_json({"error": "unauthorized"}, 401)
            _touch()
            return self._reply_json({
                "object": "list",
                "data": [
                    {"id": "sense-voice", "object": "model", "owned_by": "local"},
                    {"id": "gpt-sovits", "object": "model", "owned_by": "local"},
                ],
            })
        self._reply_json({"error": "unknown path"}, 404)

    def do_POST(self):  # noqa: N802
        _touch()
        if not self._authorized():
            return self._reply_json({"error": "unauthorized"}, 401)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            plugin = _plugin()
            path = self.path.rstrip("/")
            if path.endswith("/audio/transcriptions"):
                boundary = _multipart_boundary(self.headers.get("Content-Type") or "")
                if not boundary:
                    raise ValueError("multipart boundary 缺失")
                wav = _transcribe_multipart(body, boundary)
                self._reply_json({"text": plugin.transcribe_local(wav)})
            elif path.endswith("/audio/speech"):
                req = json.loads(body.decode("utf-8"))
                text = str(req.get("input") or "").strip()
                if not text:
                    raise ValueError("input 为空")
                # 音色名不认识（OpenAI 客户端会传 alloy/nova 之类）就交回默认音色，
                # 不报错——服务对外的语义是「能读出来」，不是「必须点某个音色」。
                voice = str(req.get("voice") or "").strip()
                if voice and voice not in plugin.status()["tts"]["voices"]:
                    voice = ""
                self._reply(200, plugin.tts_local(text, voice), "audio/wav")
            else:
                self._reply_json({"error": "unknown path"}, 404)
        except ValueError as e:  # 请求体问题 = 4xx，不该报成服务端故障
            self._reply_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001 —— 单次失败回给调用方，进程不退
            self._reply_json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ── 回包 ──
    def _health(self) -> Dict[str, Any]:
        plugin = _plugin()
        st = plugin.status()
        return {
            "ok": True,
            "service": "voice-engine",
            "loaded": plugin.engines_loaded(),
            "stt_loaded": st["stt"]["loaded"],
            "stt_ready": st["stt"]["ready"],
            "tts_running": st["tts"]["running"],
            "tts_pid": st["tts"]["pid"],
            "voices": st["tts"]["voices"],
            "voice_default": st["tts"]["voice_default"],
            "stt_idle_sec": st["stt"]["idle_sec"],
            "tts_idle_sec": st["tts"]["idle_sec"],
            "models_ready": bool(st["stt"]["ready"] and st["tts"]["weights_ready"]),
            "idle_sec": round(time.time() - _last_request, 1),
            "idle_release_sec": _IDLE_RELEASE_SEC,
        }

    def _reply_json(self, payload: Dict[str, Any], code: int = 200) -> None:
        self._reply(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json")

    def _reply(self, code: int, payload: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except Exception:  # noqa: BLE001 —— 客户端断开不算错
            pass


def _watchdog(plugin) -> None:
    """空闲释放：只回收模型，不退出进程（监听必须一直在）。"""
    while True:
        time.sleep(15)
        idle = _IDLE_RELEASE_SEC
        if idle <= 0:
            continue
        if time.time() - _last_request < idle:
            continue
        try:
            if plugin.engines_loaded():
                print(f"[voice-engine] idle {int(time.time() - _last_request)}s "
                      f"-> release engines（进程常驻，下次请求重新懒加载）", flush=True)
                plugin.release_engines()
        except Exception as e:  # noqa: BLE001 —— 回收失败不该拖垮服务
            print(f"[voice-engine] release failed: {type(e).__name__}: {e}", flush=True)


def _defaults() -> Tuple[str, int, float, str]:
    """从 ``voice_service.yaml`` 取缺省值（命令行显式给的就以命令行为准）。"""
    cfg: Dict[str, Any] = {}
    try:
        from plugins.voice.service_config import service_cfg

        cfg = service_cfg()
    except Exception:  # noqa: BLE001
        pass
    return (
        str(cfg.get("service_host") or "127.0.0.1"),
        int(cfg.get("service_port") or 18742),
        float(cfg.get("service_idle_sec") or 600),
        str(cfg.get("service_token") or ""),
    )


def main() -> int:
    global _IDLE_RELEASE_SEC, _TOKEN
    host_def, port_def, idle_def, token_def = _defaults()
    ap = argparse.ArgumentParser(description="本机语音引擎常驻服务（OpenAI 音频兼容形状）")
    ap.add_argument("--port", type=int, default=port_def)
    ap.add_argument("--host", default=host_def,
                    help="默认只听回环；给局域网用需显式 --host 0.0.0.0 并配 --token")
    ap.add_argument("--idle-release-sec", type=float, default=idle_def,
                    help="连续无请求 N 秒后释放模型（不退出进程；0=常驻不释放）")
    ap.add_argument("--token", default=token_def, help="非空时校验 Authorization: Bearer")
    args = ap.parse_args()
    _IDLE_RELEASE_SEC = args.idle_release_sec
    _TOKEN = args.token

    plugin = _plugin()
    threading.Thread(target=_watchdog, args=(plugin,), name="voice-idle-watchdog",
                     daemon=True).start()
    try:
        srv = _Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"[voice-engine] bind {args.host}:{args.port} failed: {e}", flush=True)
        return 3
    print(f"[voice-engine] listening on {args.host}:{args.port} "
          f"(transcriptions/speech; idle-release {int(_IDLE_RELEASE_SEC)}s; "
          f"lazy load)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
