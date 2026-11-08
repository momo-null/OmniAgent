"""小红书只读采集插件（大肥鱼自运营：数据分析阶段的主工具面，plan §5 M4）。

- **零写操作**：全部只读。发布/评论单点收口在 gateway/channels/xiaohongshu.py
  （四道闸），本插件永不注册发布/评论/点赞类工具——写纪律不靠自觉靠结构。
- 传输复用 xiaohongshu 通道适配器的 MCP 客户端（_mcp_call），登录态由
  xiaohongshu-mcp 的浏览器承担；未跑/未登录时工具返回明确错误。
- 数据分析约定（写进 projects/xiaohongshu/AGENTS.md）：xhs_list_feeds 看推荐流在推什么 →
  xhs_search 追对标关键词 → xhs_feed_detail 看笔记数据与评论区 → 归纳进 corpus 三件套。

工具清单（只读）：
    xhs_list_feeds()                     首页推荐流
    xhs_search(keyword)                  搜索笔记
    xhs_feed_detail(feed_id, xsec_token) 笔记详情（数据+评论）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from omni_core.tools.base import function_tool
from utils import get_logger

logger = get_logger("plugins.xiaohongshu")

_ADAPTER = None  # 懒构造单例（随通道配置）


def _adapter():
    """按 xiaohongshu 通道配置构造适配器（只用作 MCP 读客户端，不 connect）。"""
    global _ADAPTER
    if _ADAPTER is None:
        try:
            from gateway.channel_gateway import channel_config
            from gateway.channels.xiaohongshu import XhsAdapter

            _ADAPTER = XhsAdapter(channel_config("xiaohongshu"))
        except Exception as e:
            raise RuntimeError(f"xiaohongshu 通道不可用（先配置 channels/xiaohongshu.yaml）: {e}")
    return _ADAPTER


def _ok(data: Any, **extra) -> Dict[str, Any]:
    return {"ok": True, "data": data, **extra}


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": msg}


def _normalize_note(raw: Any) -> Dict[str, Any]:
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


def _mcp_data(ret: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    if not ret.get("ok"):
        return None, str(ret.get("error", "MCP 调用失败"))
    return ret.get("data"), None


def _extract_notes(data: Any) -> List[Any]:
    """MCP 返回的笔记列表可能在 data 自身 / data.items / data.notes / data.result。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "notes", "result", "feeds", "data"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def _extract_comments(data: Any) -> List[Any]:
    """评论可能在 list（多数端点）或 {list, cursor, hasMore}（get_feed_detail 实盘形态）。"""
    if isinstance(data, dict):
        for key in ("comments", "commentList", "replies"):
            v = data.get(key)
            if isinstance(v, list):
                return v
            if isinstance(v, dict) and isinstance(v.get("list"), list):
                return v["list"]
    return []


@function_tool(
    description="获取小红书首页推荐流（只读）。用于数据分析：看平台现在在推什么类型的内容。"
                "需要 xiaohongshu-mcp 已运行且已登录。",
    unit="xiaohongshu",
)
def xhs_list_feeds(n: int = 10) -> Dict[str, Any]:
    """首页推荐流（只读）。

    Args:
        n: 返回条数（1-20，缺省 10）。
    """
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))
    ret = a._mcp_call("list_feeds")
    data, err = _mcp_data(ret)
    if err:
        return _err(err)
    notes = _extract_notes(data)[: max(1, min(int(n or 10), 20))]
    return _ok([_normalize_note(x) for x in notes], count=len(notes))


@function_tool(
    description="在小红书按关键词搜索笔记（只读）。用于追踪对标账号、话题与竞品内容。"
                "返回 feed_id/xsec_token（看详情用）、标题、作者、互动数据。",
    unit="xiaohongshu",
)
def xhs_search(keyword: str, n: int = 10) -> Dict[str, Any]:
    """搜索笔记（只读）。

    Args:
        keyword: 搜索关键词。
        n: 返回条数（1-20，缺省 10）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return _err("关键词为空")
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))
    ret = a._mcp_call("search_feeds", {"keyword": keyword})
    data, err = _mcp_data(ret)
    if err:
        return _err(err)
    notes = _extract_notes(data)[: max(1, min(int(n or 10), 20))]
    return _ok([_normalize_note(x) for x in notes], count=len(notes))


@function_tool(
    description="获取小红书单条笔记的详情：正文、互动数据（赞藏评）与评论区（只读）。"
                "数据分析的主入口：看一条爆笔记长什么样、评论区在聊什么。"
                "feed_id 与 xsec_token 来自 xhs_search/xhs_list_feeds 的返回。",
    unit="xiaohongshu",
)
def xhs_feed_detail(feed_id: str, xsec_token: str = "") -> Dict[str, Any]:
    """笔记详情+评论（只读）。

    Args:
        feed_id: 笔记 id（搜索/推荐流返回）。
        xsec_token: 随笔记返回的访问令牌（非账号秘钥）。
    """
    feed_id = (feed_id or "").strip()
    if not feed_id:
        return _err("feed_id 为空")
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))
    ret = a._mcp_call("get_feed_detail", {"feed_id": feed_id, "xsec_token": (xsec_token or "").strip()})
    data, err = _mcp_data(ret)
    if err:
        return _err(err)
    # 实盘结构（2026-11-08 对参）：{"feed_id", "data": {note: {...}, comments: [...]}}
    base = data if isinstance(data, dict) else {}
    if isinstance(base.get("data"), dict):
        base = base["data"]
    note = _normalize_note(base.get("note") or base)
    comments = []
    for c in _extract_comments(base):
        if isinstance(c, dict):
            user = c.get("userInfo") or c.get("user") or {}
            comments.append({
                "comment_id": c.get("comment_id") or c.get("id") or "",
                "nickname": user.get("nickname") or user.get("nickName") or c.get("nickname") or "",
                "content": str(c.get("content") or c.get("text") or "")[:200],
                "like": c.get("likeCount") or c.get("likes"),
                "ip_location": c.get("ipLocation") or "",
                "sub_comments": int(c.get("subCommentCount") or 0) or len(c.get("subComments") or []),
            })
        else:
            comments.append({"raw": str(c)[:150]})
    return _ok({**note, "comments": comments, "comment_count": len(comments)})
