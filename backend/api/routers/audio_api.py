"""/api/audio/* + /v1/audio/*：语音插件的无状态媒体端点（转写 / 合成）。

薄壳路由：真正的实现在 ``plugins/voice``（capability ``voice_transcribe`` /
``voice_tts``，懒加载，插件未启用 = capability 不存在 = 404）。本模块不做任何
引擎/模型知识——语音对内核不可见（单链路红线），这里只有「查能力 → 调用」。

/v1/audio/*（2026-11-04）：OpenAI 音频 API 兼容形状（POST /v1/audio/transcriptions
multipart / POST /v1/audio/speech json），与 plugins/voice 的 provider=openai 客户端
及 engine_service 独立服务同一形状——后端本体即可直接当外部客户端（手机壳等）的
语音供应商，不必另起 engine_service。
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from omni_core.tools.capability import get_capability

router = APIRouter(tags=["audio"])

_MAX_UPLOAD = 10 * 1024 * 1024  # 10MB（60s 16k mono wav ≈ 1.9MB，余量给其他封装）
_MAX_TTS_CHARS = 500


def _capability(name: str):
    """按名取能力；未注册时**幂等装载一次插件**再查。

    后端不在启动时载插件（首个 agent run / 网关启动才载），而语音端点是随时可调的
    媒体接口——设置页试听、手机壳首次接入、桌面刚起来就说话，都必须先能拿到能力。
    loader 本身幂等（已装载即跳过），所以代价只有第一次。
    """
    fn = get_capability(name)
    if fn is not None:
        return fn
    try:
        import config as app_config
        from omni_core.tools.loader import PluginContext, load_plugins

        cfg = app_config.load_config() or {}
        load_plugins(cfg, ctx=PluginContext(config=cfg, wired=False))
    except Exception:  # noqa: BLE001 —— 装载失败按"能力不存在"处理（端点 404）
        return None
    return get_capability(name)


@router.post("/api/audio/transcribe")
async def audio_transcribe(request: Request):
    """WAV 音频 → 文本（voice 插件未启用时 404）。"""
    fn = _capability("voice_transcribe")
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
    fn = _capability("voice_tts")
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


@router.get("/api/audio/status")
async def audio_status():
    """引擎来源 / 运行态 / 模型就绪快照（设置页用；voice 插件未启用时 404）。

    薄壳：内容全部由能力 ``voice_status`` 自报（宿主不认识任何语音键名）。
    """
    fn = _capability("voice_status")
    if fn is None:
        return JSONResponse({"ok": False, "error": "voice 插件未启用"}, status_code=404)
    try:
        data = await run_in_threadpool(fn)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"状态读取失败: {e}"}, status_code=503)
    return {"ok": True, **(data or {})}


# ---------------- OpenAI 音频 API 兼容形状（/v1/audio/*） ----------------

@router.post("/v1/audio/transcriptions")
async def openai_transcriptions(
    file: UploadFile = File(...),
    model: str = Form(""),
):
    """multipart(file, model) → {"text": ...}（OpenAI 形状；model 仅透传兼容，能力侧自选引擎）。"""
    fn = _capability("voice_transcribe")
    if fn is None:
        return JSONResponse({"ok": False, "error": "voice 插件未启用"}, status_code=404)
    data = await file.read()
    if not data:
        return JSONResponse({"error": "空音频"}, status_code=400)
    if len(data) > _MAX_UPLOAD:
        return JSONResponse({"error": "音频过大（上限 10MB）"}, status_code=413)
    try:
        text = await run_in_threadpool(fn, data)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"转写失败: {e}"}, status_code=503)
    return {"text": text}


@router.post("/v1/audio/speech")
async def openai_speech(request: Request):
    """json{model, input, voice} → audio/wav 字节（OpenAI 形状）。"""
    fn = _capability("voice_tts")
    if fn is None:
        return JSONResponse({"error": "voice 插件未启用"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    text = str((payload or {}).get("input") or "").strip()
    voice = str((payload or {}).get("voice") or "").strip()
    if not text:
        return JSONResponse({"error": "input 为空"}, status_code=400)
    if len(text) > _MAX_TTS_CHARS:
        return JSONResponse({"error": f"文本过长（上限 {_MAX_TTS_CHARS} 字）"}, status_code=400)
    try:
        wav = await run_in_threadpool(fn, text, voice)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"合成失败: {e}"}, status_code=503)
    return Response(content=wav, media_type="audio/wav")
