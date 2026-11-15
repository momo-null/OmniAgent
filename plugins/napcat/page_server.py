"""QQ 登录托管页的插页服务（插件自带常驻进程）。

分工与 bilibili/voice 的 page_server 同构（页面 = 纯静态 ``page/index.html``，
本进程出页面与数据），但本进程额外当 **NapCat 的托管员**——这是插件行为，
不是 agent 内核的绑定：

- 自动拉起 ``napcat.bat``（manifest ``lifecycle.spawn`` 由 ServiceManager 先拉起
  本进程；本进程再按 ``napcat_home`` 配置拉起 NapCat，端口占用即视为已在跑）；
- 抓 NapCat stdout → 环形缓冲 + 落盘 ``~/.omniagent/runtime/napcat/``（仓外），
  供插件页实时看日志，不用再盯前台 CLI；
- 经 NapCat 自带 WebUI API（``config/webui.json`` 的 token，只留在服务端）查
  登录状态、取二维码内容并服务端渲染成 PNG、执行刷新/快速登录。

配置源 = 通道同一份 ``~/.omniagent/plugins/napcat.yaml``，新增两键（见
``examples/plugins/napcat.yaml``）::

    napcat_home: <OneKey Shell 目录，含 napcat.bat>   # 空 = 不托管，页面只做观察
    webui_url: http://127.0.0.1:6099                  # NapCat WebUI 地址

启动（宿主代劳；手工联调同一命令）::

    python -m plugins.napcat.page_server --port 18753
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PAGE_DIR = Path(__file__).resolve().parent / "page"
#: 主后端地址（本机回环；换端口时用环境变量覆盖——部署事实，不写进仓库默认）。
#: 配置编辑走宿主通用插件配置 API（schema 在 manifest，宿主做校验/secret 掩码），
#: 本进程不自写第二份配置读写。
BACKEND = (os.environ.get("OMNI_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")
CONFIG_API = "/api/runtime/tools/plugins/napcat/config?scope=plugin"

#: 日志环形缓冲上限（行）——SSE 回放窗口
LOG_CAP = 500
#: 进程日志落盘上限（字节），超过则在启动时截断重开
LOG_FILE_CAP = 8 * 1024 * 1024
#: 本机 HTTP 调用超时（NapCat WebUI / OneBot 探测都在本机）
LOCAL_TIMEOUT = 5.0


# ── 配置与路径 ───────────────────────────────────────────────────────────────

def _global_omni() -> Path:
    try:
        from omni_core.local.runtime_paths import global_omni

        return Path(global_omni())
    except Exception:  # noqa: BLE001 —— 独立进程，兜底到默认家目录
        return Path(os.path.expanduser("~")) / ".omniagent"


def _plugin_yaml() -> Path:
    return _global_omni() / "plugins" / "napcat.yaml"


def _runtime_dir() -> Path:
    return _global_omni() / "runtime" / "napcat"


def load_config() -> Dict[str, Any]:
    """读插件 yaml（每次现读：改完路径不用重启本进程）。"""
    try:
        import yaml

        raw = _plugin_yaml()
        if not raw.is_file():
            return {}
        data = yaml.safe_load(raw.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— 配置坏了不拖垮状态面
        return {}


def _port_of(url: str, default: int) -> int:
    try:
        from urllib.parse import urlparse

        port = urlparse(url).port
        return int(port) if port else default
    except Exception:
        return default


def port_open(port: int, timeout: float = 0.6) -> bool:
    if port <= 0:
        return False
    try:
        with __import__("socket").create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


# ── 进程日志：环形缓冲 + 落盘 ────────────────────────────────────────────────

class LogRing:
    """stdout 环形缓冲：SSE 客户端按 seq 续读，重连不重不漏（窗口内）。"""

    def __init__(self, cap: int = LOG_CAP):
        self._cap = cap
        self._lock = threading.Lock()
        self._buf: deque = deque(maxlen=cap)
        self._seq = 0

    def append(self, line: str) -> int:
        with self._lock:
            self._seq += 1
            self._buf.append((self._seq, line))
            return self._seq

    def since(self, seq: int) -> Tuple[List[Tuple[int, str]], int]:
        with self._lock:
            lines = [(s, text) for (s, text) in self._buf if s > seq]
            return lines, self._seq


# ── NapCat 进程托管 ──────────────────────────────────────────────────────────

class NapCatProc:
    """napcat.bat 的托管员：拉起、抓 stdout、树杀；外部实例按端口收编停止。"""

    def __init__(self, cfg_provider: Callable[[], Dict[str, Any]], ring: LogRing):
        self._cfg = cfg_provider
        self.ring = ring
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._log_fp = None

    # ── 配置推导 ──
    def bat_path(self) -> Optional[Path]:
        home = str(self._cfg().get("napcat_home") or "").strip()
        if not home:
            return None
        bat = Path(home) / "napcat.bat"
        return bat if bat.is_file() else None

    def onebot_port(self) -> int:
        return _port_of(str(self._cfg().get("http_url") or ""), 3000)

    # ── 状态 ──
    def managed_alive(self) -> bool:
        proc = self._proc
        return bool(proc and proc.poll() is None)

    def external_running(self) -> bool:
        """不是本进程拉起的实例（手动跑的 bat）：按 OneBot 端口判。"""
        return (not self.managed_alive()) and port_open(self.onebot_port())

    def running(self) -> bool:
        return self.managed_alive() or self.external_running()

    # ── 生命周期 ──
    def spawn(self) -> Dict[str, Any]:
        bat = self.bat_path()
        if bat is None:
            return {"result": "not_configured"}
        if self.running():
            return {"result": "already"}
        with self._lock:
            if self.managed_alive():
                return {"result": "already"}
            try:
                self._open_log_file()
                flags = 0
                if os.name == "nt":
                    # 与 ServiceManager 同款：不弹控制台 + 保留进程组便于整组终止
                    flags = 0x08000000 | 0x00000200
                self.ring.append(f"[托管] 拉起 NapCat: {bat}")
                self._proc = subprocess.Popen(
                    ["cmd", "/c", str(bat)], cwd=str(bat.parent),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    close_fds=False, creationflags=flags)
            except Exception as e:  # noqa: BLE001 —— 页面要看到原因，不是裸 500
                self.ring.append(f"[托管] 拉起失败: {type(e).__name__}: {e}")
                return {"result": "error", "error": f"{type(e).__name__}: {e}"}
        threading.Thread(target=self._reader, args=(self._proc,),
                         name="napcat-stdout", daemon=True).start()
        return {"result": "started", "pid": self._proc.pid}

    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                self.ring.append(raw.rstrip("\r\n"))
                self._write_log(raw.rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass
        finally:
            code = proc.poll()
            if code is None:
                # stdout EOF 可能早于父进程收到退出状态：短暂等一次真实退出码
                try:
                    code = proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            self.ring.append(f"[托管] napcat 进程已退出 (code={code})")
            self._write_log(f"[托管] napcat 进程已退出 (code={code})")
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            self._close_log_file()

    def stop(self) -> Dict[str, Any]:
        """树杀：本进程拉的实例直接杀；外部实例按 OneBot 端口找 pid 兜底。"""
        with self._lock:
            proc, self._proc = self._proc, None
        pid = proc.pid if proc else self._pid_on_port(self.onebot_port())
        if not pid:
            return {"result": "not_running"}
        try:
            import psutil

            p = psutil.Process(pid)
            victims = [p] + p.children(recursive=True)
            for v in victims:
                try:
                    v.terminate()
                except Exception:  # noqa: BLE001
                    pass
            _, alive = psutil.wait_procs(victims, timeout=5)
            for v in alive:
                try:
                    v.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.ring.append(f"[托管] 已停止 NapCat (pid={pid})")
            return {"result": "stopped", "pid": pid}
        except Exception as e:  # noqa: BLE001
            return {"result": "error", "error": f"{type(e).__name__}: {e}"}

    def restart(self) -> Dict[str, Any]:
        stop = self.stop()
        result = self.spawn()
        return {"stop": stop, "spawn": result}

    # ── 落盘 ──
    def _log_path(self) -> Path:
        return _runtime_dir() / "napcat.log"

    def _open_log_file(self) -> None:
        try:
            path = self._log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_FILE_CAP:
                path.write_text("", encoding="utf-8")  # 启动时截断，不无限膨胀
            self._log_fp = open(path, "ab")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self._log_fp.write(f"\n===== 托管启动 {stamp} =====\n".encode("utf-8"))
        except Exception:  # noqa: BLE001 —— 落盘失败不影响日志缓冲与状态面
            self._log_fp = None

    def _write_log(self, line: str) -> None:
        fp = self._log_fp
        if fp is None:
            return
        try:
            fp.write((line + "\n").encode("utf-8"))
            fp.flush()
        except Exception:  # noqa: BLE001
            pass

    def _close_log_file(self) -> None:
        fp, self._log_fp = self._log_fp, None
        if fp is not None:
            try:
                fp.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _pid_on_port(port: int) -> Optional[int]:
        if port <= 0:
            return None
        try:
            import psutil

            for c in psutil.net_connections(kind="inet"):
                if (c.laddr and c.laddr.port == port
                        and c.status == psutil.CONN_LISTEN and c.pid):
                    return int(c.pid)
        except Exception:  # noqa: BLE001
            return None
        return None


# ── NapCat WebUI 客户端（token 只留在服务端） ────────────────────────────────

class WebUIClient:
    """NapCat 自带 WebUI 的最小客户端：login 换 Credential，之后 Bearer 调用。"""

    def __init__(self, cfg_provider: Callable[[], Dict[str, Any]]):
        self._cfg = cfg_provider
        self._cred: Optional[str] = None
        self._token_cache: Tuple[float, Optional[str]] = (0.0, None)
        self._quick_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
        self._lock = threading.Lock()

    # ── 基址与 token ──
    def _base(self) -> str:
        return str(self._cfg().get("webui_url") or "http://127.0.0.1:6099").rstrip("/")

    def _token(self) -> Optional[str]:
        """token 读自 NapCat 的 config/webui.json（按 mtime 缓存）。"""
        home = str(self._cfg().get("napcat_home") or "").strip()
        if not home:
            return None
        pattern = Path(home) / "versions" / "*" / "resources" / "app" / "napcat" / "config" / "webui.json"
        files = sorted(glob.glob(str(pattern)))
        if not files:
            return None
        try:
            path = files[0]
            mtime = os.path.getmtime(path)
            cached_mtime, cached = self._token_cache
            if cached is not None and cached_mtime == mtime:
                return cached
            import json as _json

            data = _json.loads(Path(path).read_text(encoding="utf-8"))
            token = str(data.get("token") or "").strip() or None
            self._token_cache = (mtime, token)
            return token
        except Exception:  # noqa: BLE001
            return None

    # ── 认证 ──
    def _login(self) -> Optional[str]:
        token = self._token()
        if not token:
            return None
        # NapCat 口径：hash = sha256(token + ".napcat")（hex）
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        try:
            req = urllib.request.Request(
                self._base() + "/api/auth/login",
                data=json.dumps({"hash": digest}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            cred = ((body or {}).get("data") or {}).get("Credential")
            self._cred = str(cred) if cred else None
            return self._cred
        except Exception:  # noqa: BLE001
            self._cred = None
            return None

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[bool, Any, str]:
        """POST /api/<path>；code==0 → (True, data, "")。401/失效重登一次。"""
        with self._lock:
            cred = self._cred or self._login()
        if cred is None:
            return False, None, "webui 未就绪（token 读不到或登录失败）"
        payload = json.dumps(body or {}).encode()
        for attempt in (1, 2):
            req = urllib.request.Request(
                self._base() + "/api/" + path.lstrip("/"), data=payload,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {cred}"},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as r:
                    resp = json.loads(r.read().decode("utf-8", "replace"))
                if isinstance(resp, dict) and resp.get("code") == 0:
                    return True, resp.get("data"), ""
                msg = str((resp or {}).get("message") or "webui 返回错误")
                if attempt == 1 and ("token" in msg.lower() or "unauthorized" in msg.lower()):
                    with self._lock:
                        cred = self._login()
                    if cred:
                        continue
                return False, None, msg
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 1:
                    with self._lock:
                        cred = self._login()
                    if cred:
                        continue
                return False, None, f"webui HTTP {e.code}"
            except Exception as e:  # noqa: BLE001 —— WebUI 没起是最常见路径，如实上报
                return False, None, f"{type(e).__name__}: {e}"
        return False, None, "webui 认证失败"

    # ── 业务面 ──
    def login_status(self) -> Tuple[Optional[Dict[str, Any]], str]:
        ok, data, err = self._post("QQLogin/CheckLoginStatus")
        return (data if isinstance(data, dict) else None), ("" if ok else err)

    def login_info(self) -> Optional[Dict[str, Any]]:
        ok, data, _ = self._post("QQLogin/GetQQLoginInfo")
        return data if ok and isinstance(data, dict) else None

    def refresh_qrcode(self) -> Tuple[bool, str]:
        ok, _, err = self._post("QQLogin/RefreshQRcode")
        return ok, err

    def quick_login(self, uin: str) -> Tuple[bool, str]:
        ok, _, err = self._post("QQLogin/SetQuickLogin", {"uin": str(uin)})
        return ok, err

    def quick_logins(self) -> List[Dict[str, Any]]:
        """快速登录候选（60s 缓存；没登录才有意义，调用方按需取）。"""
        now = time.time()
        if now - self._quick_cache[0] < 60.0:
            return self._quick_cache[1]
        ok, data, _ = self._post("QQLogin/GetQuickLoginListNew")
        items = data if ok and isinstance(data, list) else []
        self._quick_cache = (now, [x for x in items if isinstance(x, dict)])
        return self._quick_cache[1]


# ── 配置编辑反代（唯一放行的宿主子树：本插件的通用配置 API） ─────────────────

def _proxy_config(method: str, body: Optional[bytes] = None) -> tuple:
    req = urllib.request.Request(BACKEND + CONFIG_API, data=body, method=method)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return int(getattr(r, "status", 200)), r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read(), e.headers.get("Content-Type", "application/json")
    except Exception as e:  # noqa: BLE001 —— 后端没起时页面要看到原因，不是裸 500
        return 502, json.dumps({"ok": False, "error": f"backend unreachable: {e}"}).encode(), "application/json"


# ── 状态聚合 ─────────────────────────────────────────────────────────────────

PHASE_LABELS = {
    "waiting_qrcode": "等待扫码",
    "qrcode_scanned": "已扫码，待确认",
    "initializing": "登录初始化中",
}


def _proc_status(proc: NapCatProc) -> Dict[str, Any]:
    bat = proc.bat_path()
    return {
        "configured": bat is not None,
        "bat": str(bat) if bat else "",
        "running": proc.running(),
        "managed": proc.managed_alive(),
        "external": proc.external_running(),
        "pid": proc._proc.pid if proc.managed_alive() else None,
        "log_file": str(proc._log_path()),
    }


def _login_status(webui: WebUIClient, logged_hint: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """CheckLoginStatus 为主，已登录时补 GetQQLoginInfo（uin/昵称/头像）。"""
    status, err = webui.login_status()
    out: Dict[str, Any] = {
        "webui_ok": status is not None,
        "error": err,
        "isLogin": bool((status or {}).get("isLogin")),
        "phase": str((status or {}).get("loginPhase") or ""),
        "phase_label": PHASE_LABELS.get(str((status or {}).get("loginPhase") or ""), ""),
        "qr_url": str((status or {}).get("qrcodeurl") or ""),
        "login_error": str((status or {}).get("loginError") or ""),
        "uin": "", "nick": "", "avatar": "",
    }
    if out["isLogin"]:
        info = logged_hint or webui.login_info() or {}
        self_info = info.get("selfInfo") if isinstance(info.get("selfInfo"), dict) else info
        out["uin"] = str(self_info.get("uin") or "")
        out["nick"] = str(self_info.get("nick") or "")
        out["avatar"] = str(self_info.get("avatarUrl") or "")
        if not out["avatar"]:
            # 有的 NapCat 版本 selfInfo 不带头像：从快速登录列表按 uin 补
            for acc in webui.quick_logins():
                if str(acc.get("uin") or "") == out["uin"]:
                    out["avatar"] = str(acc.get("faceUrl") or "")
                    break
    return out


def _healthz(proc: NapCatProc, webui: WebUIClient) -> Dict[str, Any]:
    """ServiceManager health_detail 原样透传的精简态（不认识业务键名也没关系）。"""
    bat = proc.bat_path()
    running = proc.running()
    status = webui.login_status()[0] if running else None
    return {
        "ok": True,
        "configured": bat is not None,
        "napcat_running": running,
        "login": bool((status or {}).get("isLogin")),
    }


# ── HTTP 服务 ────────────────────────────────────────────────────────────────

def make_handler(proc: NapCatProc, webui: WebUIClient) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "OmniAgentNapcatPage/1.0"

        # ── 基础 ──
        def _send(self, status: int, payload: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

        def _static(self, name: str) -> None:
            f = (PAGE_DIR / name).resolve()
            if not f.is_file() or PAGE_DIR.resolve() not in f.parents:
                self._send(404, b"not found", "text/plain")
                return
            ctype = "text/html" if f.suffix == ".html" else "text/plain"
            self._send(200, f.read_bytes(), ctype + "; charset=utf-8")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
                return data if isinstance(data, dict) else {}
            except Exception:  # noqa: BLE001
                return {}

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默：状态走 /api
            pass

        # ── GET ──
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                self._json(200, _healthz(proc, webui))
                return
            if path in ("/", "/index.html"):
                self._static("index.html")
                return
            if path == "/api/status":
                self._json(200, {
                    "ok": True,
                    "process": _proc_status(proc),
                    "onebot_http": port_open(proc.onebot_port()),
                    "webui_reachable": webui.login_status()[0] is not None,
                    "login": _login_status(webui),
                    "quick_logins": webui.quick_logins(),
                })
                return
            if path == "/api/qr.png":
                self._qr_png()
                return
            if path == "/api/logs/stream":
                self._logs_stream()
                return
            if path == "/api/config":
                status, payload, ctype = _proxy_config("GET")
                self._send(status, payload, str(ctype or "application/json"))
                return
            self._send(404, b'{"ok":false,"error":"not allowed"}')

        def _qr_png(self) -> None:
            """当前二维码内容 → 服务端渲染 PNG（二维码内容不出本进程）。"""
            status, err = webui.login_status()
            url = str((status or {}).get("qrcodeurl") or "")
            if not url:
                self._json(404, {"ok": False, "error": err or "当前没有可用二维码"})
                return
            try:
                import segno

                buf = io.BytesIO()
                segno.make(url).save(buf, kind="png", scale=6, border=2)
                self._send(200, buf.getvalue(), "image/png")
            except Exception as e:  # noqa: BLE001
                self._json(500, {"ok": False, "error": f"二维码渲染失败: {e}"})

        def _logs_stream(self) -> None:
            """SSE：回放环形缓冲窗口，再 0.5s 尾推实时行。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            seq = 0
            last_beat = time.time()
            try:
                while True:
                    lines, seq_now = proc.ring.since(seq)
                    for s, text in lines:
                        self.wfile.write(f"data: {text}\n\n".encode("utf-8"))
                    if seq_now > seq:
                        seq = seq_now
                        self.wfile.flush()
                    elif time.time() - last_beat > 15.0:  # 心跳防代理断连
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        last_beat = time.time()
                    time.sleep(0.5)
            except (ConnectionAbortedError, BrokenPipeError, OSError):
                return  # 客户端走人

        # ── POST ──
        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            body = self._body()
            if path == "/api/napcat/start":
                self._json(200, proc.spawn())
                return
            if path == "/api/napcat/stop":
                self._json(200, proc.stop())
                return
            if path == "/api/napcat/restart":
                self._json(200, proc.restart())
                return
            if path == "/api/login/refresh":
                ok, err = webui.refresh_qrcode()
                self._json(200, {"ok": ok, "error": err})
                return
            if path == "/api/login/quick":
                uin = str(body.get("uin") or "").strip()
                if not uin:
                    self._json(400, {"ok": False, "error": "需要 uin"})
                    return
                ok, err = webui.quick_login(uin)
                self._json(200, {"ok": ok, "error": err})
                return
            self._send(404, b'{"ok":false,"error":"not allowed"}')

        def do_PUT(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/config":
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                status, resp, ctype = _proxy_config("PUT", payload)
                self._send(status, resp, str(ctype or "application/json"))
                return
            self._send(404, b'{"ok":false,"error":"not allowed"}')

    return Handler


# ── 入口 ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="QQ 登录托管页插页服务（插件自带）")
    ap.add_argument("--port", type=int, required=True, help="监听端口（由宿主生命周期注入）")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    if args.port <= 0:
        print("[page_server] 端口未注入（manifest lifecycle.spawn.port 为空）", file=sys.stderr)
        return 2

    ring = LogRing()
    proc = NapCatProc(load_config, ring)
    webui = WebUIClient(load_config)

    ring.append(f"[托管] page_server 已启动 (pid={os.getpid()}, port={args.port})")
    if proc.bat_path() is None:
        ring.append("[托管] 未配置 napcat_home，不自动拉起 NapCat（见 examples/plugins/napcat.yaml）")
    else:
        result = proc.spawn()  # 配置齐了就自动拉起；已在跑则幂等跳过
        ring.append(f"[托管] 自动拉起结果: {result.get('result')}")

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(proc, webui))
    print(f"[page_server] listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
