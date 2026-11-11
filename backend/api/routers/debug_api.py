"""Debug 模块 · 可整体删除的调试/观测 API（与正式接口零耦合）。

本文件属于 Debug 模块：正式路由（/api/runtime/*）不感知本模块。
删除方式 = 删本文件 + ``debug_memory_data.py`` + server.py 里的挂载行 + 前端
``web/src/components/DebugPanel.tsx``、``web/src/components/debug/``、
``web/src/api/debugApi.ts``。

记忆可视化分两面：
- **全景**（/memory/landscape、atoms、l0、narrative、documents、document）：只读把
  L0–L3 全部记忆层、正文文件、圈层语境、巩固水位、写入队列摆给本机主人看；
- **监控**（/memory/metrics）：观测 jsonl 聚合 + 离线效果报告 + 轨迹四指标。
两者都不写记忆、不进注入面；纠正 / 遗忘复用 ``memory_brain`` 的 tombstone 软删原语。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.routers import debug_memory_data as data

router = APIRouter(prefix="/api/debug", tags=["debug"])


def _resp(fn, *args, **kwargs):
    try:
        return JSONResponse(fn(*args, **kwargs))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _body_int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except Exception:
        return default


# ------------------------------------------------------------------ 兼容旧入口

@router.get("/memory")
async def debug_memory(request: Request):
    """旧版记忆转储（保留兼容）：L1 atoms + overview。新版 UI 走全量分页端点。"""
    try:
        from omni_core import memory_brain as mb

        limit = _body_int(request.query_params.get("limit"), 200, 1, 5000)
        atoms = mb.dump_atoms(limit=limit)
        return JSONResponse({"ok": True, "atoms": atoms, "count": len(atoms),
                             "overview": mb.overview_brain()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/memory/delete")
async def debug_memory_delete(request: Request):
    """人工删除单条记忆（物理删除，不可恢复，agent 写入路径不经过这里）。"""
    try:
        from omni_core import memory_brain as mb

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        record_id = str((payload or {}).get("record_id") or "").strip()
        if not record_id:
            return JSONResponse({"ok": False, "error": "record_id 为空"}, status_code=422)
        if not mb.hard_delete(record_id):
            return JSONResponse({"ok": False, "error": "record_id 不存在"}, status_code=404)
        return JSONResponse({"ok": True, "record_id": record_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ------------------------------------------------------------------ 记忆全景

@router.get("/memory/landscape")
async def debug_memory_landscape():
    """全层总览：L0/L1/L2/L3 存量、FTS 健康、语境链、水位、队列、正文清单。"""
    from omni_core import memory_brain as mb

    return _resp(data.landscape, mb)


@router.get("/memory/atoms")
async def debug_memory_atoms(request: Request):
    """L1 全量分页（state/kind/q 过滤），不再受 dump_atoms(200) 上限约束。"""
    from omni_core import memory_brain as mb

    p = request.query_params
    return _resp(data.atoms_page, mb,
                 limit=_body_int(p.get("limit"), 100, 1, 500),
                 offset=_body_int(p.get("offset"), 0, 0, 1_000_000),
                 state=str(p.get("state") or "").strip(),
                 kind=str(p.get("kind") or "").strip(),
                 q=str(p.get("q") or "").strip())


@router.get("/memory/l0")
async def debug_memory_l0(request: Request):
    """L0 逐字检索副本分页（表是索引副本，真相在会话 jsonl 文件）。"""
    from omni_core import memory_brain as mb

    p = request.query_params
    return _resp(data.l0_page, mb,
                 limit=_body_int(p.get("limit"), 100, 1, 500),
                 offset=_body_int(p.get("offset"), 0, 0, 1_000_000),
                 q=str(p.get("q") or "").strip(),
                 context_key=str(p.get("context_key") or "").strip(),
                 source_kind=str(p.get("source_kind") or "").strip())


@router.get("/memory/narrative")
async def debug_memory_narrative(request: Request):
    """叙事索引条目（只给指针/摘要，不返回正文）。"""
    from omni_core import memory_brain as mb

    p = request.query_params
    return _resp(data.narrative_page, mb,
                 limit=_body_int(p.get("limit"), 200, 1, 1000),
                 kind=str(p.get("kind") or "").strip(),
                 q=str(p.get("q") or "").strip())


@router.get("/memory/documents")
async def debug_memory_documents():
    """所有可读记忆正文文件清单（文件是唯一真相）。"""
    from omni_core import memory_brain as mb

    return _resp(lambda: {"ok": True, "items": data.list_documents(mb)})


@router.get("/memory/document")
async def debug_memory_document(request: Request):
    """按白名单 key 读正文；key 形如 ``diary/diary-2026-11-01.md``。"""
    from omni_core import memory_brain as mb

    key = str(request.query_params.get("key") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "key 为空"}, status_code=422)
    return _resp(data.read_document, mb, key)


@router.delete("/memory/relation-card")
async def debug_memory_relation_card_delete(request: Request):
    """手动注销一张关系卡（admin 动作）：删正文 + 撤叙事索引；主人卡拒绝。

    用途：错卡/重复卡/想让她忘掉某人。冻结退役是默认（7 天窗外停更不删），
    这里只处理显式的人工决定。
    """
    from omni_core import memory_brain as mb

    uid = str(request.query_params.get("uid") or "").strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "uid 为空"}, status_code=422)
    if not mb.retire_relation_card(uid):
        return JSONResponse({"ok": False, "error": "删除失败（主人卡或卡不存在）"},
                            status_code=422)
    return JSONResponse({"ok": True, "uid": uid})


# ------------------------------------------------------------------ 监控指标

@router.get("/memory/metrics")
async def debug_memory_metrics():
    """观测 jsonl 聚合 + 离线效果报告 + 轨迹四指标 + 写入队列健康。"""
    from omni_core import memory_brain as mb

    return _resp(data.metrics, mb)


# ------------------------------------------------------------------ 主人控制面

@router.post("/memory/correct")
async def debug_memory_correct(request: Request):
    """纠正：旧条 tombstone + 新条入库，不改写历史、审计可溯。"""
    try:
        from omni_core import memory_brain as mb

        try:
            body = await request.json()
        except Exception:
            body = {}
        record_id = str((body or {}).get("record_id") or "").strip()
        new_text = str((body or {}).get("new_text") or "").strip()
        if not record_id or not new_text:
            return JSONResponse({"ok": False, "error": "record_id / new_text 不能为空"},
                                status_code=422)
        new_id = mb.update_memory(record_id, new_text)
        if not new_id:
            return JSONResponse({"ok": False, "error": "记录不存在或不可更正"}, status_code=404)
        return JSONResponse({"ok": True, "new_record_id": new_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/memory/forget")
async def debug_memory_forget(request: Request):
    """遗忘：state → tombstone（软删，保留内容与原因，不物理删）。"""
    try:
        from omni_core import memory_brain as mb

        try:
            body = await request.json()
        except Exception:
            body = {}
        record_id = str((body or {}).get("record_id") or "").strip()
        reason = str((body or {}).get("reason") or "owner-forget")[:160]
        if not record_id:
            return JSONResponse({"ok": False, "error": "record_id 为空"}, status_code=422)
        if not mb.forget(record_id, reason):
            return JSONResponse({"ok": False, "error": "记录不存在或已遗忘"}, status_code=404)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
