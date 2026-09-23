"""快照与实时观测接口：/snapshot /skills（零逻辑改动）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.routers.helpers import (
    _paths,
    _read_skills,
    _snapshot,
)

router = APIRouter(tags=["runtime"])

@router.get("/snapshot")
async def snapshot(task_id: str = "", project_id: str = ""):
    paths = _paths()
    if task_id:
        paths.validate_identifier(task_id, "task_id")
    return JSONResponse(_snapshot(task_id))

@router.get("/skills")
async def get_skills(task_id: str = ""):
    paths = _paths()
    if task_id:
        paths.validate_identifier(task_id, "task_id")
    return JSONResponse({"skills": _read_skills(task_id), "task_id": task_id})
