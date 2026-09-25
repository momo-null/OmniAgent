"""模型管理路由

委托 ``model_hub`` 管理器完成模型的列表 / 启停 / 校验 / 状态 / 日志 / 扫描 / GPU / 保存侧注。

设计边界：本路由只服务「本地模型管理链路」，与自训练链路（training）互不调用。

重构说明（model-meta-refactor, 2026-07-11）：
- 不再依赖 config 注册表；模型发现改为扫描目录（GET /api/models 支持 ?models_dir=）。
- 新增 POST /api/models/{name}/save 持久化侧注（.meta.json）。
"""
import asyncio
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.api.deps import get_model_hub
from model_hub.manager import ModelManager
from model_hub.schemas import (
    LaunchParams,
    ModelValidateRequest,
    ModelStartPathRequest,
    ModelMetaSaveRequest,
)

router = APIRouter(prefix="/api", tags=["models"])


def _extract_overrides(body: LaunchParams) -> tuple[Dict[str, Any], Optional[str]]:
    """从请求体抽取逐参数覆盖与预设名（排除 None 值）"""
    overrides: Dict[str, Any] = {}
    if body.threads is not None:
        overrides["threads"] = body.threads
    if body.ctx_size is not None:
        overrides["ctx_size"] = body.ctx_size
    if body.gpu_layers is not None:
        overrides["gpu_layers"] = body.gpu_layers
    if body.port is not None:
        overrides["port"] = body.port
    if body.reasoning_budget is not None:
        overrides["reasoning_budget"] = body.reasoning_budget
    return overrides, body.profile


@router.get("/models")
async def list_models(
    models_dir: Optional[str] = Query(None, description="扫描目录，默认 D:\\AI\\Models"),
    mgr: ModelManager = Depends(get_model_hub),
) -> List[Dict[str, Any]]:
    """列出所有模型（扫描目录 + 侧注合并结果）"""
    return mgr.list_models(models_dir)


