"""manifest 发现：只读 ``plugins/*/plugin.json`` 与用户配置，零 import。

宿主不认识任何具体插件名——这里产出的清单就是宿主对全部插件唯一的认知来源。
输出是纯数据（dict），调用方（registry / lifecycle / UI 接口）各取所需字段。

发现规则（与 ``omni_core.tools.loader`` 一致）：
- 只认目录下有 ``plugin.json`` 的插件；
- ``manifest.name`` 必须等于目录名，否则跳过（防止拷错目录静默生效）；
- JSON 解析失败跳过（不 raise，坏插件不拖垮宿主）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "plugins"

#: 第一版 ui 容器只认这两个取值（见计划 §2.3）
UI_SLOTS = {"dock", "tab"}
#: events.transport 只认这两个取值
EVENT_TRANSPORTS = {"pull", "push"}


def plugin_root() -> Path:
    return _PLUGIN_ROOT


def _plugin_config_file(name: str) -> Optional[Path]:
    """用户侧 ``~/.omniagent/plugins/<name>.yaml``（读不到就 None）。"""
    try:
        from omni_core.local.runtime_paths import plugin_config_file

        return plugin_config_file(name)
    except Exception:
        return None


def _legacy_channel_file(name: str) -> Optional[Path]:
    """过渡期旧路径 ``~/.omniagent/channels/<name>.yaml``（阶段 4 合并后主要靠它判配置）。"""
    try:
        from omni_core.local.runtime_paths import channel_config_file

        return channel_config_file(name)
    except Exception:
        return None


def configured(name: str) -> bool:
    """用户是否配置过这个插件（plugins/<name>.yaml 或旧 channels/<name>.yaml 存在）。"""
    for p in (_plugin_config_file(name), _legacy_channel_file(name)):
        if p is not None and p.is_file():
            return True
    return False


def _read_yaml(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _enabled(name: str, manifest: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """能力层开关：``plugin.yaml enabled`` > ``manifest.enabled`` > True（§0.6-A3）。

    进程层（资源层服务）不在这里判定——那是 ``model_hub.yaml`` 的事，两层正交。
    """
    if isinstance(cfg, dict) and "enabled" in cfg:
        return bool(cfg.get("enabled"))
    return bool(manifest.get("enabled", True))


def _normalize(name: str, manifest: Dict[str, Any], cfg: Dict[str, Any],
               config_file: Optional[Path]) -> Dict[str, Any]:
    events = manifest.get("events") if isinstance(manifest.get("events"), dict) else None
    if events is not None:
        transport = str(events.get("transport") or "").strip().lower()
        # 非法的 transport 等于该面不存在：宿主不猜，按「没声明」处理
        if transport not in EVENT_TRANSPORTS:
            events = None
        else:
            events = dict(events, transport=transport)
            if transport == "pull":
                events.setdefault("factory", "channel:build")
    tools = manifest.get("tools") if isinstance(manifest.get("tools"), dict) else None
    if tools is not None:
        source = str(tools.get("source") or "local").strip().lower()
        tools = dict(tools, source=source) if source in {"local", "mcp"} else None
    lifecycle = manifest.get("lifecycle") if isinstance(manifest.get("lifecycle"), dict) else None
    ui = manifest.get("ui")
    if isinstance(ui, list):
        ui = [dict(v) for v in ui
              if isinstance(v, dict) and str(v.get("slot") or "") in UI_SLOTS and v.get("url")]
    else:
        ui = []
    requires = manifest.get("requires") if isinstance(manifest.get("requires"), dict) else {}
    return {
        "name": name,
        "version": manifest.get("version"),
        "enabled": _enabled(name, manifest, cfg),
        # 没配置过的事件源不自动连（旧语义：channels/*.yaml 存在才启动，等价迁移）
        "configured": configured(name),
        "events": events,
        "tools": tools,
        "lifecycle": lifecycle,
        "ui": ui,
        "requires": dict(requires),
        "config_file": str(config_file) if config_file else None,
        "manifest": manifest,
    }


def manifests(root: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """扫描全部插件，返回 ``{name: 归一后的清单}``（按名字排序）。"""
    root = Path(root or _PLUGIN_ROOT)
    out: Dict[str, Dict[str, Any]] = {}
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith(".") or d.name.startswith("_"):
            continue
        mf = d / "plugin.json"
        if not mf.is_file():
            continue
        try:
            doc = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict) or str(doc.get("name") or "") != d.name:
            continue
        out[d.name] = _normalize(d.name, doc, _read_yaml(_plugin_config_file(d.name)), mf)
    return out


def events_units(root: Optional[Path] = None) -> List[str]:
    """声明了 events 面的插件名（= 旧「通道」，宿主不再有第三张表）。"""
    return [n for n, m in manifests(root).items() if m.get("events")]


def ui_units(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """UI 注入项：每项带插件名前缀，供 Web 拼页签。"""
    items: List[Dict[str, Any]] = []
    for name, m in manifests(root).items():
        for v in m.get("ui") or []:
            items.append({
                "plugin": name,
                "id": str(v.get("id") or ""),
                "label": str(v.get("label") or name),
                "icon": str(v.get("icon") or ""),
                "slot": str(v.get("slot") or "dock"),
                "url": str(v.get("url") or ""),
                "enabled": bool(m.get("enabled")),
            })
    return items
