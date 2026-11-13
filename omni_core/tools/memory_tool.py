"""记忆自管工具（LTM Phase 3）：agent 主动 remember/recall/update/forget/overview。

落盘权在内核（S1 红线不变）：工具提交意图，由 memory_brain 代理读写 brain.db。
启用条件：runtime.knowledge.memory.ltm.enabled（未启用时工具返回明确不可用）。
"""
from typing import Any, Dict, List

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


#: P1 观测（2026-11-02）：本 run 的记忆自取行为簿记。dispatch 是单 worker 串行
#: 执行、网关回发句柄同款进程级单值——run 开始由 sdk_bridge 清零，run 收尾取走
#: 并落 memory_recall.jsonl（「该查而没查」由此对出）。
#: §8.3（2026-11-06）：扩 by_tool（七工具分工具计数）/ context_hits / narrative_cross；
#: 配额（N-5）只在 run 作用域内强制（``active`` 由 reset_recall_stats 置位——
#: 直调工具的测试/脚本不受配额影响，真实 run 恒受限）。
_FETCH_QUOTA = 3        # 七工具合计 ≤3 次 / run
_NARRATIVE_QUOTA = 2    # narrative_* ≤2 次 / run
_run_stats: Dict[str, Any] = {"calls": 0, "hits": 0, "top1_id": "", "by_tool": {},
                              "context_hits": 0, "narrative_cross": 0, "active": False,
                              "evidence": 0, "bundle_taken": 0, "quota_blocked": 0}


def reset_recall_stats() -> None:
    _run_stats.update({"calls": 0, "hits": 0, "top1_id": "", "by_tool": {},
                       "context_hits": 0, "narrative_cross": 0, "active": True,
                       "evidence": 0, "bundle_taken": 0, "quota_blocked": 0})
    _BUNDLE["items"] = []


def take_recall_stats() -> Dict[str, Any]:
    st = dict(_run_stats)
    st["by_tool"] = dict(_run_stats.get("by_tool") or {})
    return st


def _quota_cfg() -> Dict[str, int]:
    """读侧自取配额（走配置；缺省 3 / 2，行为与旧常量一致）。"""
    try:
        import config
        return {
            "total": max(1, int(config.get_config(
                "runtime.knowledge.memory.read_quota.total", _FETCH_QUOTA) or _FETCH_QUOTA)),
            "narrative": max(0, int(config.get_config(
                "runtime.knowledge.memory.read_quota.narrative", _NARRATIVE_QUOTA) or 0)),
        }
    except Exception:
        return {"total": _FETCH_QUOTA, "narrative": _NARRATIVE_QUOTA}


def quota_left() -> Dict[str, int]:
    """剩余配额（工具侧统一记账；run 作用域外恒视为满额）。"""
    cfg = _quota_cfg()
    if not _run_stats.get("active"):
        return {"total": cfg["total"], "narrative": cfg["narrative"]}
    return {"total": max(0, cfg["total"] - int(_run_stats.get("calls") or 0)),
            "narrative": max(0, cfg["narrative"]
                             - sum(int(v) for k, v in (_run_stats.get("by_tool") or {}).items()
                                   if str(k).startswith("narrative_")))}


def _quota_block(tool: str) -> Dict[str, Any]:
    """超配额的统一拒绝文案（N-5；规则文本与稳定段同口径）。"""
    cfg = _quota_cfg()
    left = quota_left()
    if left["narrative"] <= 0 and str(tool).startswith("narrative_"):
        _run_stats["quota_blocked"] = int(_run_stats.get("quota_blocked") or 0) + 1
        return {"ok": False,
                "error": f"本轮叙事检索配额已用完（narrative_* ≤{cfg['narrative']} 次/run）"}
    if left["total"] <= 0:
        _run_stats["quota_blocked"] = int(_run_stats.get("quota_blocked") or 0) + 1
        return {"ok": False,
                "error": f"本轮自取过往的配额已用完（七工具合计 ≤{cfg['total']} 次/run）——"
                         "先消化已拿到的内容，不要反复换关键词硬查"}
    return {}


