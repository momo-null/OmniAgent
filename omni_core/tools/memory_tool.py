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
        from omni_core.brain import router as model_router

        # 端点真源收敛（2026-11-03）：与主链路同源，端点来自 models.json main 槽位；
        # config.brain 只提供引擎参数（旧端点键不再被读取）。
        src = (config.load_config() or {}).get("brain", {}) or {}
        cfg = model_router.resolve_main_with_engine(src)
        return cfg if (cfg.get("base_url") and cfg.get("model")) else {}
    except Exception:
        return {}


def _persona() -> str:
    try:
        return str(_brain()._cfg("persona") or "")
    except Exception:
        return ""


#: P1 观测（2026-11-02）：本 run 的记忆自取行为簿记。dispatch 是单 worker 串行
#: 执行、网关回发句柄同款进程级单值——run 开始由 sdk_bridge 清零，run 收尾取走
#: 并落 memory_recall.jsonl（「该查而没查」由此对出）。
#: §8.3（2026-11-06）：扩 by_tool（七工具分工具计数）/ context_hits / narrative_cross；
#: 配额（N-5）只在 run 作用域内强制（``active`` 由 reset_recall_stats 置位——
#: 直调工具的测试/脚本不受配额影响，真实 run 恒受限）。
_FETCH_QUOTA = 3        # 七工具合计 ≤3 次 / run
_NARRATIVE_QUOTA = 2    # narrative_* ≤2 次 / run
_run_stats: Dict[str, Any] = {"calls": 0, "hits": 0, "top1_id": "", "by_tool": {},
                              "context_hits": 0, "narrative_cross": 0, "active": False}


def reset_recall_stats() -> None:
    _run_stats.update({"calls": 0, "hits": 0, "top1_id": "", "by_tool": {},
                       "context_hits": 0, "narrative_cross": 0, "active": True})


def take_recall_stats() -> Dict[str, Any]:
    st = dict(_run_stats)
    st["by_tool"] = dict(_run_stats.get("by_tool") or {})
    return st


def quota_left() -> Dict[str, int]:
    """剩余配额（工具侧统一记账；run 作用域外恒视为满额）。"""
    if not _run_stats.get("active"):
        return {"total": _FETCH_QUOTA, "narrative": _NARRATIVE_QUOTA}
    return {"total": max(0, _FETCH_QUOTA - int(_run_stats.get("calls") or 0)),
            "narrative": max(0, _NARRATIVE_QUOTA
                             - sum(int(v) for k, v in (_run_stats.get("by_tool") or {}).items()
                                   if str(k).startswith("narrative_")))}


def _quota_block(tool: str) -> Dict[str, Any]:
    """超配额的统一拒绝文案（N-5；规则文本与稳定段同口径）。"""
    left = quota_left()
    if left["narrative"] <= 0 and str(tool).startswith("narrative_"):
        return {"ok": False,
                "error": f"本轮叙事检索配额已用完（narrative_* ≤{_NARRATIVE_QUOTA} 次/run）"}
    if left["total"] <= 0:
        return {"ok": False,
                "error": f"本轮自取过往的配额已用完（七工具合计 ≤{_FETCH_QUOTA} 次/run）——"
                         "先消化已拿到的内容，不要反复换关键词硬查"}
    return {}


def note_self_fetch(hits: int = 0, top1_id: str = "", tool: str = "") -> None:
    """把一次「自取过往」的工具调用并入本 run 账本。

    P2 起《记忆使用规则》的 ≤3 次合计口径覆盖 memory_recall / conversation_search /
    msg_group_history(keyword)——观测口径与规则口径必须同一个数，否则「检索使用率」
    指标只统计其中一条通道，读起来像模型不查。§8.3：``tool`` 分工具计数（by_tool），
    覆盖全部七个自取工具。
    """
    _run_stats["calls"] += 1
    tool = str(tool or "").strip() or "unknown"
    by_tool = _run_stats.setdefault("by_tool", {})
    by_tool[tool] = int(by_tool.get(tool) or 0) + 1
    if int(hits or 0) > 0:
        _run_stats["hits"] += int(hits)
        if not _run_stats["top1_id"] and top1_id:
            _run_stats["top1_id"] = str(top1_id)


def _run_source() -> str:
    """本轮来源（观测字段 T0/T1/private/api）：读网关回发句柄，与进程级单值同机制。"""
    try:
        from gateway.channels import context as _ch
        reply = _ch.get_current_reply()
    except Exception:
        return "api"
    if reply is None:
        return "api"
    chat = str(getattr(reply, "chat_id", "") or "")
    if chat.startswith("private"):
        return "private"
    if chat.startswith("group"):
        wo = getattr(getattr(reply, "_adapter", None), "_wake_origin", None) or {}
        hit = wo.get(chat)
        return "T0" if hit and str(hit[0]) == "named" else "T1"
    return str(getattr(reply, "channel_name", "") or "channel")


