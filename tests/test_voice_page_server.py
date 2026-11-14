"""语音插件页反代路径映射（参照 B 站出站审批页）。"""
from pathlib import Path

from plugins.voice.page_server import backend_path

ROOT = Path(__file__).resolve().parents[1]


def test_voice_page_path_mapping():
    assert backend_path("/api/config") == "/api/runtime/tools/plugins/voice/config?scope=plugin"
    assert backend_path("/api/voices") == "/api/models/services"
    assert backend_path("/api/tts") == "/api/audio/tts"


def test_voice_page_path_allowlist():
    assert backend_path("/api/other") is None
    assert backend_path("/api/config/extra") is None
    assert backend_path("/api/plugins/ui") is None


def test_voice_page_is_declared_as_dock_view():
    import json

    manifest = json.loads((ROOT / "plugins" / "voice" / "plugin.json").read_text(encoding="utf-8"))
    ui = manifest.get("ui") or []
    assert ui and ui[0]["slot"] == "dock"
    assert ui[0]["url"].endswith(":18752/")
