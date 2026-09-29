"""测试模型路由层（omni_core.brain.router）：目录解析、白名单、落盘。

不依赖真实 ~/.omniagent/models.json：monkeypatch 指向临时文件；目录是端点
唯一真源，config 里残留什么端点键都不参与解析。
"""
import json

import pytest

import config
from omni_core.brain import router


# --- 夹具 -------------------------------------------------------------------
# config 里故意残留旧通道端点键：证明目录之外的一切都不参与解析
_CONFIG_CFG = {
    "brain": {
        "base_url": "https://legacy/v1",
        "model": "legacy-main",
        "api_key": "sk-legacy",
    },
    "runtime": {"executor": {"base_url": "http://127.0.0.1:8085", "model": "legacy-worker"}},
}

_CATALOG = {
    "providers": {
        "online": {
            "label": "在线",
            "base_url": "https://example/v1",
            "api_key": "sk-online",
            "models": [{"id": "m1", "label": "M1"}, {"id": "m2", "label": ""}],
        },
        "local": {
            "label": "本机",
            "base_url": "http://127.0.0.1:8085",
            "models": [{"id": "qwen", "label": "", "vision": True}],
        },
    },
    "defaults": {},
}


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """把模型目录指向临时文件，并固定 config 加载结果。"""
    path = tmp_path / "models.json"
    path.write_text(json.dumps(_CATALOG), encoding="utf-8")
    monkeypatch.setattr(config, "MODELS_CONFIG_PATH", str(path))
    monkeypatch.setattr(config, "load_config", lambda: dict(_CONFIG_CFG))
    config.reload_config()
    yield path
    config.reload_config()


# --- selection 解析与白名单 -------------------------------------------------
def test_parse_selection_ok(catalog):
    assert router.parse_selection("online/m1") == ("online", "m1")


@pytest.mark.parametrize("sel", ["", None, "online", "online/nope", "nope/m1", "/m1", "a b/c"])
def test_parse_selection_rejects_unknown(catalog, sel):
    """目录外的选择一律拒绝（防请求体注入任意端点）。"""
    assert router.parse_selection(sel) is None


# --- 解析优先级 -------------------------------------------------------------
def test_resolve_selection_overrides_default(catalog):
    ep = router.resolve_slot("main", "local/qwen")
    assert ep["model"] == "qwen"
    assert ep["base_url"] == "http://127.0.0.1:8085"
    assert ep["capabilities"]["vision"] is True
    assert ep["model_provider_id"] == "local"


def test_resolve_default_from_catalog(catalog):
    router.set_default("main", "online/m1")
    ep = router.resolve_slot("main")
    assert ep["model"] == "m1" and ep["base_url"] == "https://example/v1"


def test_resolve_empty_slot_means_disabled(catalog):
    """真源收敛：目录没有选择 = 该用途未启用，不再回退旧配置。"""
    assert router.resolve_slot("main") == {}
    assert router.resolve_slot("worker") == {}


def test_resolve_invalid_selection_ignored(catalog):
    """非法 selection 被静默忽略，不报错、不落到任意端点。"""
    assert router.resolve_slot("main", "evil/https://attacker") == {}


def test_local_endpoint_gets_dummy_key(catalog):
    """本机端点无 key 时补占位（与 executor 既有处理一致）。"""
    ep = router.resolve_slot("main", "local/qwen")
    assert ep["api_key"] == "dummy"


# --- 目录视图 ---------------------------------------------------------------
def test_list_models_groups_and_masks_key(catalog):
    data = router.list_models()
    ids = [p["id"] for p in data["providers"]]
    assert ids == ["online", "local"]
    online = data["providers"][0]
    assert online["api_key_set"] is True
    assert "sk-online" not in json.dumps(data, ensure_ascii=False)
    assert [m["id"] for m in online["models"]] == ["m1", "m2"]
    # 目录未选 = source none，config 里残留的端点键不展示、不参与解析
    assert data["current"]["main"]["source"] == "none"
    assert data["current"]["main"]["model"] == ""


def test_list_models_reports_selected(catalog):
    router.set_default("main", "online/m2")
    data = router.list_models()
    cur = data["current"]["main"]
    assert cur["selection"] == "online/m2"
    assert cur["model"] == "m2"
    assert cur["source"] == "catalog"


# --- 落盘 -------------------------------------------------------------------
def test_save_providers_keeps_key_when_blank(catalog):
    """前端拿到的是脱敏视图，回传空 api_key 不应清空已存密钥。"""
    router.save_providers({
        "online": {"label": "在线", "base_url": "https://example/v1", "api_key": "",
                   "models": [{"id": "m1"}]},
    })
    saved = json.loads(catalog.read_text(encoding="utf-8"))
    assert saved["providers"]["online"]["api_key"] == "sk-online"


def test_save_providers_drops_stale_defaults(catalog):
    router.set_default("main", "online/m1")
    router.save_providers({"local": {"base_url": "http://127.0.0.1:8085",
                                     "models": [{"id": "qwen"}]}})
    saved = json.loads(catalog.read_text(encoding="utf-8"))
    assert saved["defaults"] == {}
    assert list(saved["providers"]) == ["local"]


def test_set_default_rejects_unknown(catalog):
    with pytest.raises(ValueError):
        router.set_default("main", "nope/x")
    with pytest.raises(ValueError):
        router.set_default("bogus-slot", "online/m1")


def test_set_default_empty_clears_selection(catalog):
    router.set_default("main", "online/m1")
    router.set_default("main", "")
    assert router.current_selection("main") is None


def test_list_models_reports_none_when_unset(catalog):
    """目录未选 = source none（旧配置遗留不再展示）。"""
    data = router.list_models()
    assert data["current"]["main"]["source"] == "none"
    assert data["current"]["main"]["model"] == ""
