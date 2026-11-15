"""QQ 登录托管页服务（plugins/napcat/page_server.py）离线测试。

不连真实 NapCat：进程托管用 fake napcat.bat（echo + 睡眠），WebUI 用假客户端
/假 HTTP 响应；验证配置推导、日志环形缓冲、启停收编、状态聚合与端点形状。
"""
import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from plugins.napcat import page_server as ps


# ── 测试工具 ─────────────────────────────────────────────────────────────────

FAKE_HOME_PORT = 39999  # 一定没开的端口，避免撞上本机真在跑的 NapCat


class FakeResp(io.BytesIO):
    """urlopen 返回值的上下文管理器替身（BytesIO 本身就够）。"""


def fake_cfg(tmp_path, **over):
    cfg = {
        "napcat_home": str(tmp_path),
        "webui_url": "http://127.0.0.1:39998",
        "http_url": f"http://127.0.0.1:{FAKE_HOME_PORT}",
    }
    cfg.update(over)
    return lambda: dict(cfg)


def write_fake_bat(tmp_path):
    """假 napcat.bat：打两行日志后睡眠（可被树杀）。"""
    bat = tmp_path / "napcat.bat"
    bat.write_text(
        "@echo off\r\n"
        "echo napcat-fake-boot\r\n"
        "echo napcat-fake-ready\r\n"
        "ping -n 60 127.0.0.1 >nul\r\n",
        encoding="ascii")
    return bat


def write_webui_json(tmp_path, token):
    d = tmp_path / "versions" / "9.9.9" / "resources" / "app" / "napcat" / "config"
    d.mkdir(parents=True, exist_ok=True)
    (d / "webui.json").write_text(json.dumps({"port": 6099, "token": token}),
                                  encoding="utf-8")


def wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def get(base, path):
    """GET 并容忍非 2xx（HTTPError 携带响应体，展开成同样的三元组）。"""
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            return int(r.status), r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read(), e.headers.get("Content-Type", "")


# === 配置推导 ===

def test_bat_path_missing_home():
    proc = ps.NapCatProc(fake_cfg("/nonexistent-home-xyz"), ps.LogRing())
    assert proc.bat_path() is None


def test_bat_path_found(tmp_path):
    write_fake_bat(tmp_path)
    proc = ps.NapCatProc(fake_cfg(tmp_path), ps.LogRing())
    assert proc.bat_path() == tmp_path / "napcat.bat"


def test_bat_path_absent_file(tmp_path):
    proc = ps.NapCatProc(fake_cfg(tmp_path), ps.LogRing())
    assert proc.bat_path() is None  # 目录在但没 bat = 等同未配置


def test_empty_home_means_unmanaged():
    proc = ps.NapCatProc(fake_cfg({}, napcat_home=""), ps.LogRing())
    assert proc.bat_path() is None


# === 日志环形缓冲 ===

def test_ring_since_semantics():
    ring = ps.LogRing()
    for i in range(5):
        ring.append(f"line-{i}")
    lines, seq = ring.since(0)
    assert [t for _, t in lines] == [f"line-{i}" for i in range(5)]
    lines, seq2 = ring.since(seq - 2)
    assert [t for _, t in lines] == ["line-3", "line-4"]
    assert seq2 == seq


def test_ring_cap_eviction():
    ring = ps.LogRing(cap=3)
    for i in range(6):
        ring.append(f"l{i}")
    lines, seq = ring.since(0)
    assert [t for _, t in lines] == ["l3", "l4", "l5"]
    assert seq == 6  # seq 只增：老客户端按 seq 续读不重不漏


# === 进程托管 ===

def test_spawn_not_configured():
    proc = ps.NapCatProc(fake_cfg({}, napcat_home=""), ps.LogRing())
    assert proc.spawn() == {"result": "not_configured"}


def test_spawn_read_and_stop_roundtrip(tmp_path):
    """拉起 fake bat → stdout 进环形缓冲 → 树杀干净。"""
    write_fake_bat(tmp_path)
    ring = ps.LogRing()
    proc = ps.NapCatProc(fake_cfg(tmp_path), ring)
    result = proc.spawn()
    assert result["result"] == "started"
    assert proc.managed_alive()
    assert wait_for(lambda: any("napcat-fake-ready" in t
                                for _, t in proc.ring.since(0)[0]))
    texts = [t for _, t in proc.ring.since(0)[0]]
    assert any("napcat-fake-boot" in t for t in texts)
    assert any("napcat-fake-ready" in t for t in texts)
    # 幂等：已在跑不重复拉
    assert proc.spawn()["result"] == "already"
    # 停止：cmd → bat → ping 整棵树
    assert proc.stop()["result"] == "stopped"
    assert wait_for(lambda: not proc.managed_alive(), timeout=5)


