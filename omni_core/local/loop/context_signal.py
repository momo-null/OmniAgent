"""``context_signal`` —— run 级结构化场合信号（plan personal-agent-memory §4.1，M1）。

Working 层一等输入：run 起始**单点构造一次**、run 内只读，序列化为
``<current-context>``（≤ ``context.context_chars`` 缺省 200 字符）进 system 稳定段。
该块是**运行时处境**（在哪个场合、跟谁、最近聊什么），不是记忆检索结果，也不构成
按通道开注入特例（§6.1）。

**channel 只提供结构、不提供文本**（F-4）：``context_key`` 是通道无关的结构化场合键
（取值由边界层归一，内核视为不透明字符串，INV-4）；发言人与话题词面从结构键与既有记忆推出，不额外调 LLM（N-12）。

自检三问：① 解决的是注意力（本轮什么更容易被想起），不是隔离；② 去掉它模型每轮
都被重新告知"你在哪个场合"或干脆没有场合线索；③ 不落盘为知识（run 级内存，§3.3）。
"""
from __future__ import annotations

from typing import Any, Dict, List

_CURRENT: Dict[str, Any] = {}
#: 边界层写入的结构（F-4：channel 只给结构；内核只读，不反向依赖 gateway）。
#: ``sent_count`` 是边界注入的取值函数，内核只调用不认识其实现。
_BOUNDARY: Dict[str, Any] = {}

#: 话题词面上限（§4.1：2-4 个短语、每个 ≤12 字）
_TOPICS_MAX = 4
_TOPIC_CHARS = 12
#: L0 兜底词面的取材窗口（条数）；词面 = 近窗高频 2 字滑窗（与检索分词口径一致）
_FALLBACK_MSGS = 40


def set_boundary(*, context_key: str = "", source_kind: str = "",
                 source_external: bool = False, peer: str = "",
                 session_id: str = "", sent_count=None) -> None:
    """run 起始由**边界层**（网关 / Web 入口）写入本轮结构，run 结束清空。

    内核只读 :func:`boundary`，不 import 通道层、不解析回发句柄（H-8 分层修正）。
    """
    _BOUNDARY.clear()
    _BOUNDARY.update({"context_key": str(context_key or ""),
                      "source_kind": str(source_kind or ""),
                      "source_external": bool(source_external),
                      "peer": str(peer or ""),
                      "session_id": str(session_id or ""),
                      "sent_count": sent_count})


def boundary() -> Dict[str, Any]:
    return dict(_BOUNDARY)


def build_run(task_id: str = "", objective: str = "", run_key: str = "",
              source_kind: str = "", context_key: str = "",
              peer: str = "") -> Dict[str, Any]:
    """run 起始构造一次（sdk_bridge 主链调用；子任务不构造也不渲染）。

    场合 / 对象 / 进料道来自 :func:`boundary`（边界层写入）或显式参数；
    内核不读通道句柄。任何异常静默降级为空信号，绝不阻断 run。
    """
    sig: Dict[str, Any] = {"context_key": "", "source_kind": "", "peer": "",
                           "session_id": "", "project_id": "", "recent_topics": [],
                           "tone": "", "run_id": ""}
    try:
        b = boundary()
        ck = str(context_key or b.get("context_key") or "")
        sk = str(source_kind or b.get("source_kind") or "")
        sig["context_key"] = ck
        sig["source_kind"] = sk or ("chat" if ck else "task")
        sig["peer"] = str(peer or b.get("peer") or "")
        if task_id:
            try:
                from omni_core.local.task_store import TaskStore

                meta = TaskStore.get(str(task_id)) or {}
                sig["session_id"] = str(meta.get("session_id") or "")
                sig["project_id"] = str(meta.get("project_id") or "")
            except Exception:
                pass
        sig["recent_topics"] = _recent_topics(sig.get("context_key") or "",
                                              sig.get("session_id") or "")
        sig["tone"] = _circle_tone(sig.get("context_key") or "")
        sig["run_id"] = f"{task_id}:{run_key}" if (task_id or run_key) else ""
        _CURRENT.clear()
        _CURRENT.update(sig)
    except Exception:
        pass
    return dict(_CURRENT)



