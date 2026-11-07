"""主人可见 / 可控 / 可遗忘（H-6 P6-3）：记忆的本地人工入口。

「AI Never Forgets」缺口的最小控制面：主人能看到她记了什么、能纠正、能让她忘掉。
边界（plan §16.3）：只在本机（backend 默认只绑本机回环），不新增远端；查看是
只读聚合，纠正 / 遗忘复用 ``omni_core.memory_brain`` 的既有 tombstone 软删原语——
不物理删、不改写 ``content``、``superseded_by`` 留痕审计可溯（§7.4 运行时数据口径）。
观测记录（memory_recall-*.jsonl）是量尺不是记忆，**不在本入口暴露**（§8.3）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/memory", tags=["runtime"])


@router.get("/search")
async def search(query: str = "", limit: int = 10):
    """查看（只读）：事实原子检索——与 ``memory_recall`` 同一检索面，
    多带 ``record_id`` 供纠正 / 遗忘操作定位。"""
    try:
        from omni_core import memory_brain as mb
        q = str(query or "").strip()
        if not q:
            return JSONResponse({"ok": True, "hits": []})
        try:
            k = max(1, min(20, int(limit)))
        except Exception:
            k = 10
        return JSONResponse({"ok": True, "hits": mb.search_brain(q, k=k, reinforce=False)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/narrative")
async def narrative(query: str = "", kind: str = "", limit: int = 8):
    """查看（只读）：叙事条目检索（只返回条目，不返回正文——正文按需读）。"""
    try:
        from omni_core import memory_brain as mb
        try:
            n = max(1, min(20, int(limit)))
        except Exception:
            n = 8
        rows = mb.search_narrative(str(query or ""), kind=str(kind or ""), limit=n)
        return JSONResponse({"ok": True, "items": rows})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/narrative/read")
async def narrative_read(doc_id: str, kind: str = ""):
    """查看（只读）：叙事全文（日记 / 月记 / 关系卡；正文文件是唯一真相）。"""
    try:
        from omni_core import memory_brain as mb
        r = mb.read_narrative(str(doc_id or ""), str(kind or ""))
        if not r.get("ok"):
            return JSONResponse(r, status_code=404)
        return JSONResponse(r)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/atoms/{record_id}/correct")
async def correct_atom(record_id: str, request: Request):
    """纠正：旧条 ``tombstone(superseded_by=新条)``，新条入库——不改写历史，审计可溯。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    new_text = (body or {}).get("new_text")
    if not isinstance(new_text, str) or not new_text.strip():
        return JSONResponse({"ok": False, "error": "new_text 必须是非空字符串"}, status_code=422)
    try:
        from omni_core import memory_brain as mb
        new_id = mb.update_memory(str(record_id or ""), new_text.strip())
        if not new_id:
            return JSONResponse({"ok": False, "error": "记录不存在或不可更正"}, status_code=404)
        return JSONResponse({"ok": True, "new_record_id": new_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/atoms/{record_id}/forget")
async def forget_atom(record_id: str, request: Request):
    """遗忘：``state → tombstone``（软删：保留内容与原因，审计可溯，不物理删）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str((body or {}).get("reason") or "owner-forget")[:160]
    try:
        from omni_core import memory_brain as mb
        done = mb.forget(str(record_id or ""), reason)
        if not done:
            return JSONResponse({"ok": False, "error": "记录不存在或已遗忘"}, status_code=404)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
