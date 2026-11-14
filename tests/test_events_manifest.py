"""阶段 3：manifest 声明 + discover + lifecycle（宿主接管工厂）。

覆盖点（对齐计划 §4 阶段 3 验收）：
- 14 份 manifest 全部可发现，name == 目录名，requires.host 齐全；
- 「通道」= 声明了 events 的 5 个插件；xiaohongshu 工具源声明为 mcp；
- 能力层开关 plugin.yaml > manifest；缺配置的事件源不自动连；
- registry.bootstrap 失败记 failed 不 raise；lifecycle 探针按声明机械执行；
- lifecycle.spawn 直接映射到 ServiceManager（不造第二份起进程代码）。
"""
import json

import pytest

from harness.events import discover, lifecycle, registry


@pytest.fixture(autouse=True)
def _clean_lifecycle():
    lifecycle.reset()
    yield
    lifecycle.reset()


# === discover ===

def test_all_manifests_discovered_with_requires():
    table = discover.manifests()
    assert len(table) == 14                     # 11 份既有 + feishu/napcat/wecom 三份新通道
    for name, meta in table.items():
        assert meta["name"] == name             # name == 目录名（防拷错目录静默生效）
        assert "host" in meta["requires"], name
        assert isinstance(meta["requires"]["host"], list)


def test_events_units_are_the_five_channel_plugins():
    units = discover.events_units()
    assert units == ["bilibili", "feishu", "napcat", "wecom", "xiaohongshu"]
    table = discover.manifests()
    for name in units:
        ev = table[name]["events"]
        assert ev["transport"] == "pull"        # 第一版只认 pull/push
        assert ":" in ev["factory"]             # "<模块>:<函数>"


def test_xiaohongshu_declares_mcp_tool_source():
    table = discover.manifests()
    assert table["xiaohongshu"]["tools"]["source"] == "mcp"
    assert table["xiaohongshu"]["tools"]["server"] == "xiaohongshu"
    assert table["xiaohongshu"]["tools"]["include"] == [
        "list_feeds", "search_feeds", "get_feed_detail"]
    assert table["bilibili"]["tools"] is None   # 缺省 = local，按需显式声明


def test_undefined_faces_are_none(tmp_path):
    """非事件插件不带 events/lifecycle/ui；非法 transport 视为未声明。"""
    d = tmp_path / "demo"
    d.mkdir()
    (d / "plugin.json").write_text(json.dumps({
        "name": "demo", "enabled": True,
        "events": {"transport": "carrier-pigeon"},
        "ui": [{"id": "x", "label": "X", "slot": "pane", "url": "u"}],
    }), encoding="utf-8")
    table = discover.manifests(tmp_path)
    assert table["demo"]["events"] is None
    assert table["demo"]["ui"] == []            # slot 白名单只有 dock/tab


def test_manifest_bad_json_and_name_mismatch_skipped(tmp_path):
    a = tmp_path / "good"
    a.mkdir()
    (a / "plugin.json").write_text('{"name": "good"}', encoding="utf-8")
    b = tmp_path / "bad"
    b.mkdir()
    (b / "plugin.json").write_text("{not json", encoding="utf-8")
    c = tmp_path / "wrong"
    c.mkdir()
    (c / "plugin.json").write_text('{"name": "other"}', encoding="utf-8")
    table = discover.manifests(tmp_path)
    assert list(table) == ["good"]


def test_enabled_precedence_plugin_yaml_over_manifest(_isolated_home):
    """能力层开关：plugin.yaml > manifest.enabled（§0.6-A3）。"""
    from omni_core.local import runtime_paths as P

    P.ensure_global_dirs()
    P.plugin_config_file("napcat").write_text("enabled: false\n", encoding="utf-8")
    assert discover.manifests()["napcat"]["enabled"] is False
    P.plugin_config_file("napcat").write_text("require_at: false\n", encoding="utf-8")
    assert discover.manifests()["napcat"]["enabled"] is True


def test_unconfigured_events_unit_not_autostarted(_isolated_home):
    """没配置过的事件源只被发现、不自动连（等价旧「有 channels yaml 才启动」）。"""
    assert discover.manifests()["wecom"]["configured"] is False


# === registry.bootstrap ===

