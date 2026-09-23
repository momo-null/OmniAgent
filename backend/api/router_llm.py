"""OpenAI 兼容推理路由

提供 ``/v1/chat/completions``、``/v1/completions``、``/v1/models``，
逻辑从原 ``llm_runtime/api_gateway.py`` 搬来（含 lazy load）。
"""
import json
import time
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from backend.api.deps import get_model_hub, _resolve_model_name, _ensure_model_running
from model_hub.manager import ModelManager
from llm_runtime.server_backend import LlamaServerRuntime

router = APIRouter(tags=["llm"])


class ChatMessage(BaseModel):
    """OpenAI 格式对话消息"""
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    """chat.completions 请求体"""
    model: str = "default"
    messages: List[ChatMessage]
    max_tokens: int = 512
    temperature: float = 0.1
    top_p: float = 0.9
    stream: bool = False


class CompletionRequest(BaseModel):
    """completions 请求体"""
    model: str = "default"
    prompt: str
    max_tokens: int = 512
    temperature: float = 0.1
    top_p: float = 0.9
    stream: bool = False


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """OpenAI 兼容 chat completions"""
    resolved = _resolve_model_name(req.model)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"模型未找到: {req.model}")

    # Lazy load
    start_result = _ensure_model_running(resolved)
    if start_result.get("status") not in ("already_running", "ready"):
        raise HTTPException(status_code=503, detail=f"模型启动失败: {start_result}")

    mgr = get_model_hub()
    if resolved not in mgr.processes:
        raise HTTPException(status_code=503, detail=f"模型未运行: {resolved}")
    port = mgr.processes[resolved]["port"]
    backend = LlamaServerRuntime(base_url=f"http://127.0.0.1:{port}")

    if req.stream:
        return StreamingResponse(
            _stream_chat(backend, req),
            media_type="text/event-stream",
        )

    # 同步调用
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    try:
        content = backend.generate_with_messages(messages, {
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "top_p": req.top_p,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "id": f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resolved,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })


async def _stream_chat(backend: LlamaServerRuntime, req: ChatCompletionRequest):
    """流式 chat 响应生成器"""
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    try:
        for token in backend.generate_stream_with_messages(messages, {
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "top_p": req.top_p,
        }):
            chunk = {
                "id": f"chatcmpl-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": req.model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": token},
                    "finish_reason": None,
                }],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        error_chunk = {"error": {"message": str(e), "type": "runtime_error"}}
        yield f"data: {json.dumps(error_chunk)}\n\n"


@router.post("/v1/completions")
async def completions(req: CompletionRequest):
    """OpenAI 兼容 text completions"""
    resolved = _resolve_model_name(req.model)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"模型未找到: {req.model}")

    _ensure_model_running(resolved)

    mgr = get_model_hub()
    if resolved not in mgr.processes:
        raise HTTPException(status_code=503, detail=f"模型未运行: {resolved}")
    port = mgr.processes[resolved]["port"]
    backend = LlamaServerRuntime(base_url=f"http://127.0.0.1:{port}")

    try:
        content = backend.generate(req.prompt, {
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "top_p": req.top_p,
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({
        "id": f"cmpl-{int(time.time())}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": resolved,
        "choices": [{
            "index": 0,
            "text": content,
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })


@router.get("/v1/models")
async def list_openai_models():
    """OpenAI 兼容模型列表"""
    mgr = get_model_hub()
    models = mgr.list_models()
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": m["name"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "local",
            }
            for m in models
        ],
    })
