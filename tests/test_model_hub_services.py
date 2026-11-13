"""model_hub 服务清单与统一加载器（资源层）。

只测「起不起」这一层：占位符解析、disabled/delegated 语义、端口占用视为已运行、
状态字段形状。core 的业务开关（用不用）不在此文件。
"""
import socket

from model_hub.services import ServiceManager


def _doc(**services):
    return {"services": services}


def _spec(**kw):
    base = {
        "enabled": True, "autostart": False, "python": "", "args": ["-m", "x"],
        "cwd": "{repo}", "port": 0, "health": "",
    }
    base.update(kw)
    return base


def test_command_resolves_placeholders():
    """{repo}/{models} 必须被替换；python 留空时回落到当前解释器。"""
    m = ServiceManager(_doc(s=_spec(args=["{repo}/scripts/x.py", "--dir", "{models}/m"])))
    cmd = m._command(m.spec("s"))
    joined = " ".join(cmd)
    assert "{repo}" not in joined and "{models}" not in joined
    assert cmd[1].replace("\\", "/").endswith("/scripts/x.py")
    assert cmd[0], "解释器不能为空"


def test_disabled_service_is_not_started():
    m = ServiceManager(_doc(s=_spec(enabled=False)))
    assert m.start("s")["result"] == "disabled"


def test_gguf_is_delegated_to_model_manager():
    """GGUF 走 llama-server（ModelManager），不在此重复实现。"""
    m = ServiceManager(_doc(s=_spec(kind="gguf")))
    assert m.start("s")["result"] == "delegated"


def test_port_in_use_means_already_running():
    """端口已占用 = 已有实例在跑，不抢端口（手动起的/上次没退干净）。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)  # 探测会连多次（start 一次、status 一次），backlog 留够
    port = srv.getsockname()[1]
    try:
        m = ServiceManager(_doc(s=_spec(port=port)))
        assert m.start("s")["result"] == "already"
        st = m.status("s")
        assert st["running"] is True and st["port"] == port
    finally:
        srv.close()


def test_status_shape():
    m = ServiceManager(_doc(s=_spec(port=0)))
    st = m.status("s")
    assert {"name", "enabled", "autostart", "running", "ready",
            "port", "pid", "kind"} <= set(st)


def test_unknown_service():
    m = ServiceManager(_doc())
    assert m.start("nope")["result"] == "unknown"


def test_autostart_only_starts_enabled_ones(monkeypatch):
    """ensure_started 只碰 enabled + autostart；其余跳过。"""
    m = ServiceManager(_doc(
        a=_spec(autostart=True),
        b=_spec(autostart=False),
        c=_spec(enabled=False, autostart=True),
    ))
    monkeypatch.setattr(m, "start", lambda n: {"name": n, "result": "started"})
    got = [r["name"] for r in m.ensure_started()]
    assert got == ["a"]
