"""小红书只读采集插件。

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

# 归一逻辑的唯一实现在 plugins/xiaohongshu/normalize.py：MCP 懒加载（mcp.json 的
# transform 钩子）也要用同一份，避免两份代码漂移。此处只别名导入，不做本地副本。
from plugins.xiaohongshu.normalize import (
    extract_comments as _extract_comments,
    extract_notes as _extract_notes,
    normalize_note as _normalize_note,
)

logger = get_logger("plugins.xiaohongshu")

_ADAPTER = None  # 懒构造单例（随通道配置）


def _adapter():
    """取通道只读客户端：只依赖宿主能力，不 import 具体通道实现。"""
    global _ADAPTER
    if _ADAPTER is None:
        from gateway.channel_gateway import channel_client

        client = channel_client("xiaohongshu")
        if client is None:
            raise RuntimeError("xiaohongshu 通道不可用（先配置 channels/xiaohongshu.yaml）")
        _ADAPTER = client
    return _ADAPTER


def _ok(data: Any, **extra) -> Dict[str, Any]:
    return {"ok": True, "data": data, **extra}


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": msg}


def _mcp_data(ret: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    if not ret.get("ok"):
        return None, str(ret.get("error", "MCP 调用失败"))
    return ret.get("data"), None


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