def _recent_topics(context_key: str, session_id: str) -> List[str]:
    """最近话题词面（§4.1）：``circle_note.topics`` 优先，L0 近窗高频词面兜底。

    **不额外调 LLM**（N-12）；兜底词面 = 近窗消息的 2 字滑窗按出现消息数排序
    （与 ``_query_terms`` 同分词口径），高频泛词由 df 占比自然排除不做词表（INV-4）。
    """
    try:
        from omni_core import memory_brain

        if context_key:
            note = memory_brain.get_circle_note(context_key)
            topics = [t for t in (note or {}).get("topics") or [] if t]
            if topics:
                return topics[:_TOPICS_MAX]
        return _l0_surface_topics(context_key, session_id)
    except Exception:
        return []


def _circle_tone(context_key: str) -> str:
    """场合语气预期（§6.4：circle_note 只影响话题/语气字段；取不到为空）。"""
    try:
        from omni_core import memory_brain

        if context_key:
            return str((memory_brain.get_circle_note(context_key) or {}).get("tone") or "")
    except Exception:
        pass
    return ""


def _l0_surface_topics(context_key: str, session_id: str) -> List[str]:
    try:
        from omni_core import memory_brain

        rows = memory_brain.recent_l0_lines(context_key=context_key, session_id=session_id,
                                            limit=_FALLBACK_MSGS)
        if not rows:
            return []
        df: Dict[str, int] = {}
        latest_grams = memory_brain._query_terms(str(rows[0] or ""))
        first_pos: Dict[str, int] = {g: i for i, g in enumerate(latest_grams)}
        for text in rows:
            grams = set(memory_brain._query_terms(str(text or "")))
            for g in grams:
                df[g] = df.get(g, 0) + 1
        n = len(rows)
        # 出现在 ≥20% 消息里才算"共同在聊"；并列时按最新消息中的出现序（越靠前越像当下话头）
        cands = [(g, c) for g, c in df.items() if c >= max(2, int(n * 0.2))]
        cands.sort(key=lambda kv: (-kv[1], first_pos.get(kv[0], 10 ** 6), kv[0]))
        return [g for g, _ in cands[:_TOPICS_MAX]]
    except Exception:
        return []


def current() -> Dict[str, Any]:
    """本 run 的信号（只读副本；未构造 → 空字典）。"""
    return dict(_CURRENT)


def run_id() -> str:
    """本 run 的观测主键（§8.3：``task_id:run_key``；未构造返回空串）。"""
    return str(_CURRENT.get("run_id") or "")


def render(max_chars: int = 0) -> str:
    """序列化为 ``<current-context>`` 稳定段；超预算逐级丢弃 topics，再整体截断。

    空信号（无任何字段）→ 空串（零注入，逐字节不变）。**run 开始读一次**——
    渲染结果不缓存也只会被 system prompt 组装调用一次（run 内不变）。
    """
    try:
        if not max_chars:
            try:
                import config
                max_chars = int(config.get_config("context.context_chars", 200) or 200)
            except Exception:
                max_chars = 200
        sig = _CURRENT
        context = str(sig.get("context_key") or "")
        peer = str(sig.get("peer") or "")
        source = str(sig.get("source_kind") or "")
        project = str(sig.get("project_id") or "")
        tone = str(sig.get("tone") or "")
        topics = [str(t)[:_TOPIC_CHARS] for t in (sig.get("recent_topics") or []) if t]
        # 空信号零注入：没有任何**场合内容**（场合键/对象/话题/语气）时不渲染——
        # cron/任务 run 的 source=task 单独不构成处境（避免纯标签噪音进稳定段）
        if not (context or peer or topics or tone):
            return ""
        lines = [f"context={context}" if context else "",
                 f"source={source}" if source else "",
                 f"peer={peer}" if peer else "",
                 f"project={project}" if project else "",
                 f"tone={tone}" if tone else "",
                 "topics=" + "、".join(topics) if topics else ""]
        block = "<current-context>\n" + "\n".join(
            [ln for ln in lines if ln]) + "\n</current-context>"
        # 超预算：先丢 topics（从尾部），再硬截——200 字符红线优先于信息量
        while len(block) > max_chars and topics:
            topics = topics[:-1]
            lines[-1] = "topics=" + "、".join(topics) if topics else ""
            body = [ln for ln in lines if ln]
            block = "<current-context>\n" + "\n".join(body) + "\n</current-context>"
        if len(block) > max_chars:
            block = block[:max_chars]
        return block
    except Exception:
        return ""
