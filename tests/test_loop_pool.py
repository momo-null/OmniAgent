"""loop 组件池（M1 常驻化）单测。

覆盖：MCP 跨 run 复用 / 配置变更重建 + 旧连接清理、共享 AsyncOpenAI 缓存、
健康探测 TTL、工具清单缓存、池不可用时的降级。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core.local.loop import pool  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_pool():
    """每个用例从空池开始，结束时再清一次（避免状态穿透到别的用例）。"""
    pool.clear()
    yield
    pool.clear()


def test_mcp_servers_reused_across_runs(monkeypatch):
    """同一份 mcp 配置 → 只构建一次，跨 run 拿到同一批对象。

    这是 M1 的核心收益：实测 connect 约 950ms/轮，复用后 2.8ms。
    """
    built = []

    def fake_build(servers, log=None):
        obj = {"name": "srv"}
        built.append(obj)
        return [obj]

    monkeypatch.setattr("omni_core.tools.build_mcp_servers", fake_build)
    cfg = {"enabled": True, "servers": [{"name": "x", "command": "python"}]}

    a = pool.mcp_servers(cfg)
    b = pool.mcp_servers(cfg)
    assert len(built) == 1, "同配置不应重建 server 对象"
    assert a and a[0] is b[0]
    assert pool.stats()["mcp_hit"] >= 1


def test_mcp_rebuild_and_cleanup_on_config_change(monkeypatch):
    """配置变更 → 重建，并主动清理旧连接（不泄漏子进程）。"""
    cleaned = []

    def fake_build(servers, log=None):
        return [{"name": str((servers or [{}])[0].get("name"))}]

    monkeypatch.setattr("omni_core.tools.build_mcp_servers", fake_build)
    monkeypatch.setattr(pool, "_cleanup_servers", lambda s: cleaned.extend(s))

    first = pool.mcp_servers({"enabled": True, "servers": [{"name": "a"}]})
    second = pool.mcp_servers({"enabled": True, "servers": [{"name": "b"}]})
    assert first[0]["name"] == "a" and second[0]["name"] == "b"
    assert cleaned and cleaned[0]["name"] == "a", "旧连接必须被清理"


def test_mcp_disabled_returns_empty(monkeypatch):
    called = []
    monkeypatch.setattr("omni_core.tools.build_mcp_servers",
                        lambda *a, **k: called.append(1) or [{}])
    assert pool.mcp_servers({"enabled": False, "servers": [{"name": "x"}]}) == []
    assert pool.mcp_servers({}) == []
    assert called == [], "未启用时不应构造 server"


def test_mcp_build_failure_degrades_to_empty(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("omni_core.tools.build_mcp_servers", boom)
    assert pool.mcp_servers({"enabled": True, "servers": [{"name": "x"}]}) == []


def test_shared_openai_cached_by_endpoint(monkeypatch):
    """同端点/同 key 复用一个 AsyncOpenAI（= 复用 httpx 连接池）；key 变了要新建。"""
    created = []

    class _FakeClient:
        def __init__(self, **kw):
            created.append(kw)

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeClient)
    cfg = {"base_url": "http://127.0.0.1:8080/v1", "api_key": "k1"}

    a = pool.shared_openai(cfg)
    b = pool.shared_openai(dict(cfg))
    assert a is not None and a is b and len(created) == 1

    c = pool.shared_openai({**cfg, "api_key": "k2"})
    assert c is not a and len(created) == 2


def test_shared_openai_falls_back_when_key_missing(monkeypatch):
    monkeypatch.setenv("OMNI_BRAIN_API_KEY", "")
    assert pool.shared_openai({"base_url": "http://127.0.0.1:1/v1"}) is None
    assert pool.shared_openai({}) is None


def test_health_ok_cached_within_ttl(monkeypatch):
    """run 路径不再每轮同步探测：TTL 内只探一次（超时可显式绕过）。"""
    calls = []

    def fake_health(base_url, timeout=2.0):
        calls.append(base_url)
        return False

    monkeypatch.setattr("omni_core.brain.llm.model_health_ok", fake_health)
    url = "http://127.0.0.1:8765/v1"
    assert pool.health_ok(url) is False
    assert pool.health_ok(url) is False
    assert len(calls) == 1, "TTL 内应命中缓存"

    assert pool.health_ok(url, ttl=0) is False
    assert len(calls) == 2, "ttl=0 必须重新探测"


def test_health_ok_skips_online_endpoint(monkeypatch):
    """在线 provider 不做预检（与 model_health_ok 同口径）。"""
    calls = []
    monkeypatch.setattr("omni_core.brain.llm.model_health_ok",
                        lambda *a, **k: calls.append(1) or True)
    assert pool.health_ok("https://api.example.com/v1") is None
    assert pool.health_ok("") is None
    assert calls == []


def test_sdk_tools_cached_by_tool_names():
    """工具清单按名字集合缓存：同集合命中，集合变了重建。"""

    class _Reg:
        def __init__(self, names):
            self._names = names
            self.built = 0

        def names(self):
            return list(self._names)

        def sdk_tools(self):
            self.built += 1
            return [{"name": n} for n in self._names]

    reg = _Reg(["a", "b"])
    first = pool.sdk_tools(reg)
    second = pool.sdk_tools(reg)
    assert first == second and reg.built == 1
    assert pool.stats()["tools_hit"] >= 1

    changed = _Reg(["a", "c"])
    third = pool.sdk_tools(changed)
    assert [t["name"] for t in third] == ["a", "c"]
    assert changed.built == 1


def test_prime_mcp_warms_connection_at_startup(monkeypatch):
    """启动期预热：后台线程把 MCP 连好，首个 run 不再付 1s 连接税。"""
    import time as _t

    class _FakeServer:
        name = "x"
        session = object()          # 非空 = 已连接（_ensure_mcp_connected 直接复用）

        async def list_tools(self):
            return []

    monkeypatch.setattr("omni_core.tools.build_mcp_servers",
                        lambda *a, **k: [_FakeServer()])
    pool.prime_mcp({"enabled": True, "servers": [{"name": "x"}]})
    deadline = _t.time() + 5.0
    while _t.time() < deadline and pool.stats()["mcp_build"] == 0:
        _t.sleep(0.02)
    assert pool.stats()["mcp_build"] == 1, "预热线程应完成一次构建"
    got = pool.mcp_servers({"enabled": True, "servers": [{"name": "x"}]})
    assert got and got[0].session is not None


def test_clear_resets_everything(monkeypatch):
    cleaned = []
    monkeypatch.setattr("omni_core.tools.build_mcp_servers", lambda *a, **k: [{"name": "x"}])
    monkeypatch.setattr(pool, "_cleanup_servers", lambda s: cleaned.extend(s))
    cfg = {"enabled": True, "servers": [{"name": "x"}]}
    pool.mcp_servers(cfg)
    pool.clear()
    assert pool.stats()["mcp_hit"] == 0
    assert cleaned, "clear() 必须断开 MCP 连接"
    assert pool.mcp_servers(cfg) and pool.stats()["mcp_build"] == 1
