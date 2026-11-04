"""配置示例不漂移：例子必须能解析、覆盖 schema 所有键、且不含真实密钥/本机路径。

示例是给人抄的模板，最容易悄悄过期（加了新键忘了写例子，或把本机密钥抄进去）。
这里用 plugin.json 的 schema 当唯一真源做双向校验。
"""
import json
from pathlib import Path

import yaml

VOICE = Path(__file__).resolve().parents[1] / "plugins" / "voice"

# 引擎侧不在设置页暴露、但服务确实会读的键（唱歌相关）
SERVICE_EXTRA_KEYS = {"sing_online"}


def _manifest() -> dict:
    return json.loads((VOICE / "plugin.json").read_text(encoding="utf-8"))


def _load(name: str) -> dict:
    return yaml.safe_load((VOICE / name).read_text(encoding="utf-8")) or {}


def test_plugin_example_covers_schema():
    schema = {f["key"] for f in _manifest()["config"]}
    example = _load("plugin.example.yaml")
    missing = schema - set(example)
    assert not missing, f"plugin.example.yaml 缺少: {sorted(missing)}"
    assert set(example) - schema == {"enabled"}, "示例里有 schema 未声明的键"
    assert example["enabled"] is True


def test_service_example_covers_schema():
    node = _manifest()["service_config"]
    schema = {f["key"] for f in node["fields"]}
    example = _load("voice_service.example.yaml")
    missing = schema - set(example)
    assert not missing, f"voice_service.example.yaml 缺少: {sorted(missing)}"
    extra = set(example) - schema - SERVICE_EXTRA_KEYS
    assert not extra, f"示例里有 schema 未声明的键: {sorted(extra)}"
    assert node["path"] == "~/.omniagent/voice_service.yaml"


def test_examples_have_no_real_secrets_or_paths():
    for name in ("plugin.example.yaml", "voice_service.example.yaml"):
        text = (VOICE / name).read_text(encoding="utf-8")
        doc = _load(name)
        for key in ("api_key", "service_token"):
            if key in doc:
                assert doc[key] == "", f"{name} 的 {key} 必须留空"
        assert "C:\\Users" not in text and "/home/" not in text, "示例不能带本机绝对路径"

    svc = _load("voice_service.example.yaml")
    for key in ("stt_model_dir", "gptsovits_dir", "voices_dir", "gptsovits_repo",
                "tts_python", "tts_vits", "tts_t2s"):
        assert svc[key] == "", f"{key} 应留空走自动探测"