def test_stop_not_running(monkeypatch):
    proc = ps.NapCatProc(fake_cfg({}), ps.LogRing())
    monkeypatch.setattr(ps.NapCatProc, "_pid_on_port", staticmethod(lambda port: None))
    assert proc.stop() == {"result": "not_running"}


def test_external_instance_detected(monkeypatch):
    proc = ps.NapCatProc(fake_cfg({}), ps.LogRing())
    monkeypatch.setattr(ps, "port_open", lambda port, timeout=0.6: port == FAKE_HOME_PORT)
    assert proc.external_running() is True
    assert proc.running() is True


def test_reader_logs_exit_line(tmp_path):
    """子进程自己死掉也要在日志里留痕（页面能看到退出码）。"""
    bat = tmp_path / "napcat.bat"
    bat.write_text("@echo off\r\nexit /b 3\r\n", encoding="ascii")
    ring = ps.LogRing()
    proc = ps.NapCatProc(fake_cfg(tmp_path), ring)
    proc.spawn()
    assert wait_for(lambda: any("已退出" in t for _, t in ring.since(0)[0]))
    assert any("code=3" in t for _, t in ring.since(0)[0])
    assert proc._proc is None


# === WebUI 客户端 ===

def test_token_read_from_webui_json(tmp_path):
    webui = ps.WebUIClient(fake_cfg(tmp_path))
    assert webui._token() is None  # 没装 NapCat = None，不是异常
    write_webui_json(tmp_path, "tok-1")
    assert webui._token() == "tok-1"


def test_login_hash_matches_napcat_scheme(tmp_path, monkeypatch):
    """NapCat 口径：hash = sha256(token + '.napcat')，body 走 {hash}。"""
    write_webui_json(tmp_path, "tok-1")
    webui = ps.WebUIClient(fake_cfg(tmp_path))
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = req.data
        return FakeResp(json.dumps({"code": 0, "data": {"Credential": "cred-1"}}).encode())

    monkeypatch.setattr(ps.urllib.request, "urlopen", fake_urlopen)
    assert webui._login() == "cred-1"
    expect = hashlib.sha256(b"tok-1.napcat").hexdigest()
    assert json.loads(captured["body"]) == {"hash": expect}
    assert captured["url"].endswith("/api/auth/login")

    # 凭证已缓存，后续调用带 Bearer
    def fake_urlopen2(req, timeout=0):
        assert req.get_header("Authorization") == "Bearer cred-1"
        return FakeResp(json.dumps({"code": 0, "data": {"isLogin": True}}).encode())

    monkeypatch.setattr(ps.urllib.request, "urlopen", fake_urlopen2)
    ok, data, err = webui._post("QQLogin/CheckLoginStatus")
    assert ok and data == {"isLogin": True} and err == ""


def test_post_relogin_on_401(tmp_path, monkeypatch):
    """凭证失效（HTTP 401）→ 重登一次 → 重试成功。"""
    write_webui_json(tmp_path, "tok-1")
    webui = ps.WebUIClient(fake_cfg(tmp_path))
    webui._cred = "stale-cred"
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req.full_url)
        if req.full_url.endswith("/auth/login"):
            return FakeResp(json.dumps({"code": 0, "data": {"Credential": "fresh"}}).encode())
        if req.get_header("Authorization") == "Bearer stale-cred":
            raise urllib.error.HTTPError(req.full_url, 401, "expired", {}, io.BytesIO())
        return FakeResp(json.dumps({"code": 0, "data": {"fine": 1}}).encode())

    monkeypatch.setattr(ps.urllib.request, "urlopen", fake_urlopen)
    ok, data, err = webui._post("QQLogin/CheckLoginStatus")
    assert ok and data == {"fine": 1}
    assert calls.count("http://127.0.0.1:39998/api/QQLogin/CheckLoginStatus") == 2


def test_post_webui_down_is_soft_error(tmp_path):
    """WebUI 没起（最常见的观察期状态）= 软错误，不是异常。"""
    webui = ps.WebUIClient(fake_cfg(tmp_path))  # 没有 webui.json → token None
    ok, data, err = webui._post("QQLogin/CheckLoginStatus")
    assert ok is False and data is None and "webui 未就绪" in err


# === 状态聚合 ===

def test_login_status_phase_labels():
    webui = ps.WebUIClient(fake_cfg({}))
    webui.login_status = lambda: ({"isLogin": False, "loginPhase": "waiting_qrcode",
                                   "qrcodeurl": "https://ssl.ptlogin2.qq.com/qrlogin?x=1",
                                   "loginError": ""}, "")
    out = ps._login_status(webui)
    assert out["phase_label"] == "等待扫码"
    assert out["qr_url"].startswith("https://ssl.ptlogin2.qq.com/")


