"""插件配置（阶段 4 收口）：``~/.omniagent/plugins/<name>.yaml`` 单源。

- channel_config / _channel_names / _load_routes：与 plugin_config 同构的加载器；
- build_adapter / start_channels 全走 manifest discover + registry；
- 旧 ``channels/<name>.yaml`` 只由迁移脚本处理，运行时不回落、不读取。
"""
import pytest

from omni_core.local import runtime_paths as P


def _legacy_config_file(name: str):
    """测试专用：旧路径只在这里构造，生产运行时不认识它。"""
    return P.channels_root() / f"{name}.yaml"


# === 加载器 ===

def test_channel_config_reads_per_channel_file(_isolated_home):
    from harness import channel_gateway as gw

    P.ensure_global_dirs()
    P.plugin_config_file("napcat").write_text(
        "enabled: true\nrequire_at: false\n", encoding="utf-8")
    assert gw.channel_config("napcat") == {"enabled": True, "require_at": False}


def test_channel_config_prefers_plugin_file(_isolated_home):
    """新路径是唯一事实源；旧路径即使还在也不会被读取。"""
    from harness import channel_gateway as gw

    P.ensure_global_dirs()
    P.plugin_config_file("napcat").write_text("require_at: true\n", encoding="utf-8")
    _legacy_config_file("napcat").write_text("require_at: false\n", encoding="utf-8")
    assert gw.channel_config("napcat")["require_at"] is True


def test_channel_config_ignores_legacy_file(_isolated_home):
    """旧路径只剩迁移材料：运行时不再回落，也不会把 enabled:false 改成 true。"""
    from harness import channel_gateway as gw
    from harness.events import discover

    P.ensure_global_dirs()
    _legacy_config_file("napcat").write_text(
        "enabled: false\nrequire_at: false\n", encoding="utf-8")
    assert gw.channel_config("napcat") == {}
    meta = discover.manifests()["napcat"]
    assert meta["configured"] is False
    assert meta["enabled"] is True                  # manifest 缺省；旧 enabled 不参与


def test_migrate_script_preserves_enabled(_isolated_home):
    """迁移脚本：字段原样合并，旧 enabled:false 必须保留；新路径同名键优先。"""
    import importlib.util
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "migrate_plugin_configs", repo / "scripts" / "migrate_plugin_configs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    P.ensure_global_dirs()
    _legacy_config_file("napcat").write_text(
        "enabled: false\nrequire_at: false\nhttp_url: http://127.0.0.1:3000\n",
        encoding="utf-8")
    P.plugin_config_file("napcat").write_text("timeout_sec: 10\n", encoding="utf-8")
    item = mod.plan_one("napcat")
    assert "enabled: false" in item["after"]
    assert "require_at: false" in item["after"]
    assert "timeout_sec: 10" in item["after"]        # 既有插件配置不丢
    assert "http://127.0.0.1:3000" in item["after"]

    # 新路径已明确写了 enabled 时以新值为准，避免迁移覆盖用户当前选择。
    P.plugin_config_file("napcat").write_text(
        "enabled: true\ntimeout_sec: 10\n", encoding="utf-8")
    assert "enabled: true" in mod.plan_one("napcat")["after"]


def test_channel_config_missing_or_broken_returns_empty(_isolated_home):
    from harness import channel_gateway as gw

    assert gw.channel_config("nope") == {}
    P.plugin_config_file("bad").write_text("a: [1, 2\n", encoding="utf-8")
    assert gw.channel_config("bad") == {}


def test_channel_names_are_discovered_events_units(_isolated_home):
    """「通道」= manifest 声明了 events 的插件：不再扫 channels/*.yaml。"""
    from harness import channel_gateway as gw

    P.ensure_global_dirs()
    P.channel_routes_file().write_text('"x:*":\n  project_id: p\n', encoding="utf-8")
    assert gw._channel_names() == ["bilibili", "feishu", "napcat", "wecom", "xiaohongshu"]


