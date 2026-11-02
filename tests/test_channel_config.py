"""通道配置独立化：~/.omniagent/channels/<name>.yaml（每通道一文件）+ routes.yaml。

- channel_config / _channel_names / _load_routes：与 plugin_config 同构的加载器；
- build_adapter / start_channels 改读新位置（主配置不再有 channels:/chat_routes: 段）。
背景：通道连接信息（token/ws_url）是部署事实，不该和 brain/runtime 等 core 配置
挤同一份主配置；独立成文件也与将来「通道插件化」共用同一套布局。
"""
import pytest

from omni_core.local import runtime_paths as P


# === 加载器 ===

def test_channel_config_reads_per_channel_file(_isolated_home):
    from gateway import channel_gateway as gw

    P.channel_config_file("napcat").write_text(
        "enabled: true\nrequire_at: false\n", encoding="utf-8")
    assert gw.channel_config("napcat") == {"enabled": True, "require_at": False}


def test_channel_config_missing_or_broken_returns_empty(_isolated_home):
    from gateway import channel_gateway as gw

    assert gw.channel_config("nope") == {}
    P.channel_config_file("bad").write_text("a: [1, 2\n", encoding="utf-8")
    assert gw.channel_config("bad") == {}


def test_channel_names_excludes_routes(_isolated_home):
    from gateway import channel_gateway as gw

    P.ensure_global_dirs()
    for name in ("napcat", "feishu"):
        P.channel_config_file(name).write_text("enabled: true\n", encoding="utf-8")
    P.channel_routes_file().write_text('"x:*":\n  project_id: p\n', encoding="utf-8")
    assert gw._channel_names() == ["feishu", "napcat"]   # 排序稳定；routes.yaml 不是通道


def test_load_routes(_isolated_home):
    from gateway import channel_gateway as gw

    assert gw._load_routes() == {}
    P.channel_routes_file().write_text(
        '"napcat:*":\n  project_id: qq\n', encoding="utf-8")
    assert gw._load_routes() == {"napcat:*": {"project_id": "qq"}}


# === 网关装配走新位置 ===

def test_build_adapter_reads_channel_file(monkeypatch, _isolated_home):
    from gateway import channel_gateway as gw

    monkeypatch.setattr(gw, "_register_factories", lambda: None)
    captured = {}

    def factory(cfg):
        captured["cfg"] = cfg
        return object()

    monkeypatch.setitem(gw._ADAPTER_FACTORIES, "napcat", factory)
    P.channel_config_file("napcat").write_text("enabled: true\n", encoding="utf-8")
    gw.build_adapter("napcat")
    assert captured["cfg"] == {"enabled": True}
    assert gw.build_adapter("unknown") is None          # 未知通道 fail-closed


def test_start_channels_enumerates_files(monkeypatch, _isolated_home):
    from gateway import channel_gateway as gw

    monkeypatch.setattr(gw, "_register_factories", lambda: None)
    built = []

    class Dummy:
        name = "napcat"

        def connect(self, cb):
            built.append("connect")

    monkeypatch.setitem(gw._ADAPTER_FACTORIES, "napcat", lambda cfg: Dummy())
    monkeypatch.setattr(gw.context, "register", lambda a: None)
    P.ensure_global_dirs()
    P.channel_config_file("napcat").write_text("enabled: true\n", encoding="utf-8")
    P.channel_routes_file().write_text('"x:*":\n  project_id: p\n', encoding="utf-8")
    assert gw.start_channels() == 1                     # routes.yaml 不当通道拉起
    assert built == ["connect"]
