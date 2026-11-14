"""插件注入面 API：UI 声明 + 事件源状态（前端零平台名）。

- ``GET /plugins/ui``：manifest 的 ``ui`` 投影（插件名/页签标题/图标名/容器/URL）；
  前端只按 ``slot`` 选容器（第一版 dock/tab），不解析页面语义。
- ``GET /plugins/sources``：网关落盘的事件源快照（``~/.omniagent/runtime/sources.json``）。
  读不到 = 网关没跑过 → 返回空表 + 原因，而不是编一份假状态。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


@router.get("/ui")
async def ui_views() -> Dict[str, Any]:
    from gateway.events import discover

    views: List[Dict[str, Any]] = discover.ui_units()
    return {"views": views}


@router.get("/sources")
async def sources() -> Dict[str, Any]:
    from gateway.events import lifecycle

    path = lifecycle.snapshot_path()
    if not path.is_file():
        return {"updated_at": 0, "sources": [],
                "note": "网关未运行或尚未落盘（启动网关后自动出现）"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 —— 快照坏了不等于系统坏了
        return {"updated_at": 0, "sources": [], "note": f"快照读取失败: {type(e).__name__}"}
    if not isinstance(data, dict):
        return {"updated_at": 0, "sources": [], "note": "快照结构非法"}
    return data
