"""B站通道待发审批 API。

主人控制面：看待发队列、批准真发、作废。逻辑全部复用适配器的
``approve_item``/``discard_item``（与 CLI --approve 同一份代码）——写路径依旧
单点收口在通道适配器（全量四道闸），API 只是换了个批准的入口。
边界：只在本机（backend 默认绑回环）；批准动作不可逆，接口不批量。
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/bilibili", tags=["runtime"])


def _adapter():
    from gateway.channel_gateway import channel_client

    client = channel_client("bilibili")
    if client is None:
        raise RuntimeError("bilibili 通道不可用（先配置 channels/bilibili.yaml）")
    return client


@router.get("/outbox")
async def outbox():
    """待发队列（含已发/已作废，最新在前由前端排序）。"""
    try:
        a = _adapter()
        items = list(reversed(a.outbox()))
        for i, it in enumerate(items):
            it["no"] = len(items) - i  # 原始序号（--approve/approve_item 用 1 起）
        return JSONResponse({"ok": True, "items": items,
                             "mode": a.mode, "ready": a.ready(),
                             "quotas": a.quotas, "state": a._load_state()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/outbox/{no}/approve")
async def approve(no: int):
    """批准第 no 条真发（仍过配额/黑名单/暂停/登录闸）。"""
    try:
        ret = _adapter().approve_item(no)
        return JSONResponse(ret, status_code=200 if ret.get("ok") else 400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/outbox/{no}/discard")
async def discard(no: int):
    """作废第 no 条（不发送，留审计痕迹）。"""
    try:
        ret = _adapter().discard_item(no)
        return JSONResponse(ret, status_code=200 if ret.get("ok") else 400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
