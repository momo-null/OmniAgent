"""全局长期记忆（memory/）REST 接口（零逻辑改动）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.routers.helpers import (
    _memory_enabled,
    _paths,
    _read_memory_master,
    _read_memory_summary_chars,
    _read_merged_ids,
    _read_rollout_detail,
    _read_rollouts_list,
)

router = APIRouter(tags=["runtime"])

@router.get("/memory")
async def get_memory():
    """记忆聚合只读视图：master 全文 + summary 字符数 + rollouts/已合并计数 + 注入开关。"""
    try:
        rollouts_total = _read_rollouts_list(limit=0).get("total", 0)
        merged_total = len(_read_merged_ids())
        return JSONResponse({
            "master": _read_memory_master(),
            "summary_chars": _read_memory_summary_chars(),
            "rollouts_total": rollouts_total,
            "merged_total": merged_total,
            "enabled": _memory_enabled(),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@router.get("/memory/rollouts")
async def list_memory_rollouts(limit: int = 50, offset: int = 0):
    """记忆回放列表（倒序），支持分页 ?limit=&offset=。"""
    if limit < 0 or limit > 500:
        limit = 50
    if offset < 0:
        offset = 0
    data = _read_rollouts_list(limit=limit, offset=offset)
    return JSONResponse({"rollouts": data["rollouts"], "total": data["total"]})

@router.get("/memory/rollouts/{task_id}")
async def get_memory_rollout(task_id: str):
    """单条 rollout 全文（含 trajectory 引用）。task_id 经通用标识符校验。"""
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    detail = _read_rollout_detail(task_id)
    if detail is None:
        return JSONResponse({"ok": False, "error": "rollout not found"}, status_code=404)
    return JSONResponse(detail)

@router.put("/memory")
async def put_memory(request: Request):
    """K1 用户直写唯一入口：覆盖 MEMORY.md，写盘后服务端重生成 summary。

    不校验内容（人可手改是定案）；审计留痕由调用方/落盘负责。复用 Curator 截断常量。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    master = (body or {}).get("master")
    if not isinstance(master, str):
        return JSONResponse({"ok": False, "error": "master 必须是字符串"}, status_code=422)
    try:
        from omni_core.local import runtime_paths as P
        from omni_core.local.curator import regenerate_summary
        p = P.memory_master()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(master, encoding="utf-8")
        # 服务端重生成注入视图（截断管控），下一轮注入即反映人工编辑
        chars = regenerate_summary()
        return JSONResponse({"ok": True, "summary_chars": chars, "master_chars": len(master)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@router.delete("/memory")
async def reset_memory(confirm: str = ""):
    """重置：清空 memory/ 全部产物（MEMORY.md / summary / rollouts / merged.json）。

    缺 confirm=reset 返回 400。严禁触及 task 级目录（world_model 等任务私有资产）。
    """
    if confirm != "reset":
        return JSONResponse({"ok": False, "error": "需带 ?confirm=reset 确认"}, status_code=400)
    try:
        from omni_core.local import runtime_paths as P
        import shutil
        mem = P.global_memory()
        removed = 0
        for name in ("MEMORY.md", "memory_summary.md", "merged.json"):
            fp = mem / name
            if fp.exists():
                try:
                    fp.unlink()
                    removed += 1
                except Exception:
                    pass
        rd = P.memory_rollouts()
        if rd.is_dir():
            for f in rd.glob("*.md"):
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
        return JSONResponse({"ok": True, "removed": removed})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

@router.delete("/memory/rollouts/{task_id}")
async def delete_memory_rollout(task_id: str):
    """删除单条 rollout（不回滚已合并进 MEMORY.md 的内容）。task_id 经通用标识符校验。"""
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    try:
        from omni_core.local import runtime_paths as P
        p = P.memory_rollout_file(task_id)
        if not p.exists():
            return JSONResponse({"ok": False, "error": "rollout not found"}, status_code=404)
        p.unlink()
        return JSONResponse({"ok": True, "task_id": task_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
