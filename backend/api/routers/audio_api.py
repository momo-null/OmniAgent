"""/api/audio/*：语音插件的无状态媒体端点（转写 / 合成）。

薄壳路由：真正的实现在 ``plugins/voice``（capability ``voice_transcribe`` /
``voice_tts``，懒加载，插件未启用 = capability 不存在 = 404）。本模块不做任何
引擎/模型知识——语音对内核不可见（单链路红线），这里只有「查能力 → 调用」。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from omni_core.tools.capability import get_capability

router = APIRouter(tags=["audio"])

_MAX_UPLOAD = 10 * 1024 * 1024  # 10MB（60s 16k mono wav ≈ 1.9MB，余量给其他封装）
_MAX_TTS_CHARS = 500


@router.post("/api/audio/transcribe")
async def audio_transcribe(request: Request):
    """WAV 音频 → 文本（voice 插件未启用时 404）。"""
    fn = get_capability("voice_transcribe")
    if fn is None:
        return JSONResponse({"ok": False, "error": "voice 插件未启用"}, status_code=404)
    body = await request.body()
    if not body:
        return JSONResponse({"ok": False, "error": "空音频"}, status_code=400)
    if len(body) > _MAX_UPLOAD:
        return JSONResponse({"ok": False, "error": "音频过大（上限 10MB）"}, status_code=413)
    try:
        text = await run_in_threadpool(fn, body)
    except Exception as e:  # noqa: BLE001 —— 模型缺失/依赖缺失等运行态问题原样带给前端
        return JSONResponse({"ok": False, "error": f"转写失败: {e}"}, status_code=503)
    return {"ok": True, "text": text}


@router.post("/api/audio/tts")
async def audio_tts(request: Request):
    """文本 → WAV 音频（voice 插件未启用时 404）。"""
    fn = get_capability("voice_tts")
    if fn is None:
        return JSONResponse({"ok": False, "error": "voice 插件未启用"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体必须是 JSON"}, status_code=400)
    text = str((payload or {}).get("text") or "").strip()
    voice = str((payload or {}).get("voice") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text 为空"}, status_code=400)
    if len(text) > _MAX_TTS_CHARS:
        return JSONResponse(
            {"ok": False, "error": f"文本过长（上限 {_MAX_TTS_CHARS} 字）"}, status_code=400)
    try:
        wav = await run_in_threadpool(fn, text, voice)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"合成失败: {e}"}, status_code=503)
    return Response(content=wav, media_type="audio/wav")
