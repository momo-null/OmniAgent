"""伙伴层读写接口(P0):自我画像(自动维护在 omni_core.memory_brain 巩固)、角色卡
与主人身份(认领)。人工入口都在这里,画像/角色卡不校验内容(人可手改是定案)。"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["runtime"])


def _brain_cfg() -> Dict[str, Any]:
    """巩固链同款 brain_cfg（端点真源 models.json main 槽位；与 memory_tool 同源）。"""
    try:
        import config
        from omni_core.brain import router as model_router

        src = (config.load_config() or {}).get("brain", {}) or {}
        cfg = model_router.resolve_main_with_engine(src)
        return cfg if (cfg.get("base_url") and cfg.get("model")) else {}
    except Exception:
        return {}


def _write_master_uids(uids) -> bool:
    """认领名单落盘（load→改→save 全量用户配置，不丢其他节；缓存由 save 失效）。"""
    try:
        import config
        settings = config.load_settings()
        rel = settings.setdefault("knowledge", {}).setdefault("relations", {})
        rel["master_uids"] = [str(u) for u in uids]
        config.save_settings(settings)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 主人身份（认领）

@router.get("/identity/master")
async def get_identity_master():
    """主人身份状态：认领名单 + 主人卡（relations/master.md）元信息。"""
    try:
        from omni_core.local import runtime_paths as P
        from omni_core.local.identity import MASTER_KEY, master_speaker, master_uids
        card = P.global_relations() / f"{MASTER_KEY}.md"
        meta = {"exists": card.exists()}
        if card.exists():
            head = card.read_text(encoding="utf-8").split("-----META-END-----", 1)[0]
            for ln in head.splitlines():
                if ln.startswith("updated:"):
                    meta["updated"] = ln.split(":", 1)[1].strip()
        return JSONResponse({"speaker": master_speaker(), "uids": master_uids(),
                             "card": meta})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/identity/speakers")
async def get_identity_speakers(request: Request):
    """认领候选：近 7 天活跃说话人（≥3 条，条数降序，封顶 20，排除已认领）。

    ``q`` 非空时切换为全量前缀搜索（按昵称/uid），用于认领列表外的旧人。
    """
    try:
        from omni_core import memory_brain
        from omni_core.local.identity import MASTER_KEY, master_uids
        q = str(request.query_params.get("q") or "").strip()
        items = memory_brain.list_speakers(q=q,
                                           exclude_uids=set(master_uids()) | {MASTER_KEY})
        return JSONResponse({"ok": True, "items": items})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/identity/claim")
async def post_identity_claim(request: Request):
    """认领：这个说话人是我。原子做四件事——

    1. uid 入认领名单（config 落盘）；2. 回填 l0 历史（speaker 重盖章主人）；
    3. 收编旧卡（该 uid 的普通关系卡删除+撤索引）；4. 宽窗重建主人卡（90 天素材）。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    uid = str((body or {}).get("uid") or "").strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "uid 为空"}, status_code=422)
    try:
        from omni_core import memory_brain
        from omni_core.local.identity import MASTER_KEY, master_uids
        if uid == MASTER_KEY:
            return JSONResponse({"ok": False, "error": "不能认领主人 key"}, status_code=422)
        uids = master_uids()
        if uid in uids:
            return JSONResponse({"ok": False, "error": "已在认领名单"}, status_code=409)
        if not _write_master_uids(uids + [uid]):
            return JSONResponse({"ok": False, "error": "名单落盘失败"}, status_code=500)
        backfilled = memory_brain.restamp_speaker_as_master(uid)
        merged_card = memory_brain.retire_relation_card(uid)
        rebuilt = memory_brain.maintain_master_card(_brain_cfg(), limit_days=90)
        return JSONResponse({"ok": True, "uid": uid, "backfilled": backfilled,
                             "merged_card": merged_card, "card_rebuilt": rebuilt})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.delete("/identity/claim")
async def delete_identity_claim(request: Request):
    """解除认领：名单移除该 uid（历史 l0 不回滚；之后的新消息按原始 speaker 重新分组）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    uid = str((body or {}).get("uid") or "").strip()
    if not uid:
        return JSONResponse({"ok": False, "error": "uid 为空"}, status_code=422)
    try:
        from omni_core.local.identity import master_uids
        uids = [u for u in master_uids() if u != uid]
        if not _write_master_uids(uids):
            return JSONResponse({"ok": False, "error": "名单落盘失败"}, status_code=500)
        return JSONResponse({"ok": True, "uids": uids})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.get("/profile")
async def get_profile():
    """她的自我画像(P0):self_profile.md 全文 + 注入开关(只读聚合)。"""
    try:
        from backend.api.routers.helpers import _profile_enabled
        from omni_core.local import runtime_paths as P
        pp = P.self_profile()
        profile = pp.read_text(encoding="utf-8") if pp.exists() else ""
        return JSONResponse({"profile": profile, "enabled": _profile_enabled()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.put("/profile")
async def put_profile(request: Request):
    """自我画像人工写入口:覆盖 self_profile.md(人可手改是定案,不校验内容)。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    profile = (body or {}).get("profile")
    if not isinstance(profile, str):
        return JSONResponse({"ok": False, "error": "profile 必须是字符串"}, status_code=422)
    try:
        from omni_core.local import runtime_paths as P
        _write_with_backup(P.self_profile(), profile)
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
