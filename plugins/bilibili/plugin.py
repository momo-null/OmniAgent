"""B站只读采集插件。

- **零写操作**：全部工具只读。平台写路径单点收口在 gateway/channels/bilibili.py
  （四道闸），本插件永不注册任何发送/点赞/关注类工具——写纪律不靠自觉靠结构。
- 数据源复用通道适配器的 HTTP 客户端与 WBI 签名（cookie 来自 bilibili 通道配置，
  未登录也能用其中免凭证的读接口；未启用通道时工具返回明确错误）。
- 用途约定（写进 bilibili AGENTS.md）：觉醒时先 bili_hot / bili_search 找热点 →
  bili_video_info + bili_video_comments 先读再评；复盘时 bili_self_dynamics 看自己内容数据。

工具清单（只读）：
    bili_hot()                          热门视频榜（综合热门）
    bili_search(keyword, n)             关键词搜视频
    bili_video_info(bvid 或 aid)        视频元数据（标题/简介/UP主/数据）
    bili_video_comments(oid, type, n)   评论区（wbi main 接口）
    bili_self_dynamics(n)               自己的动态列表（含数据计数，复盘用）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from omni_core.tools.base import function_tool
from utils import get_logger

logger = get_logger("plugins.bilibili")

_ADAPTER = None  # 懒构造单例（随通道配置）


def _adapter():
    """按 bilibili 通道配置构造适配器（只用作读客户端，不 connect）。"""
    global _ADAPTER
    if _ADAPTER is None:
        try:
            from gateway.channel_gateway import channel_config
            from gateway.channels.bilibili import BilibiliAdapter

            _ADAPTER = BilibiliAdapter(channel_config("bilibili"))
        except Exception as e:  # 通道不存在/未启用 → 明确报错不猜
            raise RuntimeError(f"bilibili 通道不可用（先配置并启用 channels/bilibili.yaml）: {e}")
    return _ADAPTER


def _ok(data: Any, **extra) -> Dict[str, Any]:
    return {"ok": True, "data": data, **extra}


def _err(msg: str) -> Dict[str, Any]:
    return {"ok": False, "error": msg}


def _body(ret: Dict[str, Any]) -> Dict[str, Any]:
    body = (ret or {}).get("body") or {}
    code = body.get("code")
    if code != 0:
        return None, f"bilibili API code={code}: {body.get('message', '')}"
    return body.get("data") or {}, ""


def _v(card: Any) -> Dict[str, Any]:
    """热门/搜索卡片 → 统一摘要（标题/UP/数据/跳转参数）。"""
    stat = card.get("stat") or {}
    owner = card.get("owner") or {}
    return {
        "bvid": card.get("bvid", ""),
        "aid": card.get("aid"),
        "title": card.get("title", ""),
        "desc": (card.get("desc") or "")[:120],
        "owner": owner.get("name", ""),
        "mid": owner.get("mid"),
        "play": stat.get("view"), "danmaku": stat.get("danmaku"),
        "reply": stat.get("reply"), "like": stat.get("like"),
        "pubdate": card.get("pubdate"),
    }


@function_tool(
    description="获取B站当前热门视频榜（综合热门，只读）。用于社区话题采集："
                "看现在什么在火，再挑值得跟进的内容。返回标题/简介/UP主/播放点赞等数据。",
    unit="bilibili",
)
def bili_hot(n: int = 10) -> Dict[str, Any]:
    """热门视频榜（只读）。

    Args:
        n: 返回条数（1-20，缺省 10）。
    """
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))
    n = max(1, min(int(n or 10), 20))
    ret = a.wbi_get("https://api.bilibili.com/x/web-interface/popular", {"ps": 20, "pn": 1})
    data, err = _body(ret)
    if err:
        return _err(err)
    cards = (data.get("list") or [])[:n]
    return _ok([_v(c) for c in cards], count=len(cards))


@function_tool(
    description="在B站按关键词搜索视频（只读）。用于追踪社区话题/对标账号内容。"
                "返回标题/简介/UP主/数据。",
    unit="bilibili",
)
def bili_search(keyword: str, n: int = 10) -> Dict[str, Any]:
    """关键词搜视频（只读）。

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
    n = max(1, min(int(n or 10), 20))
    ret = a.wbi_get("https://api.bilibili.com/x/web-interface/wbi/search/type",
                    {"search_type": "video", "keyword": keyword, "page": 1, "page_size": n})
    data, err = _body(ret)
    if err:
        return _err(err)
    out = []
    for r in (data.get("result") or [])[:n]:
        if r.get("type") != "video":
            continue
        out.append({
            "bvid": r.get("bvid", ""), "aid": r.get("aid"),
            "title": str(r.get("title", "")).replace('<em class="keyword">', "").replace("</em>", ""),
            "desc": str(r.get("description", ""))[:120],
            "owner": r.get("author", ""), "mid": r.get("mid"),
            "play": r.get("play"), "like": r.get("like"), "reply": r.get("review"),
            "pubdate": r.get("pubdate"),
        })
    return _ok(out, count=len(out))