def note_self_fetch(hits: int = 0, top1_id: str = "", tool: str = "") -> None:
    """把一次「自取过往」的工具调用并入本 run 账本。

    P2 起《记忆使用规则》的 ≤3 次合计口径覆盖 memory_recall / conversation_search /
    自取类工具（以实际注册表为准）——观测口径与规则口径必须同一个数，否则「检索使用率」
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


def _bundle_cfg() -> Dict[str, Any]:
    """读侧候选包参数（全部走配置：内核零词表、零场合键分支）。"""
    p = "runtime.knowledge.memory.read_bundle"
    try:
        import config
        return {
            "enabled": bool(config.get_config(p + ".enabled", True)),
            "total_cap": max(1, int(config.get_config(p + ".total_cap", 2) or 2)),
            "item_chars": max(20, int(config.get_config(p + ".item_chars", 60) or 60)),
            "l1_min_score": float(config.get_config(p + ".l1_min_score", 0.60) or 0.60),
            "narrative_min_score": float(
                config.get_config(p + ".narrative_min_score", 0.65) or 0.65),
            "l0_limit": max(0, int(config.get_config(p + ".l0_limit", 0) or 0)),
        }
    except Exception:
        return {"enabled": False, "total_cap": 2, "item_chars": 60,
                "l1_min_score": 0.60, "narrative_min_score": 0.65, "l0_limit": 0}


#: 本 run 的高置信候选包（run 级内存）。内容**只经 memory_recall 空 query 取用**，
#: 不进 system / 不进 items —— 检索结果永不自动注入（INV-4）。
_BUNDLE: Dict[str, Any] = {"items": []}


def build_candidate_bundle(query: str = "", context_key: str = "",
                           session_id: str = "", peer: str = "") -> int:
    """run 起始：把现有带分数的检索器的过线结果合成一个候选包。

    三通道都只用内核现有检索函数与结构参数，不使用词表、场合键前缀或平台名
    （INV-4 / R5）；阈值与条数全部来自 :func:`_bundle_cfg`。叙事通道额外要求
    词面与当轮输入相交，避免「按近因浏览」的固定几条被当成候选（实测噪声源）。
    返回候选条数并写入观测账本 ``evidence``；任何异常静默返回 0。
    """
    _BUNDLE["items"] = []
    cfg = _bundle_cfg()
    q = str(query or "").strip()
    if not cfg.get("enabled") or len(q) < 2:
        return 0
    b = _brain()
    try:
        if not b.enabled():
            return 0
    except Exception:
        return 0
    items: List[Dict[str, Any]] = []
    chars = int(cfg["item_chars"])
    ck, sid, pr = str(context_key or ""), str(session_id or ""), str(peer or "")
    try:
        for h in b.search_brain(q, k=3, reinforce=False, peer=pr, context_key=ck):
            if float(h.get("score") or 0) < cfg["l1_min_score"]:
                continue
            items.append({"src": "atom", "text": str(h.get("content") or "")[:chars],
                          "score": float(h.get("score") or 0),
                          "doc_id": str(h.get("record_id") or "")})
    except Exception:
        pass
    try:
        uniq = set(str(t) for t in b._query_terms(q) if t)

        def _face_ok(face: str) -> bool:
            """词面覆盖率闸（与 search_brain 的 glue 门槛同口径，零词表）。"""
            if not uniq:
                return True
            hit = sum(1 for t in uniq if t in face)
            return hit / len(uniq) >= 0.34

        for h in b.search_narrative(q, limit=3, peer=pr, context_key=ck,
                                    require_match=True):
            if float(h.get("score") or 0) < cfg["narrative_min_score"]:
                continue
            face = " ".join([str(h.get("summary") or ""),
                             " ".join(str(t) for t in (h.get("topics") or []))]).lower()
            if not _face_ok(face):
                continue
            items.append({"src": "narrative", "text": str(h.get("summary") or "")[:chars],
                          "score": float(h.get("score") or 0),
                          "doc_id": str(h.get("doc_id") or ""),
                          "kind": str(h.get("kind") or "")})
    except Exception:
        pass
    try:
        if cfg["l0_limit"]:
            terms = b._query_terms(q)
            if terms:
                for h in b.search_l0(str(terms[0]), limit=int(cfg["l0_limit"]),
                                     context_key=ck, session_id=sid):
                    items.append({"src": "raw", "text": str(h.get("text") or "")[:chars],
                                  "score": 0.0, "doc_id": ""})
    except Exception:
        pass
    items.sort(key=lambda x: -float(x.get("score") or 0))
    _BUNDLE["items"] = items[:int(cfg["total_cap"])]
    _run_stats["evidence"] = len(_BUNDLE["items"])
    return len(_BUNDLE["items"])


def take_candidate_bundle() -> List[Dict[str, Any]]:
    """一次取用（1 次/run，独立额度；不占七工具合计 ≤3 次/run 的深挖配额）。"""
    if not _BUNDLE["items"] or int(_run_stats.get("bundle_taken") or 0) >= 1:
        return []
    _run_stats["bundle_taken"] = 1
    return [dict(x) for x in _BUNDLE["items"]]


def bundle_size() -> int:
    return len(_BUNDLE["items"])


def _run_source() -> str:
    """本轮进料道（通用枚举）；结构由边界层写入，内核不解析通道句柄。"""
    try:
        from omni_core.local.loop import context_signal as _cs
        return str(_cs.boundary().get("source_kind")
                   or _cs.current().get("source_kind") or "unknown")
    except Exception:
        return "unknown"


def log_recall_run(project: str = "", context_key: str = "", source_kind: str = "",
                   source_external: bool = False, peer: str = "",
                   injected_context: bool = False,
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
            context_key=str(context_key or sig.get("context_key") or ""),
            source_kind=str(source_kind or sig.get("source_kind") or ""),
            source_external=bool(source_external),
            peer=str(peer or sig.get("peer") or ""),
            injected_context=bool(injected_context),
            context_chars=int(context_chars or 0),
            by_tool=st.get("by_tool") or {},
            calls=int(st.get("calls") or 0), hits=int(st.get("hits") or 0),
            top1_id=str(st.get("top1_id") or ""),
            context_hits=int(st.get("context_hits") or 0),
            narrative_cross=int(st.get("narrative_cross") or 0),
            sent=bool(sent), llm_calls_delta=int(llm_calls_delta or 0),
            evidence=int(st.get("evidence") or 0),
            bundle_taken=int(st.get("bundle_taken") or 0),
            quota_blocked=int(st.get("quota_blocked") or 0))
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
    rid = b.remember(text, kind=kind, importance=imp,
                     brain_cfg=cfg if cfg else None)
    if not rid:
        return {"ok": False, "error": "写入失败"}
    logger.info(f"memory_remember: {text[:60]}")
    return {"ok": True, "record_id": rid, "kind": kind}


@function_tool(
    name="memory_observe",
    description=("记一条观察见闻（客观看到的条目原文，带出处）。浏览/采集/检索时看到值得留存的"
                 "具体内容用它逐条记——进观察层，巩固日记会引用、日后可升格；"
                 "主观判断/约定/偏好用 memory_remember。"),
)
def memory_observe(text: str = "", source: str = "") -> Dict[str, Any]:
    """观察进料道（INV-13/H-7）：原文进观察层，不伪装成聊天原话。

    Args:
        text: 见闻原文（客观转述，一句到几句）。
        source: 出处说明（平台/帖子/栏目等）。缺出处不入库（H-7）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "长期记忆未启用（ltm.enabled）"}
    text = (text or "").strip()
    source = (source or "").strip()
    if not text:
        return {"ok": False, "error": "text 不能为空"}
    if not source:
        return {"ok": False, "error": "source 不能为空（缺出处不入库）"}
    chat, _sid = _current_conversation()
    rid = b.capture_observation(
        chat, text,
        provenance={"surface": "observe", "source": source[:160]},
        source_kind="observation",
        source_external=_is_external_source())
    if not rid:
        return {"ok": False, "error": "写入失败（观察是增强项，可继续当前任务）"}
    return {"ok": True, "record_id": rid}


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
    q = (query or "").strip()
    if not q:
        # 空 query = 取本轮候选证据（1 次/run，独立额度，不占深挖配额）。
        # 内容只在这一个工具返回里出现，不进 system/items（INV-4）。
        items = take_candidate_bundle()
        if not items:
            return {"ok": True, "hits": [], "candidates": [],
                    "note": "本轮没有高置信候选证据（或已取过）；不必再查"}
        return {"ok": True, "hits": [], "candidates": items,
                "note": "这些是线索不是结论，只供你自己参考；不必都用，也不要照抄到对外消息"}
    blocked = _quota_block("memory_recall")
    if blocked:
        return blocked
    # M2（plan §6.2）：场合与对象线索接通——peer 取本 run 的 context_signal（缺省
    # 从 query 解析），context_key 取当前场合；两者都只**加权不过滤**（INV-3）。
    chat, _sid = _current_conversation()
    peer = ""
    try:
        from omni_core.local.loop import context_signal as _cs
        peer = str((_cs.current() or {}).get("peer") or "")
    except Exception:
        peer = ""
    hits = b.search_brain((query or "").strip(),
                          k=max(1, min(5, int(k or 5))),
                          reinforce=True, peer=peer, context_key=chat)
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
    """当前场合的两个键 (context_key, session_id)——取不到留空，由调用方决定范围。"""
    chat = ""
    try:
        from omni_core.local.loop import context_signal as _cs
        chat = str((_cs.current() or {}).get("context_key")
                   or _cs.boundary().get("context_key") or "")
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
        from omni_core.local.loop import context_signal as _cs
        return bool(_cs.boundary().get("source_external") or False)
    except Exception:
        return False


