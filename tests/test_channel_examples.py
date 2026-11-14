"""通道模板不漂移：每个 events 插件在 examples/plugins/ 都有同名模板。

旧 ``examples/channels/`` 只保留 ``routes.yaml``——通道配置与插件配置是同一份，
模板再放回 ``channels/`` 会让用户重新走上「旧路径 + manifest 覆盖 enabled」的坑。
"""
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
CHANNEL_PLUGINS = ("bilibili", "feishu", "napcat", "wecom", "xiaohongshu")
SECRET_KEYS = {"token", "sessdata", "bili_jct", "dedeuserid", "api_key", "secret"}


def _load(name: str) -> dict:
    return yaml.safe_load((EXAMPLES / "plugins" / f"{name}.yaml").read_text(encoding="utf-8")) or {}


def test_every_channel_has_plugin_example():
    for name in CHANNEL_PLUGINS:
        path = EXAMPLES / "plugins" / f"{name}.yaml"
        assert path.is_file(), f"缺少 examples/plugins/{name}.yaml"
        doc = _load(name)
        assert isinstance(doc.get("enabled"), bool), f"{name}.yaml 的 enabled 必须是布尔值"


def test_old_channel_dir_only_has_routes():
    files = {p.name for p in (EXAMPLES / "channels").iterdir() if p.is_file()}
    assert files == {"routes.yaml"}, (
        "examples/channels/ 只应保留 routes.yaml；通道模板请放 examples/plugins/<name>.yaml"
    )


def test_channel_examples_have_no_secrets_or_machine_paths():
    for name in CHANNEL_PLUGINS:
        path = EXAMPLES / "plugins" / f"{name}.yaml"
        text = path.read_text(encoding="utf-8")
        doc = _load(name)
        for key in SECRET_KEYS & set(doc):
            assert doc[key] == "", f"{path.name} 的 {key} 必须留空"
        assert "C:\\Users" not in text and "/home/" not in text, "示例不能带本机绝对路径"