@function_tool(
    description="获取B站视频的元数据（标题/简介/UP主/播放点赞评论数，只读）。"
                "评论前先看这个了解视频讲了什么——先读再评。",
    unit="bilibili",
)
def bili_video_info(bvid: str = "", aid: int = 0) -> Dict[str, Any]:
    """视频元数据（只读）。

    Args:
        bvid: 视频 BV 号（bvid/aid 二选一）。
        aid: 视频 aid。
    """
    if not bvid and not aid:
        return _err("bvid/aid 至少给一个")
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))
    params = {"bvid": bvid} if bvid else {"aid": aid}
    ret = a._http("GET", "https://api.bilibili.com/x/web-interface/view", params=params)
    data, err = _body(ret)
    if err:
        return _err(err)
    return _ok(_v(data))


@function_tool(
    description="获取B站视频或动态的评论区（只读，前 n 条热评/最新）。"
                "评论前先读评论区在聊什么；采集语料时用它看社区说话方式。",
    unit="bilibili",
)
def bili_video_comments(oid: int, type: int = 1, n: int = 10) -> Dict[str, Any]:
    """评论区（只读）。

    Args:
        oid: 评论区 id（视频=aid，动态=dynamic_id）。
        type: 评论区类型（1=视频，17=动态，11=画片区）。
        n: 返回条数（1-20，缺省 10）。
    """
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))
    n = max(1, min(int(n or 10), 20))
    ret = a.wbi_get("https://api.bilibili.com/x/v2/reply/wbi/main",
                    {"type": int(type or 1), "oid": int(oid), "mode": 3, "ps": n})
    data, err = _body(ret)
    if err:
        if "404" in err or "啥都木有" in err:
            return _ok([], count=0, note="评论区暂无内容")
        return _err(err)
    out = []
    for r in (data.get("replies") or [])[:n]:
        out.append({
            "rpid": r.get("rpid"),
            "uname": (r.get("member") or {}).get("uname", ""),
            "mid": r.get("mid"),
            "message": (r.get("content") or {}).get("message", "")[:200],
            "like": r.get("like"),
            "rcount": r.get("rcount"),
        })
    return _ok(out, count=len(out), all_count=(data.get("cursor") or {}).get("all_count"))


