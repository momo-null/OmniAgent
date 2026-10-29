"""PC-0 请求级鉴权测试：非回环来源访问 API 必须 Bearer token（loopback 豁免、
/health 与静态豁免、逃生门 allow_tokenless_lan、启动安全门）。

全部 monkeypatch，不起真实端口。TestClient 的 client.host="testclient" 恒为
非回环，正好充当「局域网设备」角色。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.server as server_mod  # noqa: E402
import config as config_mod  # noqa: E402


@pytest.fixture
def client():
    with TestClient(server_mod.app) as c:
        yield c


@pytest.fixture
def token_on(monkeypatch):
    monkeypatch.setattr(config_mod, "get_config",
                        lambda path="", default=None:
                        "s3cret-token" if path == "server.auth_token" else default)


class TestAuthMiddleware:
    def test_api_without_token_401(self, client, token_on):
        r = client.get("/api/runtime/tasks")
        assert r.status_code == 401

    def test_api_with_bearer_passes(self, client, token_on):
        r = client.get("/api/runtime/tasks", headers={"Authorization": "Bearer s3cret-token"})
        assert r.status_code != 401

    def test_api_wrong_token_401(self, client, token_on):
        r = client.get("/api/runtime/tasks", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_health_exempt(self, client, token_on):
        """手机端探活口：/health 不要求 token。"""
        r = client.get("/health")
        assert r.status_code != 401

    def test_static_exempt(self, client, token_on):
        """静态 UI 要能加载（API 另行拦截）。"""
        r = client.get("/")
        assert r.status_code != 401

    def test_no_token_configured_passes(self, client):
        """token 未配置 = 启动门只允许回环绑定，非回环请求只在测试场景可达 → 放行。"""
        r = client.get("/api/runtime/tasks")
        assert r.status_code != 401


class TestStartupGate:
    def test_loopback_never_raises(self):
        server_mod.HOST = "127.0.0.1"
        server_mod._require_auth_for_nonloopback()

    def test_nonloopback_without_token_exits(self):
        server_mod.HOST = "0.0.0.0"
        saved = dict(server_mod._server_cfg)
        server_mod._server_cfg = {}
        try:
            with pytest.raises(SystemExit):
                server_mod._require_auth_for_nonloopback()
        finally:
            server_mod._server_cfg = saved

    def test_nonloopback_with_token_passes(self):
        server_mod.HOST = "0.0.0.0"
        saved = dict(server_mod._server_cfg)
        server_mod._server_cfg = {"auth_token": "x"}
        try:
            server_mod._require_auth_for_nonloopback()
        finally:
            server_mod._server_cfg = saved

    def test_escape_hatch_allows_tokenless_lan(self):
        server_mod.HOST = "0.0.0.0"
        saved = dict(server_mod._server_cfg)
        server_mod._server_cfg = {"allow_tokenless_lan": True}
        try:
            server_mod._require_auth_for_nonloopback()
        finally:
            server_mod._server_cfg = saved
