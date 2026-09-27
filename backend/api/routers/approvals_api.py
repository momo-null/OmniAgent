"""S2 审批决议 + 审计只读路由。

- POST /approvals/{approval_id}/decision：人工决议（approve|deny，含任务级记忆）；
- GET  /audit：审计 jsonl 尾部 N 条（设置页「安全」区消费）。

鉴权沿用现有服务面（server 仅回环绑定；非回环必须配 auth_token 才能启动）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.services.approvals import get_sink

router = APIRouter(tags=["approvals"])


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(approval_id: str, request: Request):
    """人工决议一张待批卡。body: ``{action: "approve"|"deny", remember?: bool}``"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    action = str(body.get("action") or "")
    if action not in ("approve", "deny"):
        return JSONResponse({"ok": False, "error": "action 须为 approve 或 deny"}, status_code=400)
    remember = bool(body.get("remember") or False)
    sink = get_sink()
    if sink is None:
        return JSONResponse({"ok": False, "error": "审批后端未启用"}, status_code=409)
    ok = sink.resolve(approval_id, action, remember)
    if not ok:
        return JSONResponse({"ok": False, "error": "审批不存在或已决议（可能已超时）"}, status_code=404)
    return JSONResponse({"ok": True, "msg": "已决议"})


@router.get("/audit")
async def read_audit(limit: int = 200):
    """审计记录尾部 N 条（跨月文件倒序聚合；只读）。"""
    from omni_core.tools.policy import audit_dir

    limit = max(1, min(int(limit or 200), 2000))
    d = audit_dir()
    items: List[Dict[str, Any]] = []
    if d.is_dir():
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.name, reverse=True)
        for f in files:
            if len(items) >= limit:
                break
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for ln in reversed(lines):
                if len(items) >= limit:
                    break
                if not ln.strip():
                    continue
                try:
                    items.append(json.loads(ln))
                except Exception:
                    continue
    items.reverse()  # 时间正序输出（旧→新），前端表格直接渲染
    return JSONResponse({"ok": True, "items": items})
