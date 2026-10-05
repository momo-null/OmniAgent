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
    """记忆库只读转储（查看蒸馏结果）：项目库 + 全局库 atoms，各自按更新时间倒序。

    归属解析：优先 ``project_id`` 参数；否则按 ``task_id`` 解析所属项目；
    都没有 → ``default``。只读，零副作用。
    """
    try:
        from omni_core import memory_tam
        from omni_core.local.task_store import TaskStore

        q = request.query_params
        task_id = (q.get("task_id") or "").strip()
        project_id = (q.get("project_id") or "").strip()
        if not project_id:
            if task_id:
                try:
                    project_id = TaskStore.project_of(task_id) or "default"
                except Exception:
                    project_id = "default"
            else:
                project_id = "default"
        state = memory_tam.dump_state(project_id)
        atoms = state.get("atoms", [])
        gatoms = state.get("global_atoms", [])
        return JSONResponse({
            "ok": True,
            "project_id": project_id,
            "project": {"atoms": atoms},
            "global": {"atoms": gatoms},
            "counts": {"project": len(atoms), "global": len(gatoms)},
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
