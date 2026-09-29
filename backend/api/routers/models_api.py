"""模型目录 API（``/api/runtime/models``）——消费端：Chat 页模型选择器 + 设置页模型配置。

目录存于 ``~/.omniagent/models.json``（与通用 config 隔离，见 config.MODELS_CONFIG_PATH），
本模块只做校验 + 转发，解析/落盘逻辑全在 ``omni_core.brain.router``。

- GET   /api/runtime/models          分组目录 + 各槽位当前生效端点（密钥脱敏）
- PUT   /api/runtime/models          整体写回 providers（``{"providers": {...}}``）
- PATCH /api/runtime/models/default  设置某槽位默认模型（``{"slot": "main", "selection": "p/m"}``）
"""
import os
from typing import Any, Dict

from fastapi import APIRouter
from fastapi import Request
from fastapi.responses import JSONResponse

import config
from omni_core.brain import router as model_router

router = APIRouter(tags=["runtime"])


def _bad(msg: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=400)


def _validate_providers(node: Any) -> Dict[str, Any]:
    """校验 providers：必须是 dict，每项 dict 且 base_url 非空、id 合法。"""
    if node is None:
        return {}
    if not isinstance(node, dict):
        raise ValueError("providers 必须是对象")
    out: Dict[str, Any] = {}
    for pid, p in node.items():
        if not isinstance(p, dict):
            raise ValueError(f"providers.{pid} 必须是对象")
        key = str(pid).strip()
        if not key or "/" in key:
            raise ValueError(f"provider id 非法（不能为空或含 /）: {pid}")
        base_url = str(p.get("base_url") or "").strip()
        if not base_url:
            raise ValueError(f"providers.{key}.base_url 不能为空")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError(f"providers.{key}.base_url 必须以 http:// 或 https:// 开头")
        models = p.get("models")
        if models is not None and not isinstance(models, list):
            raise ValueError(f"providers.{key}.models 必须是数组")
        out[key] = p
    return out


@router.get("/models")
async def list_models() -> Dict[str, Any]:
    """返回按提供方分组的模型目录与各槽位当前生效端点。"""
    try:
        data = model_router.list_models()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"读取模型目录失败: {e}"}, status_code=500)
    data["ok"] = True
    data["_meta"] = {
        "path": config.MODELS_CONFIG_PATH,
        "exists": os.path.exists(config.MODELS_CONFIG_PATH),
    }
    return data


@router.put("/models")
async def put_models(req: Request) -> JSONResponse:
    """整体写回 providers（前端设置页维护目录）。

    api_key 传空串 = 保持不变（GET 返回的是脱敏视图，避免回传把密钥清空）。
    """
    try:
        body = await req.json()
    except Exception:
        return _bad("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _bad("请求体必须是对象")
    try:
        providers = _validate_providers(body.get("providers"))
    except ValueError as e:
        return _bad(str(e))
    try:
        model_router.save_providers(providers)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"保存模型目录失败: {e}"}, status_code=500)
    return JSONResponse({"ok": True})


@router.patch("/models/default")
async def set_model_default(req: Request) -> JSONResponse:
    """设置某槽位默认模型；selection 为空 = 跟随配置（brain / runtime.agents）。"""
    try:
        body = await req.json()
    except Exception:
        return _bad("请求体不是合法 JSON")
    if not isinstance(body, dict):
        return _bad("请求体必须是对象")
    slot = str(body.get("slot") or "main").strip()
    selection = body.get("selection")
    try:
        model_router.set_default(slot, None if selection is None else str(selection))
    except ValueError as e:
        return _bad(str(e))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"设置默认模型失败: {e}"}, status_code=500)
    return JSONResponse({"ok": True})
