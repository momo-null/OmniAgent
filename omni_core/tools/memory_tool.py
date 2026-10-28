"""记忆自管工具（LTM Phase 3）：agent 主动 remember/recall/update/forget/overview。

落盘权在内核（S1 红线不变）：工具提交意图，由 memory_brain 代理读写 brain.db。
启用条件：runtime.knowledge.memory.ltm.enabled（未启用时工具返回明确不可用）。
"""
from typing import Any, Dict

from omni_core.tools.base import function_tool
from utils import get_logger

logger = get_logger("tools.memory")


def _brain():
    from omni_core import memory_brain
    return memory_brain


def _brain_cfg() -> Dict[str, Any]:
    try:
        import config
        return dict(config.get_config("brain") or {})
    except Exception:
        return {}


def _persona() -> str:
    try:
        return str(_brain()._cfg("persona") or "")
    except Exception:
        return ""


@function_tool(
    name="memory_remember",
    description="把一条信息显式写入长期记忆（跨会话保留）。适合：用户明确说「记住这个」、"
                "确认的规则/偏好、重要事实。一次性操作和寒暄不要记。",
)
def memory_remember(text: str = "", kind: str = "fact") -> Dict[str, Any]:
    """显式写入长期记忆。

    Args:
        text: 要记住的事实/规则/偏好，一句陈述 ≤120 字。
        kind: fact(客观事实) | preference(用户偏好) | rule(规律/纪律) | episode(事件片段)。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "长期记忆未启用（ltm.enabled）"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "text 不能为空"}
    if kind not in ("fact", "preference", "rule", "episode"):
        kind = "fact"
    imp = 0.8  # agent 显式 remember 默认较高重要性（LLM 精调交巩固期）
    cfg = _brain_cfg()
    rid = b.remember(text, kind=kind, importance=imp, persona=_persona(),
                     brain_cfg=cfg if cfg else None)
    if not rid:
        return {"ok": False, "error": "写入失败"}
    logger.info(f"memory_remember: {text[:60]}")
    return {"ok": True, "record_id": rid, "kind": kind}


@function_tool(
    name="memory_recall",
    description="主动检索长期记忆（跨会话）。适合：需要回想起之前的事实/约定/偏好的时候。",
)
def memory_recall(query: str = "", k: int = 5) -> Dict[str, Any]:
    """按关键词/语义线索检索长期记忆，返回最相关的条目。

    Args:
        query: 检索线索（关键词或自然语言短语）。
        k: 返回条数上限（1-8）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "长期记忆未启用（ltm.enabled）"}
    hits = b.search_brain((query or "").strip(), persona=_persona(),
                          k=max(1, min(8, int(k or 5))), include_dormant=True,
                          reinforce=True)
    return {"ok": True, "hits": [{"record_id": h["record_id"], "content": h["content"],
                                  "kind": h.get("kind", "")} for h in hits]}


@function_tool(
    name="memory_update",
    description="更正一条已有记忆（旧条目作废并保留审计痕迹，新内容入库）。需要先 memory_recall 拿到 record_id。",
)
def memory_update(record_id: str = "", new_text: str = "") -> Dict[str, Any]:
    """更正已有记忆内容。

    Args:
        record_id: 要更正的记忆条目 ID（来自 memory_recall）。
        new_text: 更正后的完整内容。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "长期记忆未启用（ltm.enabled）"}
    new_text = (new_text or "").strip()
    if not record_id or not new_text:
        return {"ok": False, "error": "record_id 与 new_text 不能为空"}
    rid = b.update_memory(record_id, new_text, persona=_persona())
    if not rid:
        return {"ok": False, "error": "未找到该 record_id 或更新失败"}
    return {"ok": True, "record_id": rid}


@function_tool(
    name="memory_forget",
    description="主动遗忘一条记忆（作废但保留审计痕迹）。仅限：内容已被用户明确更正/确认失效。",
)
def memory_forget(record_id: str = "", reason: str = "") -> Dict[str, Any]:
    """主动遗忘一条记忆。

    Args:
        record_id: 要遗忘的记忆条目 ID（来自 memory_recall）。
        reason: 遗忘原因（写入审计痕迹）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "长期记忆未启用（ltm.enabled）"}
    if not record_id:
        return {"ok": False, "error": "record_id 不能为空"}
    ok = b.forget(record_id, reason=(reason or "")[:200], persona=_persona())
    return {"ok": bool(ok)} if ok else {"ok": False, "error": "未找到该 record_id"}


@function_tool(
    name="memory_overview",
    description="查看长期记忆总览（总量/分类/状态分布/最近记忆），用于自检。",
)
def memory_overview() -> Dict[str, Any]:
    """长期记忆总览。"""
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "长期记忆未启用（ltm.enabled）"}
    return {"ok": True, **b.overview_brain()}