def test_login_status_logged_in_pulls_account():
    webui = ps.WebUIClient(fake_cfg({}))
    webui.login_status = lambda: ({"isLogin": True, "loginPhase": "initializing"}, "")
    webui.login_info = lambda: {"selfInfo": {"uin": "10001", "nick": "测试号",
                                             "avatarUrl": "http://q.qlogo.cn/x"}}
    out = ps._login_status(webui)
    assert out["uin"] == "10001" and out["nick"] == "测试号"
    assert out["avatar"] == "http://q.qlogo.cn/x"


def test_healthz_shape_when_unconfigured():
    proc = ps.NapCatProc(fake_cfg({}), ps.LogRing())  # 未配置 → 不碰 WebUI
    data = ps._healthz(proc, ps.WebUIClient(fake_cfg({})))
    assert data == {"ok": True, "configured": False, "napcat_running": False, "login": False}


# === HTTP 端点 ===

@pytest.fixture
def server(tmp_path):
    """真起一个 page_server（随机端口），proc/webui 全假。"""
    write_fake_bat(tmp_path)
    proc = ps.NapCatProc(fake_cfg(tmp_path), ps.LogRing())
    webui = ps.WebUIClient(fake_cfg(tmp_path))
    # WebUI 全假：不登录也算可达可查
    webui.login_status = lambda: ({"isLogin": False, "loginPhase": "waiting_qrcode",
                                   "qrcodeurl": "https://ssl.ptlogin2.qq.com/qrlogin?test=1",
                                   "loginError": "", "qrLoginAccepted": False}, "")
    webui.quick_logins = lambda: [{"uin": "10001", "nick": "测试号"}]
    webui.refresh_qrcode = lambda: (True, "")
    webui.quick_login = lambda uin: (True, "")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ps.make_handler(proc, webui))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", proc, webui
    httpd.shutdown()
    httpd.server_close()


def test_endpoint_healthz(server):
    base, _, _ = server
    status, body, ctype = get(base, "/healthz")
    data = json.loads(body)
    assert status == 200 and ctype.startswith("application/json")
    assert data["ok"] is True and data["configured"] is True


def test_endpoint_status_shape(server):
    base, _, _ = server
    _, body, _ = get(base, "/api/status")
    data = json.loads(body)
    assert data["ok"] is True
    assert data["process"]["configured"] is True
    assert data["onebot_http"] is False  # fake 端口没开
    assert data["webui_reachable"] is True
    assert data["login"]["phase_label"] == "等待扫码"
    assert data["quick_logins"][0]["uin"] == "10001"


def test_endpoint_qr_png(server):
    base, _, _ = server
    status, body, ctype = get(base, "/api/qr.png")
    assert status == 200 and ctype == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic


def test_endpoint_qr_missing_gives_json_404(server):
    base, _, webui = server
    webui.login_status = lambda: (None, "webui 未就绪（token 读不到或登录失败）")
    status, body, _ = get(base, "/api/qr.png")
    data = json.loads(body)
    assert status == 404 and data["ok"] is False and "webui" in data["error"]


def test_endpoint_static_index(server):
    base, _, _ = server
    status, body, ctype = get(base, "/")
    assert status == 200 and b"QQ" in body and "text/html" in ctype


def test_endpoint_unknown_path_404(server):
    base, _, _ = server
    status, body, _ = get(base, "/api/whatever")
    assert status == 404


def test_endpoint_post_actions(server):
    base, _, _ = server
    # refresh 透传到假 webui
    req = urllib.request.Request(base + "/api/login/refresh", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read())["ok"] is True
    # quick 缺 uin → 400
    req = urllib.request.Request(base + "/api/login/quick", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)
    assert excinfo.value.code == 400


def test_endpoint_log_stream_replays(server):
    """SSE 首帧回放环形缓冲已有行。"""
    base, proc, _ = server
    proc.ring.append("boot-line-alpha")
    with urllib.request.urlopen(base + "/api/logs/stream", timeout=5) as r:
        first = r.readline().decode("utf-8")
    assert first.startswith("data: ")
    assert "boot-line-alpha" in first


# === 配置编辑反代 ===

