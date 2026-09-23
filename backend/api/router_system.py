"""系统路由

提供网关健康检查与 GPU 监控接口。
"""
import subprocess
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> Dict[str, str]:
    """网关自身健康检查"""
    return {"status": "ok"}


@router.get("/api/system/gpu")
async def system_gpu() -> Dict[str, Any]:
    """GPU VRAM 信息（nvidia-smi 查询，失败返回 error）"""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace"
        )
        if result.returncode == 0:
            parts = [p.strip() for p in result.stdout.strip().split(",")]
            return {
                "name": parts[0] if len(parts) > 0 else "unknown",
                "memory_total_mb": float(parts[1]) if len(parts) > 1 else 0,
                "memory_used_mb": float(parts[2]) if len(parts) > 2 else 0,
                "memory_free_mb": float(parts[3]) if len(parts) > 3 else 0,
            }
    except Exception as e:
        from utils import get_logger
        get_logger("router_system").warning("GPU 信息获取失败: %s", e)
    return {"error": "nvidia-smi 不可用"}
