"""信号与稳态指标接口。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

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

@router.put("/signals/calibration")
async def signals_calibration(request: Request):
    """C₃ 人工抽检校准写入：收 ``{samples:[{predicted, human}]}``，
    用 ``calibrate_c1c2`` 算不一致率并写进聚合基线的 ``c1_calibration_error``。
    该字段此前无写入入口，永远为 null；本端点补齐后 C₃ 才能被验证/采信。
    """
    from omni_core.local import signals as _sig
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    samples = (body or {}).get("samples")
    if not isinstance(samples, list):
        return JSONResponse({"ok": False, "error": "samples 必须是数组 [{predicted, human}]"}, status_code=422)
    clean = []
    for s in samples:
        pred = (s or {}).get("predicted")
        human = (s or {}).get("human")
        if isinstance(pred, str) and isinstance(human, str) and pred.strip() and human.strip():
            clean.append({"predicted": pred.strip(), "human": human.strip()})
    if not clean:
        return JSONResponse({"ok": False, "error": "无有效样本"}, status_code=422)
    error = _sig.calibrate_c1c2(clean)
    agg = _sig.read_aggregate()
    agg["c1_calibration_error"] = error
    agg["calibration_samples"] = len(clean)
    agg["updated_at"] = datetime.now(timezone.utc).isoformat()
    p = _sig.memory_signals_aggregate()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"写入聚合失败: {e}"}, status_code=500)
    trusted = error <= 0.1
    return JSONResponse({
        "ok": True,
        "c1_calibration_error": error,
        "trusted": trusted,
        "threshold": 0.1,
        "samples": len(clean),
        "note": "不一致率 ≤10%（0.1）方采信分级判定",
    })
