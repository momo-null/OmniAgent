"""插件配置 API（schema 驱动）单测：类型校验 / 点分键 / 密钥不回传 / 服务配置分离。

宿主不认识任何具体键名——这里用假插件 manifest，验证的是「按 schema 通用读写」
与两个 scope（插件目录 vs manifest 声明的独立服务文件），不是某个插件的字段。
"""
import os
import sys

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backend.api.routers.tools_api as tools_api  # noqa: E402
import backend.server as server_mod  # noqa: E402

PLUGIN_FIELDS = [
    {"key": "provider", "label": "来源", "type": "select",
     "options": [{"value": "a"}, {"value": "b"}]},
    {"key": "nested.key", "label": "密钥", "type": "password", "secret": True},
    {"key": "num", "label": "数", "type": "number", "min": 0, "max": 10, "default": 3},
    {"key": "flag", "label": "开关", "type": "bool", "default": False},
]

SERVICE_FIELDS = [
    {"key": "port", "label": "端口", "type": "number", "min": 1024, "max": 65535},
    {"key": "voice", "label": "音色", "type": "text", "default": "duorou"},
    {"key": "token", "label": "令牌", "type": "password", "secret": True},
]


@pytest.fixture
def client():
    with TestClient(server_mod.app) as c:
        yield c


@pytest.fixture
def fake_plugin(tmp_path, monkeypatch):
    """manifest / 配置路径全部指向临时对象，绝不碰用户 ~/.omniagent。"""
    import omni_core.local.runtime_paths as runtime_paths
    import omni_core.tools.loader as loader

    manifest = {
        "name": "fake",
        "config": PLUGIN_FIELDS,
        "service_config": {
            "path": str(tmp_path / "fake_service.yaml"),
            "fields": SERVICE_FIELDS,
        },
    }
    monkeypatch.setattr(
        loader, "plugin_manifest",
        lambda name, cfg=None: manifest if name == "fake" else {})
    monkeypatch.setattr(runtime_paths, "plugin_config_file",
                        lambda name: tmp_path / f"{name}.yaml")
    return tmp_path


# ── 纯函数 ────────────────────────────────────────────────

def test_coerce_by_type():
    assert tools_api._coerce({"key": "b", "type": "bool"}, True) is True
    with pytest.raises(ValueError):
        tools_api._coerce({"key": "b", "type": "bool"}, "true")
    assert tools_api._coerce({"key": "n", "type": "number", "min": 0, "max": 10}, 5) == 5
    assert tools_api._coerce({"key": "n", "type": "number"}, 5.5) == 5.5
    with pytest.raises(ValueError):
        tools_api._coerce({"key": "n", "type": "number", "max": 10}, 11)
    with pytest.raises(ValueError):
        tools_api._coerce({"key": "s", "type": "select",
                           "options": [{"value": "x"}]}, "y")
    with pytest.raises(ValueError):
        tools_api._coerce({"key": "t", "type": "text"}, 123)


def test_dotted_path_roundtrip():
    data: dict = {}
    tools_api._put_dig(data, "openai.base_url", "http://x")
    assert data == {"openai": {"base_url": "http://x"}}
    assert tools_api._dig(data, "openai.base_url") == "http://x"
    assert tools_api._dig(data, "openai.missing", "d") == "d"


# ── 端点：插件 scope ──────────────────────────────────────

def test_get_unknown_plugin_404(client, fake_plugin):
    r = client.get("/api/runtime/tools/plugins/nope/config")
    assert r.status_code == 404


def test_get_masks_secret(client, fake_plugin):
    (fake_plugin / "fake.yaml").write_text(
        yaml.safe_dump({"nested": {"key": "plain-secret"}}), encoding="utf-8")
    r = client.get("/api/runtime/tools/plugins/fake/config")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "plugin"
    assert body["values"]["nested.key"] == ""          # 明文永不出站
    assert body["secrets_set"]["nested.key"] is True
    assert body["values"]["num"] == 3                  # schema 默认值兜底


def test_put_validates_and_writes(client, fake_plugin):
    r = client.put("/api/runtime/tools/plugins/fake/config",
                   json={"values": {"provider": "a", "nested.key": "s", "num": 7}})
    assert r.status_code == 200
    doc = yaml.safe_load((fake_plugin / "fake.yaml").read_text(encoding="utf-8"))
    assert doc["provider"] == "a"
    assert doc["nested"]["key"] == "s"                 # 点分键写成嵌套
    assert doc["num"] == 7


def test_put_rejects_unknown_and_invalid(client, fake_plugin):
    assert client.put("/api/runtime/tools/plugins/fake/config",
                      json={"values": {"nope": 1}}).status_code == 400
    assert client.put("/api/runtime/tools/plugins/fake/config",
                      json={"values": {"provider": "zzz"}}).status_code == 400
    assert client.put("/api/runtime/tools/plugins/fake/config",
                      json={"values": {"num": 99}}).status_code == 400


def test_put_empty_secret_keeps_existing(client, fake_plugin):
    (fake_plugin / "fake.yaml").write_text(
        yaml.safe_dump({"nested": {"key": "keep-me"}}), encoding="utf-8")
    r = client.put("/api/runtime/tools/plugins/fake/config",
                   json={"values": {"nested.key": "", "num": 2}})
    assert r.status_code == 200
    doc = yaml.safe_load((fake_plugin / "fake.yaml").read_text(encoding="utf-8"))
    assert doc["nested"]["key"] == "keep-me"
    assert doc["num"] == 2


# ── 端点：service scope（服务配置独立成文件） ─────────────

def test_service_scope_uses_declared_file(client, fake_plugin):
    r = client.get("/api/runtime/tools/plugins/fake/config?scope=service")
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "service"
    assert body["path"].endswith("fake_service.yaml")
    assert [f["key"] for f in body["schema"]] == ["port", "voice", "token"]
    assert body["values"]["voice"] == "duorou"          # 默认值

    r = client.put("/api/runtime/tools/plugins/fake/config?scope=service",
                   json={"values": {"port": 19000, "voice": "x"}})
    assert r.status_code == 200
    svc = yaml.safe_load((fake_plugin / "fake_service.yaml").read_text(encoding="utf-8"))
    assert svc == {"port": 19000, "voice": "x"}
    assert not (fake_plugin / "fake.yaml").exists(), "服务配置不能写进插件配置"


def test_service_scope_undeclared_400(client, fake_plugin, monkeypatch):
    import omni_core.tools.loader as loader

    monkeypatch.setattr(loader, "plugin_manifest",
                        lambda name, cfg=None: {"name": "fake", "config": PLUGIN_FIELDS})
    r = client.get("/api/runtime/tools/plugins/fake/config?scope=service")
    assert r.status_code == 400
    assert "service" in r.json()["error"]
