"""伙伴层读写接口(P0):画像(自动维护在 omni_core.memory_brain 巩固)与角色卡。
画像/角色卡的人工入口都在这里,均不校验内容(人可手改是定案)。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["runtime"])


@router.get("/profile")
async def get_profile():
    """全局用户画像(P0):user_profile.md 全文 + 注入开关(只读聚合)。"""
    try:
        from backend.api.routers.helpers import _profile_enabled
        from omni_core.local import runtime_paths as P
        pp = P.user_profile()
        profile = pp.read_text(encoding="utf-8") if pp.exists() else ""
        return JSONResponse({"profile": profile, "enabled": _profile_enabled()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.put("/profile")
async def put_profile(request: Request):
    """用户画像人工写入口:覆盖 user_profile.md(人可手改是定案,不校验内容)。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    profile = (body or {}).get("profile")
    if not isinstance(profile, str):
        return JSONResponse({"ok": False, "error": "profile 必须是字符串"}, status_code=422)
    try:
        from omni_core.local import runtime_paths as P
        _write_with_backup(P.user_profile(), profile)
        return JSONResponse({"ok": True, "chars": len(profile)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _write_with_backup(path, text: str) -> None:
    """覆盖写入前把旧文件备份为 ``.bak``(单级;人工编辑/自动维护/误写都可就地恢复)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.with_suffix(path.suffix + ".bak"))
    path.write_text(text, encoding="utf-8")


def _parse_character_name(text: str) -> str:
    """从角色卡顶部 frontmatter 解析 ``name:``;无 frontmatter/无 name → 默认 "OmniAgent"。"""
    text = text or ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for ln in parts[1].splitlines():
                if ln.strip().startswith("name:"):
                    return ln.split(":", 1)[1].strip() or "OmniAgent"
    return "OmniAgent"


@router.get("/character")
async def get_character():
    """单角色助手角色卡(P0):character.md 全文 + 解析出的助手名(随 system prompt 注入)。"""
    try:
        from omni_core.local import runtime_paths as P
        cp = P.character_card()
        character = cp.read_text(encoding="utf-8") if cp.exists() else ""
        return JSONResponse({"character": character, "name": _parse_character_name(character)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.put("/character")
async def put_character(request: Request):
    """角色卡人工写入口:覆盖 character.md(改动自下一次运行起生效,同 AGENTS.md 语义)。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    character = (body or {}).get("character")
    if not isinstance(character, str):
        return JSONResponse({"ok": False, "error": "character 必须是字符串"}, status_code=422)
    try:
        from omni_core.local import runtime_paths as P
        _write_with_backup(P.character_card(), character)
        return JSONResponse({"ok": True, "chars": len(character)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/instructions")
async def get_instructions():
    """纪律文件（AGENTS.md）全文——system prompt 的用户可编辑层（M2.5 手机编辑器）。

    与 character 卡同语义：改动自下一次运行起生效（run 内沿用起始快照）。
    """
    try:
        from omni_core.local import runtime_paths as P
        p = P.global_agents_file()
        content = p.read_text(encoding="utf-8") if p.exists() else ""
        return JSONResponse({"ok": True, "content": content, "exists": p.exists()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.put("/instructions")
async def put_instructions(request: Request):
    """纪律文件人工写入口：覆盖全局 AGENTS.md（下次运行生效）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    content = (body or {}).get("content")
    if not isinstance(content, str):
        return JSONResponse({"ok": False, "error": "content 必须是字符串"}, status_code=422)
    try:
        from omni_core.local import runtime_paths as P
        _write_with_backup(P.global_agents_file(), content)
        return JSONResponse({"ok": True, "chars": len(content)})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