@function_tool(
    description="获取B站视频的字幕转写（优先 CC/AI 字幕，带时间戳，只读）。"
                "这是「读懂视频内容」的正道：评论/采写前先看字幕，评论才言之有物。"
                "无字幕视频会明确返回提示（此时只能靠标题/简介/评论判断，别装懂）。",
    unit="bilibili",
)
def bili_video_subtitle(bvid: str = "", aid: int = 0, max_chars: int = 4000) -> Dict[str, Any]:
    """视频字幕转写（只读）。

    Args:
        bvid: 视频 BV 号（bvid/aid 二选一）。
        aid: 视频 aid。
        max_chars: 转写截断长度（缺省 4000 字符，带时间戳）。
    """
    if not bvid and not aid:
        return _err("bvid/aid 至少给一个")
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))

    # 1) view 拿 cid + 标题
    params = {"bvid": bvid} if bvid else {"aid": aid}
    ret = a._http("GET", "https://api.bilibili.com/x/web-interface/view", params=params)
    data, err = _body(ret)
    if err:
        return _err(err)
    cid = ((data.get("pages") or [{}])[0]).get("cid")
    title = data.get("title", "")
    if not cid:
        return _err("拿不到 cid（分P结构异常）")
    q = {"aid": data.get("aid"), "cid": cid}
    if bvid:
        q["bvid"] = bvid

    # 2) player wbi/v2 拿字幕轨（AI 字幕需登录态；未登录多半为空）
    pret = a.wbi_get("https://api.bilibili.com/x/player/wbi/v2", q)
    pdata, perr = _body(pret)
    subs = ((pdata or {}).get("subtitle") or {}).get("subtitles") or []
    if perr or not subs:
        return _ok({"title": title, "has_subtitle": False},
                   note="该视频无 CC/AI 字幕——只能靠标题/简介/评论区判断内容，别假装看过")

    # 3) 取第一轨（优先中文）拉字幕 JSON；URL 常见三种形态：https://、http://、协议相对 //（实盘 2026-11-08）
    subs_sorted = sorted(subs, key=lambda s: 0 if "zh" in str(s.get("lan", "")) else 1)
    sub_url = str(subs_sorted[0].get("subtitle_url") or "")
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    elif sub_url.startswith("http://"):
        sub_url = "https://" + sub_url[7:]
    if not sub_url:
        return _err("字幕轨存在但无下载地址")
    sret = a._http("GET", sub_url)
    sbody = (sret.get("body") or {})
    items = sbody.get("body") if isinstance(sbody, dict) else None
    if not items:
        return _err("字幕内容为空或结构异常")

    # 4) 带时间戳转写
    limit = max(500, min(int(max_chars or 4000), 20000))
    lines, total = [], 0
    for it in items:
        t = int(it.get("from") or 0)
        line = f"[{t // 60:02d}:{t % 60:02d}] {it.get('content', '')}"
        lines.append(line)
        total += len(line)
        if total >= limit:
            lines.append(f"…（转写超 {limit} 字符截断，共 {len(items)} 段字幕）")
            break
    return _ok({"title": title,
                "has_subtitle": True,
                "subtitle_lan": subs_sorted[0].get("lan", ""),
                "transcript": "\n".join(lines),
                "truncated": total >= limit})


@function_tool(
    description="获取自己的B站动态列表及其数据（播放/点赞/评论/转发数，只读）。"
                "用于运营复盘：看哪类内容数据好，调整选题。",
    unit="bilibili",
)
def bili_self_dynamics(n: int = 10) -> Dict[str, Any]:
    """自己的动态与数据（只读）。

    Args:
        n: 返回条数（1-20，缺省 10）。
    """
    try:
        a = _adapter()
    except RuntimeError as e:
        return _err(str(e))
    if not a.self_uid:
        return _err("未登录（bilibili 通道无凭证），查不了自己")
    n = max(1, min(int(n or 10), 20))
    ret = a._http("GET", "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/dynamic_new",
                  params={"uid": a.self_uid, "type_list": 268435455})
    data, err = _body(ret)
    if err:
        return _err(err)
    import json as _json

    out = []
    for c in (data.get("cards") or [])[:n]:
        d = c.get("desc") or {}
        card = {}
        try:
            card = _json.loads(c.get("card", "{}"))
        except Exception:
            pass
        item = card.get("item") or {}
        text = item.get("description") or item.get("content") or ""
        out.append({
            "dynamic_id": d.get("dynamic_id"),
            "type": d.get("type"),
            "text": str(text)[:120],
            "view": d.get("view"), "like": d.get("like"),
            "reply": d.get("comment"), "repost": d.get("repost"),
            "timestamp": d.get("timestamp"),
        })
    return _ok(out, count=len(out))