def _is_public_context(chat: str) -> bool:
    """按结构键判断可见面：一对一会话之外都算公开面（定不到场合也按公开算）。

    一对一的可见面只有对话双方，再收窄到「当前会话」没有意义——问另一处说过什么
    本来就该查得到。多人场合才要防「一个人问、一群人看」，外部来源必须锁在当前场合。
    定不到场合时 fail-closed 按公开处理。
    """
    kind = str(chat or "").strip().lower().split(":", 1)[0]
    return kind != "private"


def _is_master_source() -> bool:
    """本轮发言人是否主人（复用 identity 的归一：裸 uid / ``name(uid)`` / 认领名单）。

    主人自己的提问不做范围限制——哪怕在多人场合问「他今天说了什么」，也该一次查全。
    外部门（``tools_enabled``）不参与判断，保持原样。
    """
    try:
        from omni_core.local import identity as _id
        from omni_core.local.loop import context_signal as _cs
        peer = str((_cs.boundary() or {}).get("peer") or "")
        if not peer:
            return False
        return _id.MASTER_KEY in _id.resolve_peer_uids(peer)
    except Exception:
        return False


@function_tool(
    name="conversation_search",
    description=("按关键词检索原始对话记录（逐字，含各场合与你自己说的话，不是提炼后的记忆摘要）。"
                 "问到具体的人、时间、旧约定、原话时用；memory_recall 只有摘要或查不到时用。"
                 "keyword 用 2-4 字关键词最有效；问「某个人说过什么/找这个人」时用 peer 填名字或标识，"
                 "此时 keyword 可留空。缺省查全部会话（各个场合都算），"
                 "不用为了跨场合去配 scope；只有公开面里的他人发言会自动收窄。"),
)
def conversation_search(keyword: str = "", scope: str = "all",
                        days: int = 30, limit: int = 8,
                        peer: str = "") -> Dict[str, Any]:
    """检索 L0 原始聊天记录。

    Args:
        keyword: 关键词（人名/昵称/数字/名词片段，2-4 字最有效；给了 peer 时可留空）。
        scope: all(缺省，全部会话) | current(只查当前场合)。
        days: 回溯天数（1-365，缺省 30）。
        limit: 最多几条（1-20，缺省 8）。
        peer: 只查这个人说过的话（名字/昵称/标识都可以；空 = 不限对象）。
    """
    b = _brain()
    if not b.enabled():
        return {"ok": False, "error": "记忆层未启用（runtime.knowledge.memory.enabled）"}
    blocked = _quota_block("conversation_search")
    if blocked:
        return blocked
    kw = (keyword or "").strip()
    who = (peer or "").strip()
    if not kw and not who:
        return {"ok": False, "error": "keyword 与 peer 至少给一个"}
    chat, sid = _current_conversation()
    # 场合隔离（不是安全边界）：默认全量，新增场合/新人不必配任何东西；只有
    # 「公开面 + 他人发言」才锁当前会话——这时答案是给多人看的，不该夹带别处的话。
    # 一对一会话与主人发言不受限（可见面只有当事人，主人有权看自己的记忆）。
    external = _is_external_source() and not _is_master_source()
    scoped = (_is_public_context(chat)
              and (external or str(scope or "all").strip().lower() != "all"))
    if scoped and not (chat or sid):
        return {"ok": False, "error": "外部来源必须能确定当前会话，否则不允许跨会话检索"}
    try:
        n = max(1, min(20, int(limit or 8)))
    except Exception:
        n = 8
    rows = b.search_l0(kw, context_key=chat if scoped else "", session_id=sid if scoped else "",
                       days=days, limit=n, speaker=who)
    note_self_fetch(len(rows), str(rows[0]["id"]) if rows else "", tool="conversation_search")
    return {"ok": True,
            "scope": "current" if scoped else "all",
            "count": len(rows),
            "note": ("按时间正序；[时间] 谁：原话" if rows else
                     ("本轮检索范围里没找到这个人的话——不等于没说过，"
                      "可换 peer / keyword，或把 days 调大" if who else
                      "本轮检索范围里没匹配到——不等于没发生过，"
                      "可换 keyword，或把 days 调大")),
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
    rid = b.update_memory(record_id, new_text)
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
    ok = b.forget(record_id, reason=(reason or "")[:200])
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
