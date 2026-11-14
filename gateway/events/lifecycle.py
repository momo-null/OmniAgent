"""事件源生命周期（宿主侧）：autostart / 体检 / 状态聚合。

执行侧不写第二份代码：插件 manifest 声明 ``lifecycle.spawn`` 时直接映射给
``model_hub.services.ServiceManager``（进程层唯一实现，见计划 §0.6-A1）；本模块只做
三件事——把 manifest 投影成状态、按声明机械体检、把状态聚合成一份可查快照。

状态语义：
- ``connected``：适配器已连上（长连接/轮询都算）；
- ``disabled``：能力层关（``plugin.yaml`` > ``manifest.enabled``）；
- ``failed``：构造或连接抛错（附原因）；
- ``probe`` 是叠加信息：ready / not_ready / unknown / error，不改变 source 状态。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_SOURCES: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_LOOP_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()
#: 体检间隔（秒）；宿主只按这个节奏轮询，探针本身随插件声明走
HEALTH_INTERVAL = 60.0


def reset() -> None:
    """清空状态（测试用；进程内只该有一份）。"""
    with _LOCK:
        _SOURCES.clear()


def _record(name: str, manifest: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    m = manifest or {}
    events = m.get("events") or {}
    lifecycle = m.get("lifecycle") or {}
    rec = _SOURCES.get(name)
    if rec is None:
        rec = {"name": name, "state": "stopped", "error": "", "adapter": None,
               "transport": events.get("transport"),
               "autostart": bool(lifecycle.get("autostart", True)),
               "probe": "unknown", "checked_at": 0.0}
        _SOURCES[name] = rec
    else:
        rec["transport"] = events.get("transport") or rec.get("transport")
        rec["autostart"] = bool(lifecycle.get("autostart", rec.get("autostart", True)))
    rec["manifest"] = m
    return rec


def mark_starting(name: str, manifest: Optional[Dict[str, Any]] = None) -> None:
    with _LOCK:
        _record(name, manifest)["state"] = "starting"


def bind(name: str, adapter, manifest: Optional[Dict[str, Any]] = None) -> None:
    """适配器已连接：登记为 connected。"""
    with _LOCK:
        rec = _record(name, manifest)
        rec["adapter"] = adapter
        rec["state"] = "connected"
        rec["error"] = ""
        rec["checked_at"] = time.time()


def mark_disabled(name: str, error: str = "", manifest: Optional[Dict[str, Any]] = None) -> None:
    with _LOCK:
        rec = _record(name, manifest)
        rec["adapter"] = None
        rec["state"] = "disabled"
        rec["probe"] = "unknown"
        rec["error"] = str(error or "")[:300]


def mark_failed(name: str, error: str, manifest: Optional[Dict[str, Any]] = None) -> None:
    with _LOCK:
        rec = _record(name, manifest)
        rec["adapter"] = None
        rec["state"] = "failed"
        rec["error"] = str(error or "")[:300]
        rec["probe"] = "error"


def _probe_name(manifest: Optional[Dict[str, Any]]) -> str:
    """探针 = manifest 声明的方法名（宿主只做机械匹配，不内置任何平台词表）。"""
    health = ((manifest or {}).get("lifecycle") or {}).get("health") or {}
    if isinstance(health, str):  # 语法糖：health 直接写方法名
        return health.strip()
    return str(health.get("probe") or "").strip()


def probe(name: str) -> str:
    """按声明探活：ready / not_ready / unknown / error。"""
    with _LOCK:
        rec = _SOURCES.get(name)
        if rec is None:
            return "unknown"
        adapter = rec.get("adapter")
        manifest = rec.get("manifest")
    method = _probe_name(manifest)
    if not method or adapter is None:
        result = "unknown"
    else:
        fn = getattr(adapter, method, None)
        if not callable(fn):
            result = "unknown"
        else:
            try:
                result = "ready" if fn() else "not_ready"
            except Exception:  # noqa: BLE001 —— 探针异常只降级，不打断主循环
                result = "error"
    with _LOCK:
        rec = _SOURCES.get(name)
        if rec is not None:
            rec["probe"] = result
            rec["checked_at"] = time.time()
    return result


def snapshot() -> List[Dict[str, Any]]:
    """聚合状态（不含 adapter 对象，可直接 JSON 化）。"""
    with _LOCK:
        return [{
            "name": r["name"],
            "transport": r.get("transport"),
            "state": r.get("state"),
            "autostart": bool(r.get("autostart", True)),
            "probe": r.get("probe", "unknown"),
            "error": r.get("error", ""),
            "checked_at": r.get("checked_at", 0.0),
        } for r in sorted(_SOURCES.values(), key=lambda x: x["name"])]


def snapshot_path() -> Path:
    """``~/.omniagent/runtime/sources.json``（Web 只读这份快照）。"""
    try:
        from omni_core.local.runtime_paths import global_omni

        root = global_omni()
    except Exception:
        root = Path(os.path.expanduser("~")) / ".omniagent"
    return Path(root) / "runtime" / "sources.json"


def write_snapshot(path: Optional[Path] = None) -> None:
    p = Path(path or snapshot_path())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"updated_at": time.time(), "sources": snapshot()},
                          ensure_ascii=False, indent=2)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(p)  # 同盘 rename：读方永远看到完整 JSON
    except Exception:
        pass  # 快照是观测面，写失败不能影响网关主循环


def _health_loop(interval: float) -> None:
    while not _STOP.wait(max(1.0, interval)):
        with _LOCK:
            names = [n for n, r in _SOURCES.items() if r.get("state") == "connected"]
        for n in names:
            probe(n)
        write_snapshot()


def start_health_loop(interval: float = HEALTH_INTERVAL) -> None:
    """后台体检 + 落快照（幂等；daemon 线程随进程退出）。"""
    global _LOOP_THREAD
    if _LOOP_THREAD is not None and _LOOP_THREAD.is_alive():
        return
    _STOP.clear()
    _LOOP_THREAD = threading.Thread(target=_health_loop, args=(interval,),
                                    name="events-health", daemon=True)
    _LOOP_THREAD.start()


def stop_health_loop() -> None:
    _STOP.set()
    t = _LOOP_THREAD
    if t is not None and t.is_alive():
        t.join(timeout=2.0)


# ── 插件声明的常驻服务（lifecycle.spawn → ServiceManager） ─────────────────

def ensure_plugin_services(discovered: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """把 manifest ``lifecycle.spawn`` 投影成 ServiceManager 清单并拉起（幂等）。

    - 只处理能力层启用的插件（与事件源同一套 ``enabled`` 口径）；
    - ``autostart: false`` 的插件不在这里起，由它的控制面按需拉起；
    - 起不来只记录，不打断网关启动。
    """
    from . import discover as _discover

    table = discovered if discovered is not None else _discover.manifests()
    services: Dict[str, Any] = {}
    for name, meta in table.items():
        if not meta.get("enabled", True):
            continue
        lifecycle = meta.get("lifecycle") or {}
        spawn = lifecycle.get("spawn")
        if not isinstance(spawn, dict) or not spawn:
            continue
        if lifecycle.get("autostart") is False:
            continue
        spec = dict(spawn)
        spec.setdefault("autostart", True)
        spec.setdefault("enabled", True)
        services[name] = spec
    if not services:
        return []
    try:
        from model_hub.services import ServiceManager

        return ServiceManager({"services": services}).ensure_started()
    except Exception as e:  # noqa: BLE001
        return [{"name": "*", "result": "error", "error": str(e)}]


__all__ = [
    "reset", "mark_starting", "bind", "mark_disabled", "mark_failed", "probe",
    "snapshot", "snapshot_path", "write_snapshot",
    "start_health_loop", "stop_health_loop", "ensure_plugin_services",
]
