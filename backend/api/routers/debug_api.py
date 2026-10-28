"""Debug 模块 · 可整体删除的调试/观测 API（与正式接口零耦合）。

本文件属于 Debug 模块：正式路由（/api/runtime/*）不感知本模块。
删除方式 = 删本文件 + server.py 里的挂载行 + 前端
`web/src/components/DebugPanel.tsx` 与 `web/src/api/debugApi.ts`。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/memory")
async def debug_memory(request: Request):
    """记忆库只读转储（brain.db 单库）：全部 atoms（状态/强度全字段）+ 总览。

    只读，零副作用。``task_id``/``project_id`` 参数已无分区语义，保留仅为前端兼容。
    """
    try:
        from omni_core import memory_brain

        atoms = memory_brain.dump_atoms()
        return JSONResponse({"ok": True, "atoms": atoms, "count": len(atoms),
                             "overview": memory_brain.overview_brain()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/memory/delete")
async def debug_memory_delete(request: Request):
    """人工删除单条记忆（前端右键删除；物理删除，不可恢复）。

    body: ``{record_id}``。这是唯一物理删除通道，agent 写入路径不经过这里。
    """
    try:
        from omni_core import memory_brain
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        record_id = str((payload or {}).get("record_id") or "").strip()
        if not record_id:
            return JSONResponse({"ok": False, "error": "record_id 为空"}, status_code=422)
        if not memory_brain.hard_delete(record_id):
            return JSONResponse({"ok": False, "error": "record_id 不存在"}, status_code=404)
        return JSONResponse({"ok": True, "record_id": record_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
