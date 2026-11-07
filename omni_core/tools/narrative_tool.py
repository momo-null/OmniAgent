"""narrative_search / narrative_read —— 自传体叙事的检索与读取端（plan §13.3，NP-1）。

叙事资产 = 日记 / 月记 / 关系卡（第一人称经历叙事，拟人载体）。检索返回**条目**
（指针 + 摘要 + 场合标注，INV-12 不含正文）；正文由 narrative_read 按需读取，
文件仍是唯一真相。``read_scene`` 不扩叙事（§13.3.5：物理隔离，扩了会触碰 R4
全文函数白名单）。

出口约束全部落**工具层**（§6.5，INV-8）：
- 外部来源（他人可达通道）：``relation`` 只返回 summary、正文置空；
  跨场合 ``diary/month`` 返回全文但带 ``cross_context=True``；
- 稳定段的「叙事内容不得直接转述到对外消息」规则（NP-2 落 instructions）与此配套；
- 内部轮（Web / 任务 / cron）全库可读。

纯读工具：不声明 risk（S1 漏斗照常生效）；配额与计数走 memory_tool 统一账本
（N-5：七工具合计 ≤3 次/run，narrative_* ≤2 次/run）。
"""
from __future__ import annotations

from typing import Any, Dict

from omni_core.tools.base import function_tool
from omni_core.tools.memory_tool import _brain, _brain_cfg, _is_external_source, \
    _quota_block, note_self_fetch


def _current_context_key() -> str:
    """当前场合键来自 run 级信号（边界层写入），内核不读通道句柄。"""
    try:
        from omni_core.local.loop import context_signal as _cs
        return str((_cs.current() or {}).get("context_key")
                   or _cs.boundary().get("context_key") or "")
    except Exception:
        return ""


@function_tool(
    name="narrative_search",
    description=("检索你自己的经历叙事（日记/月记/对人的印象卡）：被问「上次/那天/最近"
                 "我们/某个人」而你只有模糊印象时用，返回条目（日期/摘要/涉及的人），"
                 "不带正文；要读全文用 narrative_read。日期用 YYYY-MM-DD（本地日历日）。"),
)
def narrative_search(query: str = "", kind: str = "", date_from: str = "",
                     date_to: str = "", peer: str = "", context_key: str = "",
                     limit: int = 8) -> Dict[str, Any]:
    """检索叙事条目（不返回正文）。

    Args:
        query: 检索线索（关键词/短语；空 = 按日期区间浏览）。
        kind: diary(日记) | month(月记) | relation(印象卡)；空 = 全部。
        date_from / date_to: 本地日历日区间（含端点；空 = 不限）。
        peer: 对象（结构化 peer 键）；关于 TA 的叙事加权（不过滤）。
        context_key: 场合键；同场合标注的叙事加权（不过滤，跨场合照样可召回）。
        limit: 最多几条（1-20，缺省 8）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "记忆层未启用（runtime.knowledge.memory.enabled）"}
    blocked = _quota_block("narrative_search")
    if blocked:
        return blocked
    rows = b.search_narrative(query=(query or "").strip(),
                              kind=str(kind or "").strip().lower(),
                              date_from=str(date_from or "").strip(),
                              date_to=str(date_to or "").strip(),
                              peer=str(peer or "").strip(),
                              context_key=str(context_key or "").strip(),
                              limit=int(limit or 8) if str(limit or "").strip() else 8)
    note_self_fetch(len(rows), str(rows[0]["doc_id"]) if rows else "",
                    tool="narrative_search")
    external = _is_external_source()
    cur_chat = _current_context_key()
    out = []
    for r in rows:
        cross = bool(external and cur_chat and cur_chat not in (r.get("context_keys") or []))
        out.append({"doc_id": r["doc_id"], "kind": r["kind"], "date": r["date"],
                    "summary": r["summary"], "topics": r.get("topics") or [],
                    "score": r["score"], "context_keys": r.get("context_keys") or [],
                    "peers": r.get("peers") or [], "cross_context": cross,
                    **({"project": r["project_id"]} if r.get("project_id") else {})})
    return {"ok": True, "count": len(out),
            "note": ("按相关度排序；要读全文调 narrative_read(doc_id, kind)"
                     if out else "没有匹配的经历记录"),
            "results": out}


@function_tool(
    name="narrative_read",
    description=("读你自己一篇经历叙事的全文：日记（diary-YYYY-MM-DD.md）、月记"
                 "（diary-month-YYYY-MM.md）或对某人的印象卡（对象标识）。"
                 "doc_id 来自 narrative_search 的结果。"),
)
def narrative_read(doc_id: str = "", kind: str = "") -> Dict[str, Any]:
    """读叙事正文（文件是唯一真相；外部来源按出口约束降级，INV-8）。

    Args:
        doc_id: 条目 id（narrative_search 返回的 doc_id）。
        kind: diary | month | relation；空则按 doc_id 推断。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "记忆层未启用（runtime.knowledge.memory.enabled）"}
    blocked = _quota_block("narrative_read")
    if blocked:
        return blocked
    core = b.read_narrative(str(doc_id or "").strip(), kind=str(kind or "").strip().lower())
    if not core.get("ok"):
        return core
    note_self_fetch(1, str(core.get("doc_id") or ""), tool="narrative_read")
    external = _is_external_source()
    cur_chat = _current_context_key()
    context_keys = core.get("context_keys") or []
    cross = bool(external and cur_chat and cur_chat not in context_keys)
    text = str(core.get("text") or "")
    summary = str(core.get("summary") or "")
    result: Dict[str, Any] = {"ok": True, "kind": core["kind"], "doc_id": core["doc_id"],
                              "chars": len(text), "cross_context": cross}
    if external:
        if core["kind"] == "relation":
            # 外部来源 + 印象卡：只给 summary，正文置空（N-10，INV-8）
            result.update({"text": "", "summary": summary, "chars": 0,
                           "note": "外部场合只提供摘要，不外泄对人的印象原文"})
        else:
            # 跨场合日记/月记：全文可读，但带标记——叙事内容不得直接转述到对外消息
            result.update({"text": text,
                           "note": ("这是其他场合的经历（cross_context）：可借鉴分寸与话题，"
                                    "不得把这里的内容当当前场合的事直接说出口")
                                    if cross else ""})
            if cross:
                try:
                    from omni_core.tools import memory_tool as _mt
                    _mt._run_stats["narrative_cross"] = \
                        int(_mt._run_stats.get("narrative_cross") or 0) + 1
                except Exception:
                    pass
    else:
        result.update({"text": text, "summary": summary})
    return result
