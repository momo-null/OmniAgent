"""B 站插件页反代路径映射（阶段 6 回归）。"""
from pathlib import Path

from plugins.bilibili.page_server import backend_path

ROOT = Path(__file__).resolve().parents[1]


def test_outbox_path_mapping():
    assert backend_path("/api/outbox") == "/bilibili/outbox"
    assert backend_path("/api/outbox/2/approve") == "/bilibili/outbox/2/approve"
    assert backend_path("/api/outbox/3/discard") == "/bilibili/outbox/3/discard"


def test_outbox_path_allowlist():
    assert backend_path("/api/other") is None
    assert backend_path("/api/outboxfoo") is None
    assert backend_path("/api/plugins/ui") is None


def test_plugin_iframe_keeps_same_origin():
    src = (ROOT / "web" / "src" / "components" / "dock" / "PluginFrame.tsx").read_text(encoding="utf-8")
    assert 'allow-same-origin' in src