def test_load_routes(_isolated_home):
    from harness import channel_gateway as gw

    assert gw._load_routes() == {}
    P.channel_routes_file().write_text(
        '"napcat:*":\n  project_id: qq\n', encoding="utf-8")
    assert gw._load_routes() == {"napcat:*": {"project_id": "qq"}}


# === 网关装配走新位置 ===

def test_build_adapter_reads_manifest_factory(monkeypatch, _isolated_home):
    """build_adapter = discover 清单 + registry 构造；未知/未配置/未启用一律 None。"""
    from harness import channel_gateway as gw

    captured = {}

    def fake_build(name, cfg, factory):
        captured["args"] = (name, cfg, factory)
        return object()

    monkeypatch.setattr("harness.events.registry.build", fake_build)
    meta = {"enabled": True, "configured": True, "events": {"factory": "channel:build"}}
    monkeypatch.setattr(gw, "_discovered", lambda: {"napcat": meta})
    P.plugin_config_file("napcat").write_text("enabled: true\n", encoding="utf-8")
    gw.build_adapter("napcat")
    assert captured["args"] == ("napcat", {"enabled": True}, "channel:build")
    assert gw.build_adapter("unknown") is None          # 未知通道 fail-closed


def test_start_channels_bootstraps_discovered_units(monkeypatch, _isolated_home):
    """start_channels：discover → bootstrap → connect → lifecycle.bind；未配置跳过。"""
    from harness import channel_gateway as gw
    from harness.events import lifecycle

    lifecycle.reset()
    built = []

    class Dummy:
        name = "napcat"

        def connect(self, cb):
            built.append("connect")

    monkeypatch.setattr("harness.events.registry.bootstrap",
                        lambda names, provider, disc: {"napcat": {
                            "adapter": Dummy(), "state": "built", "error": ""}})
    monkeypatch.setattr(gw, "_discovered", lambda: {
        "napcat": {"enabled": True, "configured": True,
                   "events": {"transport": "pull", "factory": "channel:build"}},
    })
    monkeypatch.setattr(gw.context, "register", lambda a: None)
    monkeypatch.setattr(lifecycle, "write_snapshot", lambda path=None: None)
    P.ensure_global_dirs()
    P.channel_routes_file().write_text('"x:*":\n  project_id: p\n', encoding="utf-8")
    assert gw.start_channels() == 1                     # routes.yaml 不当通道拉起
    assert built == ["connect"]
    assert lifecycle.snapshot()[0]["state"] == "connected"


def test_channel_client_ignores_enabled_gate(monkeypatch):
    """只读客户端不受 enabled 门限：通道未自动拉起时插件工具仍可用。"""
    from plugins.bilibili import channel as bilibili
    from plugins.xiaohongshu import channel as xiaohongshu

    class Dummy:
        def __init__(self, cfg):
            self.cfg = cfg

    monkeypatch.setattr(bilibili, "BilibiliAdapter", Dummy)
    monkeypatch.setattr(xiaohongshu, "XhsAdapter", Dummy)
    cfg = {"enabled": False}
    assert bilibili.build(cfg) is None
    assert xiaohongshu.build(cfg) is None
    assert isinstance(bilibili.client(cfg), Dummy)
    assert isinstance(xiaohongshu.client(cfg), Dummy)


def test_channel_client_resolves_by_name(monkeypatch):
    """宿主门面按名解析通道的 client(cfg)；非法名/未知名 fail-closed。"""
    from harness import channel_gateway as gw
    from plugins.bilibili import channel as bilibili

    assert gw.channel_client("") is None
    assert gw.channel_client("not/valid") is None
    assert gw.channel_client("no_such_channel") is None
    monkeypatch.setattr(gw, "channel_config", lambda name: {"from": name})
    monkeypatch.setattr(bilibili, "client", lambda cfg: cfg)
    assert gw.channel_client("bilibili") == {"from": "bilibili"}