def log_recall_run(project: str = "", context_key: str = "", source_kind: str = "",
                   source_external: bool = False, injected_context: bool = False,
                   context_chars: int = 0, sent: bool = False,
                   llm_calls_delta: int = 0) -> None:
    """run 收尾：把本 run 的自取行为落一行观测记录（fail-soft）。

    §8.3 schema：主键 ``run_id = task_id:run_key``（context_signal 构造）；``context_key``
    真实值只落本机；按月分片 ``memory_recall-YYYY-MM.jsonl``。
    """
    try:
        from omni_core import memory_brain
        from omni_core.local.loop import context_signal as _cs

        st = take_recall_stats()
        sig = _cs.current() or {}
        memory_brain.log_recall_observability(
            project=project, source=_run_source(),
            run_id=str(sig.get("run_id") or ""),
            context_key=str(context_key or sig.get("chat_id") or ""),
            source_kind=str(source_kind or sig.get("source_kind") or ""),
            source_external=bool(source_external),
            injected_context=bool(injected_context),
            context_chars=int(context_chars or 0),
            by_tool=st.get("by_tool") or {},
            calls=int(st.get("calls") or 0), hits=int(st.get("hits") or 0),
            top1_id=str(st.get("top1_id") or ""),
            context_hits=int(st.get("context_hits") or 0),
            narrative_cross=int(st.get("narrative_cross") or 0),
            sent=bool(sent), llm_calls_delta=int(llm_calls_delta or 0))
    except Exception:
        pass


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
        k: 返回条数上限（1-5，与注入侧 top_k 同约束）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "长期记忆未启用（ltm.enabled）"}
    blocked = _quota_block("memory_recall")
    if blocked:
        return blocked
    # M2（plan §6.2）：场合与对象线索接通——peer 取本 run 的 context_signal（缺省
    # 从 query 解析），chat_id 取当前聊天窗口；两者都只**加权不过滤**（INV-3）。
    chat, _sid = _current_conversation()
    peer = ""
    try:
        from omni_core.local.loop import context_signal as _cs
        peer = str((_cs.current() or {}).get("peer") or "")
    except Exception:
        peer = ""
    if not peer:
        peer = b.extract_peer(query or "")
    hits = b.search_brain((query or "").strip(), persona=_persona(),
                          k=max(1, min(5, int(k or 5))),
                          reinforce=True, peer=peer, chat_id=chat)
    note_self_fetch(len(hits), str(hits[0]["record_id"]) if hits else "",
                    tool="memory_recall")
    _run_stats["context_hits"] = int(_run_stats.get("context_hits") or 0) + \
        sum(1 for h in hits if h.get("ctx_hit"))
    return {"ok": True, "hits": [{"record_id": h["record_id"], "content": h["content"],
                                  "kind": h.get("kind", "")} for h in hits]}


#: 当前 run 绑定的 task_id（conversation_search 据此解析「当前会话」；sdk_bridge 每次 run 注入，
#: 与 scene_tool.set_scene_task_context / reset_recall_stats 同款进程级单值机制）。
_l0_task_id: Dict[str, str] = {"value": ""}


def set_l0_task_context(task_id: str) -> None:
    _l0_task_id["value"] = task_id or ""


def _current_conversation() -> tuple:
    """当前聊天窗口的两个键 (chat_id, session_id)——取不到留空，由调用方决定范围。"""
    chat = ""
    try:
        from gateway.channels import context as _ch
        reply = _ch.get_current_reply()
        if reply is not None:
            chat = str(getattr(reply, "chat_id", "") or "")
    except Exception:
        chat = ""
    sid = ""
    tid = _l0_task_id.get("value") or ""
    if tid:
        try:
            from omni_core.local.task_store import TaskStore
            sid = str((TaskStore.get(tid) or {}).get("session_id") or "")
        except Exception:
            sid = ""
    return chat, sid


def _is_external_source() -> bool:
    try:
        from gateway.channels import context as _ch
        return bool(_ch.get_source_external())
    except Exception:
        return False


@function_tool(
    name="conversation_search",
    description=("按关键词检索原始聊天记录（逐字，含群聊/私聊/你自己说的话，不是提炼后的记忆摘要）。"
                 "问到具体的人、时间、旧约定、原话时用；memory_recall 只有摘要或查不到时用。"
                 "keyword 用 2-4 字关键词最有效，scope=current 只查当前会话/群，all 查全部会话。"),
)
def conversation_search(keyword: str = "", scope: str = "current",
                        days: int = 30, limit: int = 8) -> Dict[str, Any]:
    """检索 L0 原始聊天记录。

    Args:
        keyword: 关键词（人名/昵称/数字/名词片段，2-4 字最有效）。
        scope: current(当前会话或群) | all(全部会话；外部来源不支持)。
        days: 回溯天数（1-365，缺省 30）。
        limit: 最多几条（1-20，缺省 8）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "记忆层未启用（runtime.knowledge.memory.enabled）"}
    blocked = _quota_block("conversation_search")
    if blocked:
        return blocked
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "error": "keyword 不能为空"}
    chat, sid = _current_conversation()
    scoped = str(scope or "current").strip().lower() != "all" or _is_external_source()
    if scoped and not (chat or sid):
        # 外部来源（他人可达）越权口子只有这一处：定不到当前会话就拒绝，绝不放成全局检索
        return {"ok": False, "error": "无法确定当前会话，请改用 scope=all（仅内部来源可用）"}
    try:
        n = max(1, min(20, int(limit or 8)))
    except Exception:
        n = 8
    rows = b.search_l0(kw, chat_id=chat if scoped else "", session_id=sid if scoped else "",
                       days=days, limit=n)
    note_self_fetch(len(rows), str(rows[0]["id"]) if rows else "", tool="conversation_search")
    return {"ok": True,
            "scope": "current" if scoped else "all",
            "count": len(rows),
            "note": ("按时间正序；[时间] 谁：原话" if rows else "没有匹配的聊天记录"),
            "results": [f"[{str(r['when'])[:16].replace('T', ' ')}] "
                        f"{r['who'] or ('我' if r['role'] == 'assistant' else '对方')}："
                        f"{r['text']}" for r in rows]}


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