def test_config_proxy_forwards_get_and_put(server, monkeypatch):
    """/api/config 是唯一放行的宿主子树：GET 取 schema+values，PUT 整包转发。"""
    base, _, _ = server
    calls = []
    real_urlopen = ps.urllib.request.urlopen  # 测试自身的 HTTP 请求要走真身

    class FakeResp(io.BytesIO):
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        # 只截获 page_server → 主后端的反代调用；测试客户端访问本页服务的原样放行
        if isinstance(req, str) or not req.full_url.startswith(ps.BACKEND):
            return real_urlopen(req, timeout=timeout)
        calls.append((req.get_method(), req.full_url, req.data))
        payload = (b'{"ok":true,"schema":[],"values":{},"secrets_set":{}}'
                   if req.get_method() == "GET"
                   else b'{"ok":true}')
        return FakeResp(payload)

    monkeypatch.setattr(ps.urllib.request, "urlopen", fake_urlopen)
    status, body, _ = get(base, "/api/config")
    assert status == 200 and b'"schema"' in body
    req = urllib.request.Request(base + "/api/config", method="PUT",
                                 data=json.dumps({"values": {"timeout_sec": 15}}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert json.loads(r.read())["ok"] is True
    methods = [m for m, _, _ in calls]
    assert "GET" in methods and "PUT" in methods
    put_call = next(c for m, _, c in calls if m == "PUT")
    assert json.loads(put_call) == {"values": {"timeout_sec": 15}}
    assert all("/api/runtime/tools/plugins/napcat/config" in u for _, u, _ in calls)


def test_config_proxy_rejects_other_paths(server):
    base, _, _ = server
    status, body, _ = get(base, "/api/config/extra")
    assert status == 404


# === manifest 自检 ===

def _napcat_manifest() -> dict:
    return json.loads(
        (Path(__file__).resolve().parents[1] / "plugins" / "napcat" / "plugin.json")
        .read_text(encoding="utf-8"))


def test_manifest_declares_spawn_ui_probe():
    manifest = _napcat_manifest()
    spawn = manifest["lifecycle"]["spawn"]
    assert spawn["args"] == ["-m", "plugins.napcat.page_server"]
    assert spawn["port"] == 18753 and spawn["health"] == "/healthz"
    assert manifest["lifecycle"]["health"]["probe"] == "ready"
    ui = manifest["ui"][0]
    assert ui["slot"] == "dock" and ui["url"] == "http://127.0.0.1:18753/"


def test_manifest_config_schema_valid():
    """配置 schema 每项都有 key/label，type 在宿主词汇表内，secret 只有 token。"""
    manifest = _napcat_manifest()
    fields = manifest.get("config") or []
    assert fields, "配置可视化依赖 manifest config 数组"
    vocab = {"text", "password", "path", "number", "bool", "select", "list"}
    for f in fields:
        assert f.get("key") and f.get("label"), f
        assert f["type"] in vocab, f
        assert not (f.get("secret") and f["key"] != "token")
    keys = [f["key"] for f in fields]
    assert len(keys) == len(set(keys)), "schema 键不能重复"
    assert "stickers.enabled" in keys and "outbound.style" in keys  # 点分嵌套键


def test_manifest_config_defaults_match_code_fallbacks(monkeypatch):
    """schema 默认值 == 代码内部兜底值：页面原样保存一次不能悄悄改行为。"""
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    from plugins.napcat.channel import NapcatAdapter

    manifest = _napcat_manifest()
    defaults = {f["key"]: f.get("default") for f in manifest.get("config") or []}
    adapter = NapcatAdapter({})
    expect = {
        "http_url": adapter.http_url,
        "ws_url": adapter.ws_url,
        "token_env": "NAPCAT_TOKEN",
        "timeout_sec": adapter.timeout_sec,
        "reply_groups": adapter.reply_groups,
        "reply_private": adapter.reply_private,
        "reply_poke": adapter.reply_poke,
        "require_at": adapter.require_at,
        "allow_groups": sorted(adapter.allow_groups),
        "allow_users": sorted(adapter.allow_users),
        "default_chat_id": adapter.default_chat_id,
        "allow_tools": adapter.allow_tools,
        "images": adapter.images_enabled,
        "voice": adapter.voice_enabled,
        "voice_reply_ratio": adapter._voice_reply_ratio,
        "stickers.enabled": adapter.stickers_enabled,
        "stickers.debug_chat_id": adapter.sticker_debug_chat_id,
        "outbound.identity": "",
        "outbound.style": "",
    }
    for key, want in expect.items():
        got = defaults.get(key)
        if isinstance(want, float):
            got = float(got)
        assert got == want, f"schema 默认与代码兜底不一致: {key}"
    assert defaults.get("token") == ""            # token 走 secret，不出站
    assert defaults.get("stickers.dir") == ""     # 空 = 适配器默认贴纸库
    assert defaults.get("napcat_home") == ""      # 空 = 不托管


def test_manifest_webui_url_default_matches_page_server():
    """webui_url 的 schema 默认必须等于 page_server 的兜底，否则保存一次就漂移。"""
    manifest = _napcat_manifest()
    default = next(f.get("default") for f in manifest["config"]
                   if f["key"] == "webui_url")
    assert default == ps.WebUIClient(lambda: {})._base()