def test_bootstrap_marks_failed_and_skips(monkeypatch):
    """坏插件记 failed 并跳过，不影响同一批其它插件。"""
    table = {
        "boom": {"enabled": True, "events": {"factory": "channel:build"}},
        "ok": {"enabled": True, "events": {"factory": "channel:build"}},
        "off": {"enabled": False, "events": {"factory": "channel:build"}},
    }

    def fake_construct(name, cfg, factory):
        if name == "boom":
            raise ImportError("no such module")
        return f"adapter:{name}"

    monkeypatch.setattr(registry, "_construct", fake_construct)
    out = registry.bootstrap(["boom", "ok", "off"], lambda n: {}, table)
    assert out["boom"]["state"] == "failed"
    assert "no such module" in out["boom"]["error"]
    assert out["ok"]["state"] == "built"
    assert out["off"]["state"] == "disabled"


def test_build_resolves_factory_file_and_func(monkeypatch):
    calls = {}

    def fake_import(path):
        calls["path"] = path

        class M:
            @staticmethod
            def make(cfg):
                calls["cfg"] = cfg
                return "made"

        return M

    monkeypatch.setattr("importlib.import_module", fake_import)
    assert registry.build("demo", {"a": 1}, "alt:make") == "made"
    assert calls["path"] == "plugins.demo.alt"
    assert calls["cfg"] == {"a": 1}


def test_build_missing_name_returns_none():
    assert registry.build("") is None
    assert registry.build("not/a/name") is None


# === lifecycle ===

def test_lifecycle_states_and_snapshot():
    meta = {"events": {"transport": "pull"}, "lifecycle": {"autostart": True}}
    lifecycle.mark_starting("a", meta)
    assert lifecycle.snapshot()[0]["state"] == "starting"
    lifecycle.bind("a", object(), meta)
    lifecycle.mark_disabled("b", "未配置", {"events": {"transport": "pull"}})
    snap = {r["name"]: r for r in lifecycle.snapshot()}
    assert snap["a"]["state"] == "connected"
    assert snap["a"]["transport"] == "pull"
    assert snap["b"]["state"] == "disabled"
    assert snap["b"]["error"] == "未配置"


def test_probe_is_mechanical():
    """探针 = manifest 声明的方法名：ready / not_ready / unknown / error。"""
    meta = {"lifecycle": {"health": {"probe": "ready"}}}

    class Ready:
        def ready(self):
            return True

    class Broken:
        def ready(self):
            raise RuntimeError("boom")

    lifecycle.bind("r", Ready(), meta)
    lifecycle.bind("b", Broken(), meta)
    lifecycle.bind("n", object(), meta)          # 无该方法
    lifecycle.bind("d", Ready(), {})             # 未声明探针
    assert lifecycle.probe("r") == "ready"
    assert lifecycle.probe("b") == "error"
    assert lifecycle.probe("n") == "unknown"
    assert lifecycle.probe("d") == "unknown"
    assert lifecycle.probe("nope") == "unknown"


def test_health_string_shorthand():
    meta = {"lifecycle": {"health": "ready"}}

    class Ready:
        def ready(self):
            return False

    lifecycle.bind("x", Ready(), meta)
    assert lifecycle.probe("x") == "not_ready"


def test_write_snapshot_roundtrip(_isolated_home, tmp_path):
    path = tmp_path / "runtime" / "sources.json"
    lifecycle.bind("a", None, {"events": {"transport": "push"}})
    lifecycle.write_snapshot(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sources"][0]["name"] == "a"
    assert data["sources"][0]["state"] == "connected"


def test_plugin_services_reuse_service_manager(monkeypatch):
    """lifecycle.spawn → ServiceManager：不写第二份起进程代码（§0.6-A1）。"""
    captured = {}

    class FakeMgr:
        def __init__(self, doc):
            captured["doc"] = doc

        def ensure_started(self):
            return [{"name": "page", "result": "already"}]

    monkeypatch.setattr("model_hub.services.ServiceManager", FakeMgr)
    table = {
        "page": {"enabled": True, "lifecycle": {"autostart": True,
                 "spawn": {"args": ["-m", "plugins.page.server"], "port": 18799}}},
        "off": {"enabled": False, "lifecycle": {"spawn": {"port": 18800}}},
        "manual": {"enabled": True, "lifecycle": {"autostart": False,
                   "spawn": {"port": 18801}}},
    }
    out = lifecycle.ensure_plugin_services(table)
    assert out == [{"name": "page", "result": "already"}]
    services = captured["doc"]["services"]
    assert list(services) == ["page"]           # 关闭的与手动的不在清单里
    assert services["page"]["autostart"] is True
    assert services["page"]["enabled"] is True


def test_plugin_services_empty_when_no_spawn(monkeypatch):
    monkeypatch.setattr("model_hub.services.ServiceManager",
                        lambda doc: pytest.fail("不该构造 ServiceManager"))
    assert lifecycle.ensure_plugin_services({"a": {"enabled": True, "lifecycle": {"autostart": True}}}) == []
