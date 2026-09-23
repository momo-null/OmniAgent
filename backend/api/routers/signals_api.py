"""信号与稳态指标接口（零逻辑改动）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.routers.helpers import (
    _paths,
)

router = APIRouter(tags=["runtime"])

@router.get("/signals")
async def list_signals(task_id: str = ""):
    """某任务各 run 的三实体一致率 + 标签。task_id 经通用标识符校验。"""
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    from omni_core.local import signals as _sig
    return JSONResponse({"task_id": task_id, "signals": _sig.query_signals(task_id)})

@router.get("/signals/summary")
async def signals_summary():
    """聚合：假成功率（分模式）、漏报率、分歧率、不可判占比、C₁/C₂ 分布、校准误差。"""
    from omni_core.local import signals as _sig
    return JSONResponse(_sig.query_summary())

@router.get("/signals/steady")
async def signals_steady():
    """K5 四信号 + 域收敛状态。"""
    from omni_core.local import steady_state as _ss
    return JSONResponse(_ss.evaluate_steady())