@router.post("/models/{name}/start")
async def start_model(
    name: str,
    body: LaunchParams = LaunchParams(),
    mgr: ModelManager = Depends(get_model_hub),
) -> Dict[str, Any]:
    """启动指定模型（支持逐参数覆盖与预设；可直接传 gguf_path）"""
    import asyncio
    try:
        overrides, profile = _extract_overrides(body)
        # P2.4: 同步阻塞操作移出事件循环，避免阻塞 SSE 心跳
        result = await asyncio.to_thread(
            mgr.start_model,
            name,
            gguf_path=body.gguf_path,
            wait_ready=True,
            timeout=90,
            overrides=overrides or None,
            profile=profile,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/{name}/validate")
async def validate_model(
    name: str,
    body: ModelValidateRequest = ModelValidateRequest(),
    mgr: ModelManager = Depends(get_model_hub),
) -> Dict[str, Any]:
    """校验模型：仅对已在运行的实例发测试请求；不自动启动（需先点「启动」）"""
    import asyncio
    try:
        overrides, profile = _extract_overrides(body)
        return await asyncio.to_thread(
            mgr.validate_model,
            name,
            gguf_path=body.gguf_path,
            prompt=body.prompt,
            image_path=body.image_path,
            overrides=overrides or None,
            profile=profile,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/{name}/save")
async def save_model_meta(
    name: str,
    body: ModelMetaSaveRequest = ModelMetaSaveRequest(gguf_path=""),
    mgr: ModelManager = Depends(get_model_hub),
) -> Dict[str, Any]:
    """保存/更新模型侧注（.meta.json）

    非 None 字段写入；显式传 None 的已知字段从侧注删除。
    """
    try:
        return mgr.save_model_meta(name, body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/start-path")
async def start_model_path(
    body: ModelStartPathRequest,
    mgr: ModelManager = Depends(get_model_hub),
) -> Dict[str, Any]:
    """启动扫描发现的未注册 GGUF（自动分配端口）"""
    import asyncio
    try:
        overrides: Dict[str, Any] = {}
        if body.threads is not None:
            overrides["threads"] = body.threads
        if body.ctx_size is not None:
            overrides["ctx_size"] = body.ctx_size
        if body.gpu_layers is not None:
            overrides["gpu_layers"] = body.gpu_layers
        if body.port is not None:
            overrides["port"] = body.port
        if body.reasoning_budget is not None:
            overrides["reasoning_budget"] = body.reasoning_budget
        return await asyncio.to_thread(
            mgr.start_model_path,
            body.path, name=body.name,
            overrides=overrides or None, profile=body.profile,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/models/{name}/stop")
async def stop_model(
    name: str,
    mgr: ModelManager = Depends(get_model_hub),
) -> Dict[str, Any]:
    """停止指定模型"""
    try:
        return mgr.stop_model(name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models/{name}/status")
async def model_status(
    name: str,
    mgr: ModelManager = Depends(get_model_hub),
) -> Dict[str, Any]:
    """模型状态详情（运行态优先，否则从扫描结果取端口信息）"""
    active = mgr.get_active_models()
    for m in active:
        if m["name"] == name:
            logs = mgr.get_model_logs(name)
            return {**m, "logs": logs}

    entry = None
    for m in mgr.scan_and_build_models():
        if m["name"] == name:
            entry = m
            break
    if entry:
        return {"name": name, "status": "stopped", "port": entry.get("port")}
    return {"name": name, "status": "stopped", "port": None}


@router.get("/models/{name}/logs")
async def model_logs(
    name: str,
    lines: int = Query(50, ge=1, le=2000),
    mgr: ModelManager = Depends(get_model_hub),
) -> Dict[str, str]:
    """获取模型日志（轮询兜底）"""
    return mgr.get_model_logs(name, lines)


@router.get("/models/{name}/logs/stream")
async def model_logs_stream(
    name: str,
    mgr: ModelManager = Depends(get_model_hub),
):
    """实时日志流（SSE）：推送新增日志行，进程结束后关闭"""
    async def event_gen():
        seen = 0
        # 先把已存在日志推一遍
        while True:
            try:
                res = mgr.tail_logs(name, seen)
            except Exception:
                break
            for ln in res["lines"]:
                yield f"data: {ln}\n\n"
            seen = res["seen"]
            if name not in mgr.processes:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/active")
async def active_models(
    mgr: ModelManager = Depends(get_model_hub),
) -> List[Dict[str, Any]]:
    """返回当前运行中的模型"""
    return mgr.get_active_models()


@router.post("/stop-all")
async def stop_all(
    mgr: ModelManager = Depends(get_model_hub),
) -> List[Dict[str, Any]]:
    """停止所有模型"""
    return mgr.stop_all()


@router.post("/models/kill-orphans")
async def kill_orphans(
    mgr: ModelManager = Depends(get_model_hub),
) -> List[Dict[str, Any]]:
    """C：强制释放所有 llama-server 进程（含已跟踪实例与未接管孤儿）

    与 /api/stop-all 不同：本接口会系统级扫一遍 llama-server.exe 兜底强杀，
    覆盖「后端崩溃遗留、且未被启动接管」的孤儿进程。返回被杀清单。
    """
    import asyncio
    try:
        return await asyncio.to_thread(mgr.kill_orphans)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/scan")
async def scan_models(
    models_dir: Optional[str] = Query(None, description="扫描目录，默认 D:\\AI\\Models"),
    mgr: ModelManager = Depends(get_model_hub),
) -> List[Dict[str, Any]]:
    """扫描本地 GGUF 文件（与 /api/models 相同的统一结果）"""
    return mgr.scan_models_dir(models_dir)


@router.get("/gpu")
async def gpu_info() -> Dict[str, Any]:
    """GPU VRAM 信息（nvidia-smi 查询，失败返回 error）"""
    try:
        import subprocess
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
        get_logger("router_models").warning("GPU 信息获取失败: %s", e)
    return {"error": "nvidia-smi 不可用"}
