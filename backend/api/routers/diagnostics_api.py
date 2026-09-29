"""只读诊断：进程内存与运行时容器占用量化。

用途：定位后端进程内存增长来源（outbox / live 快照 / 运行体表 / SSE 协程）。
约定：**只读**，不清理、不修改任何业务状态；条目体积用抽样估算，
避免对上万条事件做全量 json 序列化（那样本身就制造一次内存峰值）。
"""
from __future__ import annotations

import json as _json
import os
import threading
from typing import Any, Dict, Iterable, List

from fastapi import APIRouter, Query

from backend.api.runtime_manager import manager
from backend.api.routers.helpers import live_stats, sse_connections

# 重型依赖：一旦被 import 就会把整个运行时（torch/CUDA、OCR 权重等）钉进进程 RSS
_HEAVY_MODULES = ("torch", "easyocr", "cv2", "numpy", "onnxruntime",
                  "paddle", "transformers", "mss", "PIL")

# 前缀 /diagnostics：避免与 memory_api 的 /memory 同名路由撞车
# （最终路径 GET /api/runtime/diagnostics/memory）。
router = APIRouter(prefix="/diagnostics", tags=["runtime"])

_SAMPLE = 30  # 每通道抽样条数（估算均值用）


def _bytes_of(obj: Any) -> int:
    try:
        return len(_json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return 0


def _stats(items: Iterable[Any]) -> Dict[str, int]:
    lst: List[Any] = list(items)
    n = len(lst)
    if n == 0:
        return {"entries": 0, "bytes_est": 0, "max_entry_bytes": 0}
    sizes = [_bytes_of(x) for x in lst[:_SAMPLE]]
    avg = sum(sizes) / len(sizes)
    return {
        "entries": n,
        "bytes_est": int(avg * n),
        "max_entry_bytes": max(sizes),
    }


def _aggregate(boxes: Dict[str, Dict[str, list]]) -> Dict[str, Any]:
    """把 {task_id: {channel: [entry]}} 聚成 总计 / 分通道 / 单 task 排行。"""
    per_channel: Dict[str, Dict[str, int]] = {}
    per_task: Dict[str, int] = {}
    total = 0
    for tid, box in boxes.items():
        t_bytes = 0
        for ch, entries in box.items():
            st = _stats(entries)
            t_bytes += st["bytes_est"]
            agg = per_channel.setdefault(ch, {"entries": 0, "bytes_est": 0})
            agg["entries"] += st["entries"]
            agg["bytes_est"] += st["bytes_est"]
        per_task[tid] = t_bytes
        total += t_bytes
    top = sorted(per_task.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return {
        "tasks": len(boxes),
        "bytes_est": total,
        "bytes_est_mb": round(total / 1048576, 1),
        "by_channel": per_channel,
        "top_tasks": [{"task_id": t, "bytes_est": b} for t, b in top],
    }


def _heavy_modules() -> Dict[str, Any]:
    """哪些重型依赖已被加载进本进程（一次性常驻，不随任务数增长）。"""
    import sys
    out: Dict[str, Any] = {m: bool(sys.modules.get(m)) for m in _HEAVY_MODULES}
    t = sys.modules.get("torch")
    if t is not None:
        try:
            out["torch_version"] = t.__version__
            out["cuda_available"] = bool(t.cuda.is_available())
            if t.cuda.is_available():
                out["cuda_allocated_mb"] = round(t.cuda.memory_allocated() / 1048576, 1)
                out["cuda_reserved_mb"] = round(t.cuda.memory_reserved() / 1048576, 1)
        except Exception:
            pass
    return out


def _gc_top_types(limit: int = 15) -> List[Dict[str, Any]]:
    """按类型聚合对象数与字节数（getsizeof 不递归；ndarray/Tensor 按真实数据大小统计）。

    仅在 ?gc=1 时调用：gc.get_objects() 本身会建一张全对象表，有额外开销。
    """
    import gc
    import sys
    agg: Dict[str, List[int]] = {}
    for o in gc.get_objects():
        name = type(o).__name__
        try:
            if name == "ndarray":
                n = int(getattr(o, "nbytes", 0) or 0)
            elif name == "Tensor":
                n = int(o.numel() * o.element_size()) if hasattr(o, "numel") else 0
            else:
                n = sys.getsizeof(o)
        except Exception:
            n = 0
        a = agg.setdefault(name, [0, 0])
        a[0] += 1
        a[1] += n
    top = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)[:limit]
    return [{"type": t, "count": c, "bytes": b, "mb": round(b / 1048576, 1)}
            for t, (c, b) in top]


@router.get("/memory")
def memory_diagnostics(gc: int = Query(0, ge=0, le=1)) -> Dict[str, Any]:
    """进程内存 + 运行时容器占用量化（只读）。"""
    rss_mb = None
    try:
        import psutil
        rss_mb = round(psutil.Process(os.getpid()).memory_info().rss / 1048576, 1)
    except Exception:
        pass

    outboxes = manager.outbox_stats()
    live = live_stats()

    payload: Dict[str, Any] = {
        "ok": True,
        "process_rss_mb": rss_mb,
        "threads": threading.active_count(),
        "thread_names": [t.name for t in threading.enumerate()],
        "sse_connections": sse_connections(),
        "heavy_modules": _heavy_modules(),
        "outboxes": _aggregate(outboxes),
        "live_snapshot": _aggregate(live),
        "runs": manager.runs_stats(),
    }
    if gc:
        payload["gc_top_types"] = _gc_top_types()
    return payload
