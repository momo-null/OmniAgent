"""小红书 MCP 返回结构归一（供 MCP 懒加载的 ``transform`` 钩子复用）。

历史：这些函数原本内联在 ``plugin.py``，给三个只读 ``@function_tool`` 用。懒加载后
xhs 走 mcp.json 的 ``mode: lazy`` + ``transform`` 钩子（工具 schema 不常驻），归一逻辑
必须能脱离插件工具独立调用——所以在这里落一份，``plugin.py`` 改为从这里 import。

约定（``omni_core.tools.mcp_meta._apply_transform``）：``transform.<tool> = "<函数名>"``，
模块路径固定为 ``plugins.<server 名（连字符换下划线）>.normalize``，即本文件。
"""
from __future__ import annotations

from typing import Any, Dict, List


def normalize_note(raw: Any) -> Dict[str, Any]:
    """xiaohongshu-mcp 返回结构有出入 → 宽容归一成统一笔记摘要（TODO_XHS_LIVE 对参）。"""
    if not isinstance(raw, dict):
        return {"raw": str(raw)[:200]}
    card = raw.get("noteCard") or raw.get("note") or raw
    user = card.get("user") or {}
    interact = card.get("interactInfo") or {}
    return {
        "feed_id": raw.get("id") or card.get("noteId") or "",
        "xsec_token": raw.get("xsecToken") or raw.get("xsec_token") or "",
        "title": card.get("displayTitle") or card.get("title") or "",
        "desc": str(card.get("desc") or "")[:120],
        "nickname": user.get("nickname") or user.get("nickName") or "",
        "user_id": user.get("userId") or user.get("id") or "",
        "liked": interact.get("likedCount") or card.get("likes"),
        "collected": interact.get("collectedCount"),
        "comments": interact.get("commentCount") or card.get("comments"),
        "type": card.get("type") or raw.get("type") or "",
    }


def extract_notes(data: Any) -> List[Any]:
    """MCP 返回的笔记列表可能在 data 自身 / data.items / data.notes / data.result。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "notes", "result", "feeds", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def extract_comments(data: Any) -> List[Any]:
    """评论可能在 list（多数端点）或 {list, cursor, hasMore}（get_feed_detail 实盘形态）。"""
    if isinstance(data, dict):
        for key in ("comments", "commentList", "replies"):
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("list"), list):
                return v["list"]
    return []


# ---------------------------------------------------------------- transform 入口
# 配置写法：mcp.json → servers[].transform = {"list_feeds": "xhs_notes", ...}

def xhs_notes(data: Any) -> List[Dict[str, Any]]:
    """列表类工具（list_feeds / search_feeds）→ 统一笔记摘要数组。"""
    return [normalize_note(x) for x in extract_notes(data)]


def xhs_detail(data: Any) -> Dict[str, Any]:
    """详情类工具（get_feed_detail）→ {note, comments}。

    详情返回的顶层结构在不同版本里不一致（笔记可能在 data 自身或 data.note），
    评论则常见 ``{comments:{list:[...]}}``——两者都按宽容口径取。
    """
    note_src = data
    if isinstance(data, dict) and isinstance(data.get("note"), dict):
        note_src = data["note"]
    return {"note": normalize_note(note_src), "comments": extract_comments(data)}
