"""观察见闻原文检索（P5 消费端）：按 ``source_kind=observation`` 查 L0。

与 ``conversation_search`` 同表分道（INV-13）；结果只作追溯，不冒充客观事实
（INV-14 / H-5）。配额走同一本七工具账本。
"""
from __future__ import annotations

from typing import Any, Dict

from omni_core.tools.base import function_tool
from omni_core.tools.memory_tool import _brain, _quota_block, note_self_fetch


@function_tool(
    name="observation_search",
    description=("按关键词检索你自己主动看过/查到的见闻原文（逐字，不是提炼后的记忆摘要）。"
                 "需要回忆「之前看到过什么内容」时用；结果不得当作客观事实转述。"),
)
def observation_search(keyword: str = "", days: int = 30,
                       limit: int = 8) -> Dict[str, Any]:
    """检索见闻原文。

    Args:
        keyword: 关键词（2-4 字最有效）。
        days: 回溯天数（1-365，缺省 30）。
        limit: 最多几条（1-20，缺省 8）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "记忆层未启用（runtime.knowledge.memory.enabled）"}
    blocked = _quota_block("observation_search")
    if blocked:
        return blocked
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword 不能为空"}
    try:
        n = max(1, min(20, int(limit or 8)))
    except Exception:
        n = 8
    rows = b.search_l0(kw, days=days, limit=n, source_kind="observation")
    note_self_fetch(len(rows), str(rows[0]["id"]) if rows else "", tool="observation_search")
    return {"ok": True, "count": len(rows),
            "note": "见闻原文只作追溯，不得冒充客观事实（INV-14 / H-5）",
            "results": [f"[{str(r['when'])[:16].replace('T', ' ')}] {r['text']}"
                        for r in rows]}
