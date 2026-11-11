"""记忆层（单一方案；现行架构见 doc/plans/implemented/memory-architecture.md）。

四层分工：
- L0 会话：``capture`` 收 task_store 落库的消息（进程内 buffer，任务收尾一次性提炼）；
- L1 原子记忆：``flush_brain`` 提炼 → 价值/kind/importance 两段判 → 一致性四态
  （novel/duplicate/merge/conflict）→ brain.db；``search_brain`` 融合排序
  （relevance × importance × recency，active-only，inject_floor）+ 召回即强化；
  ``inject_text_brain`` 出 ``<relevant-memories>`` 文本（仅供工具/调试；每轮自动注入
  已下线，见 implemented/memory-architecture.md §13.1）；
- L2 场景块：机制与触发在 ``local/scene_executor.py``（本模块只在收尾时调 ``maybe_maintain``）；
- L3 自我画像：``maintain_profile`` 由巩固周期驱动，把 brain.db 新 atoms 中**关于她
  自己**的事实增量重写进 ``self_profile.md``（读取在 local/knowledge_inject.py）；
  对话者（含主人）的画像归关系卡 ``maintain_relations``。

总开关 ``runtime.knowledge.memory.enabled``；参数在 ``runtime.knowledge.memory.ltm.*``。
自管工具经 tools/memory_tool.py 走同一落盘路径（S1 红线：agent 不直接碰文件）。

不变量：语义判断全 LLM、簿记全公式；记忆文件只由内核写；任何异常静默降级不阻断主流程。
遗忘状态机（dormant/archived）与 salience 衰减已删除（P7，2026-11-02）：``salience``
字段保留但恒等于 ``importance``；``tombstone`` 保留（被取代/人工遗忘，审计链）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from omni_core.local import llm_judge
from omni_core.local import runtime_paths as rp
from utils import get_logger

logger = get_logger("memory_brain")

_BRAIN_PATH = os.path.join(os.path.expanduser("~"), ".omniagent", "memory", "brain.db")
_ARCHIVE_DIR = os.path.join(os.path.expanduser("~"), ".omniagent", "memory", "archive")
_GATE = "runtime.knowledge.memory.enabled"   # 记忆层总开关（L1/L2/L3 同一门）

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS atoms(
  record_id   TEXT PRIMARY KEY,
  content     TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'fact',
  persona     TEXT DEFAULT '',
  origin      TEXT DEFAULT '',
  source_task TEXT DEFAULT '',
  importance  REAL NOT NULL DEFAULT 0.5,
  salience    REAL NOT NULL DEFAULT 0.5,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_access TEXT DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'active',
  superseded_by TEXT DEFAULT '',
  version     INTEGER NOT NULL DEFAULT 0,
  created     TEXT NOT NULL,
  updated     TEXT NOT NULL,
  peer        TEXT NOT NULL DEFAULT '',
  context_key TEXT NOT NULL DEFAULT '',
  source_kind TEXT NOT NULL DEFAULT 'dialogue',
  observed_at TEXT DEFAULT '',
  provenance  TEXT DEFAULT '',
  review_after TEXT DEFAULT ''
);
CREATE VIRTUAL TABLE IF NOT EXISTS atoms_fts USING fts5(
  content, record_id UNINDEXED, tokenize='{tokenizer}');
CREATE INDEX IF NOT EXISTS idx_atoms_state ON atoms(state, salience DESC);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# P2（2026-11-02，plan memory-recall §P2）：L0 逐字检索副本，对齐原仓库 ``l0_conversations``
# ——「写入时同步插一行到主库」，不是独立索引库。原会话 jsonl 继续作为文件真相，本表只是
# 检索副本（可重建）；``kind`` 区分通道消息与任务输入（两者同为 role=user，防检索串味）。
# 与原仓库的两处有意差异：① 多一列 ``session_id``——Web/API 会话没有 IM 群号，
# 靠它定位「当前会话」；② 不建 ``timestamp``（消息原始时间戳）——我们只把消息落盘一次，
# ``recorded_at`` 就是那个时间，再存一份空列是死字段。
# ``source_external`` 是边界层在 run 起始归一后盖上的**结构信号**（run 级
# context_signal.boundary()）：本行是否来自外部可达来源。内核只读写这个布尔列，
# 不解析任何场合键取值或前缀；信号随行落库，跨轮/跨进程仍可判定。
_L0_SCHEMA = """
CREATE TABLE IF NOT EXISTS l0_messages(
  record_id   TEXT PRIMARY KEY,
  context_key     TEXT NOT NULL DEFAULT '',
  session_id  TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'chat',
  role        TEXT NOT NULL DEFAULT 'user',
  speaker     TEXT NOT NULL DEFAULT '',
  text        TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  source_kind TEXT NOT NULL DEFAULT 'chat',
  provenance  TEXT DEFAULT '',
  source_external INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_l0_context_time ON l0_messages(context_key, recorded_at);
CREATE INDEX IF NOT EXISTS idx_l0_session ON l0_messages(session_id, recorded_at);
CREATE VIRTUAL TABLE IF NOT EXISTS l0_fts USING fts5(
  text, record_id UNINDEXED, tokenize='{tokenizer}');
"""

# Narrative 自传体叙事索引（plan personal-agent-memory §13.2.1）：**只存指针不存正文**
# （INV-12）——正文文件（diary-*.md / relations/<uid>.md）仍是唯一真相，本表只存
# 摘要/主题/场合标注/指针，供 narrative_search 检索。单一 upsert 通道
# ``upsert_narrative_index``（先删后插）；LLM 沙箱无该表写权限。
_NARRATIVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS narrative_index(
  doc_id     TEXT NOT NULL,
  kind       TEXT NOT NULL,
  project_id TEXT DEFAULT '',
  date_from  TEXT DEFAULT '',
  date_to    TEXT DEFAULT '',
  time_from  TEXT DEFAULT '',
  time_to    TEXT DEFAULT '',
  summary    TEXT DEFAULT '',
  topics     TEXT DEFAULT '',
  context_keys   TEXT DEFAULT '',
  peers      TEXT DEFAULT '',
  atmosphere TEXT DEFAULT '',
  date_basis TEXT DEFAULT '',
  updated    TEXT DEFAULT '',
  PRIMARY KEY(doc_id, kind)
);
CREATE VIRTUAL TABLE IF NOT EXISTS narrative_fts USING fts5(
  search_text, doc_id UNINDEXED, kind UNINDEXED, tokenize='{tokenizer}');
CREATE INDEX IF NOT EXISTS idx_narr_kind_date ON narrative_index(kind, date_from);
"""

# 圈层语境链（plan §4.2 / §5.2，M3）：context_buffer 按 context_key 累积通用信号（覆盖不
# 追加）；circle_note 一行一场合（**索引键，非物理分区**，INV-2）——语境预期（聊什么/
# 什么分寸/共同话题词面），不是可引用事实：永不进 atoms、不参与四态（INV-5）。
_CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_buffer(
  context_key   TEXT PRIMARY KEY,
  buffer    TEXT DEFAULT '',
  msg_count INTEGER DEFAULT 0,
  updated   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS circle_note(
  context_key        TEXT PRIMARY KEY,
  summary        TEXT DEFAULT '',
  topics         TEXT DEFAULT '',
  tone           TEXT DEFAULT '',
  source_count   INTEGER DEFAULT 0,
  source_kinds   TEXT DEFAULT '',
  prev_summary   TEXT DEFAULT '',
  version        INTEGER DEFAULT 0,
  msg_count      INTEGER DEFAULT 0,
  last_reflection TEXT DEFAULT '',
  updated        TEXT DEFAULT ''
);
"""

_LTM_DEFAULTS: Dict[str, Any] = {
    "top_k": 5,          # P1（2026-11-02）：8→5，对齐原仓库 maxResults；工具返回同受此约束
    "inject_floor": 0.30,
    "rank_weights": {"relevance": 0.55, "salience": 0.30, "recency": 0.15},
    "consolidation_every": 20,
}

_lock = threading.Lock()
_consolidation_lock = threading.Lock()          # worker 独占（不阻塞收尾）
_dispatch_lock = threading.Lock()               # 派发闸（防重复起 worker）
_dispatch_state: Dict[str, bool] = {"running": False}


# ---- 分词器 / 检索助手（trigram 缺省 | simple 扩展，wangfenjin/simple，MIT） ----
_SIMPLE_STATE: Dict[str, Any] = {"tok": None, "ext_dir": "", "dict_dir": "", "dict_set": False}


def _fts_tokenizer() -> str:
    """当前分词器（进程内解析一次）：simple（配置且扩展就位）| trigram（缺省/降级）。"""
    if _SIMPLE_STATE["tok"] is None:
        tok = "trigram"
        ext_dir = ""
        try:
            import config
            tok = str(config.get_config("runtime.knowledge.memory.fts_tokenizer", "trigram")
                      or "trigram").strip().lower()
            ext_dir = str(config.get_config("runtime.knowledge.memory.fts_ext_dir", "") or "").strip()
        except Exception:
            tok, ext_dir = "trigram", ""
        if tok == "simple":
            ext_dir = os.path.expanduser(ext_dir or os.path.join(
                os.path.expanduser("~"), ".omniagent", "ext", "simple"))
            dll = os.path.join(ext_dir, "simple.dll")
            dict_dir = os.path.join(ext_dir, "dict")
            if not (os.path.isfile(dll) and os.path.isdir(dict_dir)):
                tok = "trigram"
                ext_dir = ""
        if tok != "simple":
            ext_dir, dict_dir = "", ""
        else:
            dict_dir = os.path.join(ext_dir, "dict")
        _SIMPLE_STATE.update({"tok": tok, "ext_dir": ext_dir, "dict_dir": dict_dir})
    return _SIMPLE_STATE["tok"]


def _ensure_simple_ext(conn: sqlite3.Connection) -> bool:
    """连接内加载 simple 扩展 + 设置 jieba 词典（绝对路径，无需 chdir）。失败 False。"""
    try:
        if _fts_tokenizer() != "simple":
            return False
        conn.enable_load_extension(True)
        conn.load_extension(os.path.join(_SIMPLE_STATE["ext_dir"], "simple"))
        conn.execute("SELECT jieba_dict(?)", (_SIMPLE_STATE["dict_dir"],))
        _SIMPLE_STATE["dict_set"] = True
        return True
    except Exception:
        # 扩展加载失败：进程级降级 trigram（后续连接不再尝试 simple）
        _SIMPLE_STATE.update({"tok": "trigram", "ext_dir": "", "dict_dir": ""})
        return False


def _fts_ok(conn: sqlite3.Connection) -> bool:
    """确保 FTS 表存在且 schema 版本匹配（版本号含分词器，切换即重建+回填）；失败 → False。"""
    try:
        tok = _fts_tokenizer()
        ver = None
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='fts_schema_version'").fetchone()
            ver = row[0] if row else None
        except Exception:
            pass
        target = f"{tok}-2"   # -2（2026-11-03）：老库索引里存了陈旧副本，换版即重建回填
        if ver != target:
            conn.execute("DROP TABLE IF EXISTS atoms_fts")  # FTS5 无 ALTER,只能重建
            conn.execute(
                "CREATE VIRTUAL TABLE atoms_fts USING fts5("
                "content, record_id UNINDEXED, tokenize='" + tok + "')")
            # 重建即回填：无回填 = 重建后检索恒空（工程上踩过的坑）
            try:
                conn.execute("INSERT INTO atoms_fts(record_id, content) "
                             "SELECT record_id, content FROM atoms")
            except Exception:
                pass  # 全新库 atoms 尚无数据 → 空索引即正确
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('fts_schema_version',?)",
                         (target,))
            conn.commit()
        return True
    except Exception:
        return False


def _match_expr(conn: sqlite3.Connection, query: str) -> Tuple[Optional[str], List[str]]:
    """query → (MATCH 表达式或 None, LIKE 兜底词列表)。

    simple 模式：CJK 连串按 2 字滑窗短语 OR（simple 索引对 CJK 是单字级 token，
    短语=相邻单字序列，实测「买入点」→"买入"+"入点" 3 发 3 中）；ASCII 连串整词短语。
    trigram 模式：≥3 字符短语/3-gram OR + 2 字词 LIKE 兜底。
    """
    import re
    runs = re.findall(r"[0-9A-Za-z_㐀-鿿]+", query or "")
    if _fts_tokenizer() == "simple":
        frags: List[str] = []
        seen: set = set()

        def _push(term: str) -> None:
            if term and term not in seen:
                seen.add(term)
                frags.append('"' + term.replace('"', '""') + '"')

        for run in runs:
            if len(run) < 2:
                continue
            if run.isascii():
                _push(run)
            else:
                for i in range(len(run) - 1):
                    _push(run[i:i + 2])
        return (" OR ".join(frags) if frags else None), []
    # trigram 模式
    fts_terms: List[str] = []
    like_terms: List[str] = []
    for run in runs:
        if len(run) >= 3:
            if run.isascii() and len(run) <= 32:
                fts_terms.append(run)
            else:
                fts_terms.extend(run[i:i + 3] for i in range(min(len(run) - 2, 24)))
        elif len(run) == 2:
            like_terms.append(run)
    match = " OR ".join('"' + t.replace('"', '""') + '"' for t in fts_terms) if fts_terms else None
    return match, like_terms


def _search_conn(conn: sqlite3.Connection, query: str, k: int) -> List[Dict[str, Any]]:
    """裸字面召回（MATCH + LIKE 兜底，不做排序加权）：供核对/脚本用。"""
    if not _fts_ok(conn):
        return []
    match, like_terms = _match_expr(conn, query)
    out: Dict[str, Dict[str, Any]] = {}
    if match:
        try:
            rows = conn.execute(
                "SELECT record_id, content, bm25(atoms_fts) AS r FROM atoms_fts "
                "WHERE atoms_fts MATCH ? ORDER BY r LIMIT ?", (match, max(k * 3, 15))).fetchall()
            for rid, content, rank in rows:
                rel = -float(rank)
                score = rel / (1.0 + rel) if rel > 0 else 0.0
                out[str(rid)] = {"record_id": rid, "content": content, "score": round(score, 6)}
        except Exception:
            pass  # 奇形 token 触发 MATCH 语法错误 → 降级 LIKE，不吞掉整次检索
    seen = set(out.keys())
    for kw in like_terms:
        try:
            rows = conn.execute(
                "SELECT record_id, content FROM atoms WHERE content LIKE ? "
                "ORDER BY updated DESC LIMIT ?", (f"%{kw}%", max(k, 10))).fetchall()
        except Exception:
            continue
        for rid, content in rows:
            if str(rid) in seen:
                continue
            seen.add(str(rid))
            out[str(rid)] = {"record_id": rid, "content": content, "score": 0.0}
    results = list(out.values())
    results.sort(key=lambda d: d["score"], reverse=True)
    return results[:k]


def _fts_neighbors(conn: sqlite3.Connection, query: str, exclude_id: str,
                   limit: int = 5) -> List[Tuple[str, str]]:
    """FTS 近邻（全文作 query，active-only，排除自身）——巩固期去重候选（P5，2026-11-02）。

    取代旧的 ``content[:12]`` 精确短语探针：实测 33 条 active 候选命中 0/33，
    去重/合并/矛盾判定等于从未触发（plan memory-recall §2.4）。
    """
    if not _fts_ok(conn):
        return []
    match, like_terms = _match_expr(conn, query)
    out: List[Tuple[str, str]] = []
    seen = {str(exclude_id)}
    if match:
        try:
            rows = conn.execute(
                "SELECT f.record_id, a.content FROM atoms_fts f "
                "JOIN atoms a ON a.record_id = f.record_id "
                "WHERE atoms_fts MATCH ? AND a.state='active' AND f.record_id != ? "
                "ORDER BY bm25(atoms_fts) LIMIT ?",
                (match, str(exclude_id), limit)).fetchall()
            for r, c in rows:
                seen.add(str(r))
                out.append((str(r), str(c)))
        except Exception:
            pass
    for kw in like_terms:
        if len(out) >= limit:
            break
        try:
            rows = conn.execute(
                "SELECT record_id, content FROM atoms WHERE content LIKE ? "
                "AND state='active' AND record_id != ? LIMIT ?",
                (f"%{kw}%", str(exclude_id), limit)).fetchall()
        except Exception:
            continue
        for r, c in rows:
            if str(r) not in seen and len(out) < limit:
                seen.add(str(r))
                out.append((str(r), str(c)))
    return out[:limit]


_FLUSH_MIN_ROLES = 2        # buffer 少于此条不走收尾(无内容可收尾)
# buffer 上限（plan §3.3）：barge-in 频繁时（打断轮不 flush，且可能有并发通道）缓冲
# 会无限增长。超限即**滚动切段**——丢最旧的，只保留最近窗口，并记 dropped 计数。
# 之所以不是"超限就强制 flush"：flush 要跑 3 次 LLM，把它塞进消息路径等于把
# 尾延迟搬回主链路，正是本轮要去掉的东西。
_CAPTURE_MAX_MSGS = 80
_CAPTURE_MAX_CHARS = 24000
_buf_lock = threading.RLock()
_buffers: Dict[str, List[Dict[str, str]]] = {}  # pid -> [{role, content}]
_buf_dropped: Dict[str, int] = {}               # pid -> 被切掉的消息条数


def capture(project_id: str, role: str, content: str, context_key: str = "") -> None:
    """会话消息入 buffer(不提炼;提炼在任务收尾 flush_brain 时批量做)。

    ``context_key``（§4.3）：这条消息属于哪个场合——只作 atoms 的**来源标注**
    （「在哪儿听来的」线索，供加权与共同背景召回），不参与过滤（INV-3）。
    """
    if not enabled() or not project_id:
        return
    with _buf_lock:
        buf = _buffers.setdefault(project_id, [])
        buf.append({"role": role, "content": content, "context_key": str(context_key or "")})
        # 滚动切段：先按条数裁，再按字符裁（单条超长也吃得下，只要它在最新窗口里）
        dropped = 0
        while len(buf) > _CAPTURE_MAX_MSGS:
            buf.pop(0)
            dropped += 1
        while len(buf) > 1 and sum(len(m.get("content") or "") for m in buf) > _CAPTURE_MAX_CHARS:
            buf.pop(0)
            dropped += 1
        if dropped:
            _buf_dropped[project_id] = _buf_dropped.get(project_id, 0) + dropped


def buffer_stats(project_id: str) -> Dict[str, int]:
    """L0 缓冲状态（诊断面：证明切段确实在工作，而不是静默长胖）。"""
    with _buf_lock:
        buf = _buffers.get(project_id) or []
        return {"msgs": len(buf),
                "chars": sum(len(m.get("content") or "") for m in buf),
                "dropped": int(_buf_dropped.get(project_id, 0))}


def pop_capture(project_id: str) -> List[Dict[str, str]]:
    """取走 capture 缓冲（取走即清）。"""
    with _buf_lock:
        return _buffers.pop(project_id, [])


# ---- P2 · L0 逐字检索（写入即入表 + conversation_search 的查询端） ----------------
#
# 对齐原仓库 l0_conversations：会话消息落 jsonl 的**同时**往主库插一行，检索直接查这张
# 表。jsonl 仍是文件真相，表只是检索副本（删表重跑 backfill 即还原，无数据损失）。

_L0_TEXT_MAX = 600          # 单条索引文本上限（过程长文截断——检索要的是可定位，不是全文归档）
_L0_CONN: Dict[str, Any] = {"path": "", "conn": None}   # 进程级连接缓存（见 _l0_conn）


def _l0_id(session_id: str, recorded_at: str, role: str, text: str) -> str:
    """确定性主键：同一行重复入库（回填 × 写入并存）自动 INSERT OR IGNORE 去重。"""
    import hashlib
    return "l0_" + hashlib.sha1(
        f"{session_id}|{recorded_at}|{role}|{text}".encode("utf-8")).hexdigest()[:16]


def _l0_conn() -> sqlite3.Connection:
    """L0 读写用的进程级连接（路径变更即重开）。

    每条消息都 ``open_brain()`` 一次 = 每次重建 schema + 两次版本查询：实测回填 1161 行
    要 99 秒（85ms/行），实时写入也会压在消息路径上。连接复用后写入回到毫秒级。
    """
    if _L0_CONN["conn"] is None or _L0_CONN["path"] != _BRAIN_PATH:
        old = _L0_CONN["conn"]
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        _L0_CONN.update({"path": _BRAIN_PATH, "conn": open_brain()})
    return _L0_CONN["conn"]


def _resolve_source_external(explicit: Optional[bool]) -> bool:
    """归一「本行是否来自外部可达来源」的结构信号。

    边界层在 run 起始把结论写进 ``context_signal.set_boundary(source_external=...)``；
    内核只读这个布尔结构信号，不看任何场合键取值。调用方显式盖章时优先。
    """
    if explicit is not None:
        return bool(explicit)
    try:
        from omni_core.local.loop import context_signal as _cs
        return bool((_cs.boundary() or {}).get("source_external") or False)
    except Exception:
        return False


def index_l0_message(session_id: str, role: str, text: str, *, context_key: str = "",
                     kind: str = "chat", recorded_at: str = "",
                     source_kind: str = "", provenance: str = "",
                     speaker: str = "", source_external: Optional[bool] = None) -> str:
    """会话消息入 L0 检索表（写入即入表；任何异常静默——索引不打断主流程）。

    ``kind``：``chat``=通道消息，``task``=任务输入（cron/手动派发写进会话的那条 user 气泡）。
    ``source_kind``（§4.6 进料道，INV-13）：``chat`` / ``observation`` / ``task``——
    空则从 ``kind`` 推导（老调用语义不变）；观察原文必须经 ``capture_observation()``
    进入，不得伪装成聊天原话。``provenance`` 为结构化来源指针（不含真实标识）。
    ``source_external``：边界层归一后的**结构信号**（本行是否来自外部可达来源），
    由调用方盖章；未盖章时由 :func:`_resolve_source_external` 读 run 级结构信号，
    内核不解析场合键。返回 record_id；未启用/空文本/失败返回空串。
    """
    text = str(text or "").strip()
    if not text or not enabled() or not (session_id or context_key):
        return ""
    kind = kind if kind in ("chat", "task") else "chat"
    if source_kind not in ("chat", "observation", "task"):
        source_kind = "task" if kind == "task" else "chat"
    role = role if role in ("user", "assistant", "system") else "user"
    now = recorded_at or _now()
    # H-7：任何入口都不留空场合键；内核不假设渠道前缀，未给时以会话标识兜底。
    context_key = str(context_key or "").strip() or str(session_id)
    body = text[:_L0_TEXT_MAX]
    rid = _l0_id(str(session_id or ""), now, role, body)
    try:
        with _lock:
            conn = _l0_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO l0_messages(record_id, context_key, session_id, kind, "
                "role, speaker, text, recorded_at, source_kind, provenance, source_external) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (rid, str(context_key or ""), str(session_id or ""), kind, role,
                 str(speaker or ""), body, now,
                 source_kind, str(provenance or "")[:200],
                 1 if _resolve_source_external(source_external) else 0))
            if cur.rowcount:
                conn.execute("INSERT OR IGNORE INTO l0_fts(record_id, text) VALUES(?,?)",
                             (rid, body))
                conn.commit()
        return rid
    except Exception:
        return ""


def _format_provenance(value: Any) -> str:
    """结构化出处 → ``k=v|k=v`` 哈希串（不含真实账号 / URL 明文，由采集器填）。"""
    if isinstance(value, dict):
        return "|".join(f"{k}={value[k]}" for k in sorted(value)
                        if value[k] not in (None, ""))
    return str(value or "").strip()


def capture_observation(context_key: str, text: str, provenance: Any,
                        observed_at: str = "", source_kind: str = "observation",
                        source_external: Optional[bool] = None) -> str:
    """统一观察进料道**唯一入口**（§4.6 / §5.5，R8）：所有非对话来源（主动阅读 /
    检索结果 / 工具产出）都走它写入 ``l0_messages(source_kind=observation)``。

    - 观察不直接写 atoms（INV-6）：见闻进叙事层；出处/时效四字段只作追溯
      标注，不作升格门槛（INV-14 / H-5）；观察原文不进 ``conversation_search``
      （INV-13，``search_l0`` 只查 ``source_kind=chat``）。
    - ``context_key`` 是**一等通用场合键**（H-7）：取值由边界层归一，内核视为
      不透明字符串、不解析其语义（INV-4）；无别名、不双写、不迁移旧列。
    - ``provenance`` **必填**（H-7）：``platform/surface/actor/ref`` 哈希串或等价
      字典，内核只透传不解析；缺出处不入库，避免再次出现「来源全空」。
    - ``source_external``：边界层归一后的结构信号（本行是否来自外部可达来源），
      交给 :func:`_resolve_source_external` 归一后随行落库；内核**只读布尔、
      不推断任何场合键取值**。巩固期据此判断是否内化为事实。
    - 返回 record_id；未启用 / 空文本 / 失败返回空串（观察是增强项，静默）。
    """
    text = str(text or "").strip()
    if not text or not enabled() or not (context_key or "").strip():
        return ""
    prov = _format_provenance(provenance)
    if not prov:
        return ""
    if source_kind not in ("observation", "task"):
        source_kind = "observation"
    return index_l0_message("", "system", text, context_key=str(context_key),
                            kind="chat", recorded_at=observed_at or "",
                            source_kind=source_kind, provenance=prov,
                            source_external=_resolve_source_external(source_external))


def _l0_cutoff(days: Any) -> str:
    try:
        d = max(1, int(days))
    except Exception:
        d = 30
    return (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()


def search_l0(keyword: str, *, context_key: str = "", session_id: str = "",
              days: Any = 30, limit: int = 8, source_kind: str = "chat") -> List[Dict[str, Any]]:
    """L0 逐字检索（FTS + 时间窗）→ ``[{when, who, role, text, context_key, kind}]``，时间正序。

    范围（ACL 由调用方决定，本函数只认参数）：传 ``context_key`` / ``session_id`` = 只查该场合 / 会话
    （两个键取并集：实时写入行两个都有，回填行只有 session_id）；两者皆空 = 全部会话。
    ``source_kind``（INV-13）：缺省 ``chat``——「原始聊天记录」语义不含观察原文与
    任务输入；观察走 P5 的 ``observation_search``（同表按 ``source_kind=observation``）。
    """
    keyword = str(keyword or "").strip()
    if not keyword or not enabled():
        return []
    try:
        c = _l0_conn()
        if not _l0_fts_ok(c):
            return []
        match, like_terms = _match_expr(c, keyword)
        if not match and not like_terms:
            return []
        try:
            n = max(1, min(20, int(limit)))
        except Exception:
            n = 8
        cutoff = _l0_cutoff(days)
        # 范围条件：场合键与存储定位键取并集（部分来源只提供其中一个键）。
        cid = str(context_key or "")
        sid = str(session_id or "")
        k1 = cid or sid
        k2 = sid or k1
        scope, params = "", []
        if k1:
            scope = "AND (l.context_key = ? OR l.session_id = ?)"
            params = [k1, k2]
        sk = str(source_kind or "").strip().lower()
        sk_sql, sk_params = "", []
        if sk in ("chat", "observation", "task"):
            sk_sql = "AND l.source_kind = ?"
            sk_params = [sk]
        base = (f"SELECT l.record_id, l.context_key, l.session_id, l.kind, l.role, l.speaker, "
                f"l.text, l.recorded_at FROM l0_messages l "
                f"JOIN l0_fts f ON f.record_id = l.record_id "
                f"WHERE l0_fts MATCH ? AND l.recorded_at >= ? {scope} {sk_sql} "
                f"ORDER BY bm25(l0_fts) LIMIT ?")
        rows: List[Any] = []
        if match:
            try:
                rows = c.execute(base, tuple([match, cutoff] + params + sk_params
                                             + [max(n * 3, 20)])).fetchall()
            except Exception:
                rows = []
        out: List[Dict[str, Any]] = []
        seen: set = set()

        def _push(row: Any) -> None:
            rid = str(row[0])
            if rid in seen:
                return
            seen.add(rid)
            out.append({"id": rid, "when": row[7], "who": str(row[5] or ""), "role": row[4],
                        "text": row[6], "context_key": row[1], "kind": row[3]})

        for row in rows:
            _push(row)
        # LIKE 兜底：2 字词在 trigram 降级模式下 MATCH 无结果（同 atoms 的老坑）
        for kw in like_terms:
            lsql = ("SELECT l.record_id, l.context_key, l.session_id, l.kind, l.role, l.speaker, "
                    "l.text, l.recorded_at FROM l0_messages l WHERE l.text LIKE ? "
                    "AND l.recorded_at >= ? " + scope + " " + sk_sql
                    + " ORDER BY l.recorded_at DESC LIMIT ?")
            try:
                for row in c.execute(lsql, tuple([f"%{kw}%", cutoff] + params + sk_params
                                                 + [n])).fetchall():
                    _push(row)
            except Exception:
                continue
        out.sort(key=lambda d: str(d.get("when") or ""))
        return out[-n:]
    except Exception:
        return []


_l0_backfill_started = threading.Lock()
_l0_backfill_state: Dict[str, bool] = {"running": False}


def _cfg(key: str, default: Any = None) -> Any:
    """读 runtime.knowledge.memory.ltm.<key>，缺省合并内置默认。"""
    try:
        import config
        val = config.get_config(f"runtime.knowledge.memory.ltm.{key}", None)
        if val is None:
            return default if default is not None else _LTM_DEFAULTS.get(key)
        if isinstance(default, dict) and isinstance(val, dict):
            merged = dict(_LTM_DEFAULTS.get(key, {}))
            merged.update(val)
            return merged
        return val
    except Exception:
        return default if default is not None else _LTM_DEFAULTS.get(key)


def _ctx_cfg(key: str, default: Any = None) -> Any:
    """读 context.<key>（§6.2 场合/共同背景加权配置：w_ctx 0.25 / w_bg 0.10 等）。

    与 LTM 分节的原因：这两个键不属于单库参数，是检索侧的一维信号强度；
    键路径按 plan §12 定为 ``context.*``。
    """
    try:
        import config
        val = config.get_config(f"context.{key}", None)
        return default if val is None else val
    except Exception:
        return default


def enabled() -> bool:
    """记忆层总开关（L1/L2/L3 共用；缺省关）。"""
    try:
        import config
        return bool(config.get_config(_GATE, False))
    except Exception:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_since(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        return max(0.0, (datetime.now(timezone.utc)
                         - datetime.fromisoformat(ts)).total_seconds() / 86400.0)
    except Exception:
        return 0.0


#: H-7 schema 闸门：版本不符 = 旧库，直接重建（数据全丢弃，不迁移 / 不双写 / 不留别名）。
#: 这不是兼容层——不读旧行、不 ALTER 补列、不提供转换脚本，只做「丢掉重来」。
#: h9：l0_messages 增 source_external 结构信号列（列在 DDL，版本不符即重建）。
_SCHEMA_VERSION = "h9"
_BRAIN_TABLES = ("atoms_fts", "atoms", "l0_fts", "l0_messages",
                 "narrative_fts", "narrative_index", "context_buffer",
                 "circle_note", "meta")


def _brain_schema_is_current(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        return bool(row) and str(row[0]) == _SCHEMA_VERSION
    except Exception:
        return False


def _rebuild_brain_schema(conn: sqlite3.Connection) -> None:
    """旧库一律丢弃重建；同时让 L0 的进程级连接缓存失效，避免继续用旧句柄。"""
    for table in _BRAIN_TABLES:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        except Exception:
            pass
    conn.commit()
    old = _L0_CONN.get("conn")
    if old is not None and old is not conn:
        try:
            old.close()
        except Exception:
            pass
    _L0_CONN.update({"path": "", "conn": None})


def open_brain() -> sqlite3.Connection:
    """打开/初始化 brain.db（H-8：列全在 DDL，版本不符即重建空库，零兼容）。"""
    os.makedirs(os.path.dirname(_BRAIN_PATH), exist_ok=True)
    conn = sqlite3.connect(_BRAIN_PATH, check_same_thread=False)
    conn.executescript("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
    if not _brain_schema_is_current(conn):
        _rebuild_brain_schema(conn)
    tok = _fts_tokenizer()
    if tok == "simple":
        _ensure_simple_ext(conn)
    conn.executescript(_SCHEMA_V2.format(tokenizer=tok))
    conn.executescript(_L0_SCHEMA.format(tokenizer=tok))
    conn.executescript(_NARRATIVE_SCHEMA.format(tokenizer=tok))
    conn.executescript(_CONTEXT_SCHEMA)
    _l0_fts_ok(conn)
    _narr_fts_ok(conn)
    _meta_set(conn, "schema_version", _SCHEMA_VERSION)
    conn.commit()
    return conn






def _l0_fts_ok(conn: sqlite3.Connection) -> bool:
    """L0 全文索引就绪（分词器换版即重建 + 从 l0_messages 回填）；失败 False（检索降级空结果）。

    与 ``_fts_ok`` 同款：FTS5 无 ALTER，只能 DROP 重建；重建不回填 = 索引恒空（老坑）。
    """
    try:
        tok = _fts_tokenizer()
        ver = None
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='l0_fts_schema_version'").fetchone()
            ver = row[0] if row else None
        except Exception:
            pass
        target = f"{tok}-1"
        if ver != target:
            conn.execute("DROP TABLE IF EXISTS l0_fts")
            conn.execute("CREATE VIRTUAL TABLE l0_fts USING fts5("
                         "text, record_id UNINDEXED, tokenize='" + tok + "')")
            try:
                conn.execute("INSERT OR IGNORE INTO l0_fts(record_id, text) "
                             "SELECT record_id, text FROM l0_messages")
            except Exception:
                pass
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('l0_fts_schema_version',?)",
                         (target,))
            conn.commit()
        return True
    except Exception:
        return False


def _narr_fts_ok(conn: sqlite3.Connection) -> bool:
    """narrative_fts 就绪（分词器换版即重建 + 从 narrative_index 回填）；失败 False。

    与 ``_fts_ok`` 同款：FTS5 无 ALTER，只能 DROP 重建；重建不回填 = 索引恒空（老坑）。
    search_text = summary + topics + atmosphere（P6-1 可回味词面；**不存正文**，INV-12）。
    """
    try:
        tok = _fts_tokenizer()
        ver = None
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='narr_fts_schema_version'").fetchone()
            ver = row[0] if row else None
        except Exception:
            pass
        target = f"{tok}-2"
        if ver != target:
            conn.execute("DROP TABLE IF EXISTS narrative_fts")
            conn.execute("CREATE VIRTUAL TABLE narrative_fts USING fts5("
                         "search_text, doc_id UNINDEXED, kind UNINDEXED, "
                         "tokenize='" + tok + "')")
            try:
                rows = conn.execute(
                    "SELECT doc_id, kind, summary, topics, atmosphere "
                    "FROM narrative_index").fetchall()
                for doc_id, kind, summary, topics, atmosphere in rows:
                    tlist = _json_list(topics)
                    search_text = (str(summary or "") + " " + " ".join(tlist) + " "
                                   + str(atmosphere or "")).strip()
                    if search_text:
                        conn.execute("INSERT INTO narrative_fts(search_text, doc_id, kind) "
                                     "VALUES(?,?,?)", (search_text[:400], doc_id, kind))
            except Exception:
                pass  # 全新空库无行可回填
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('narr_fts_schema_version',?)",
                         (target,))
            conn.commit()
        return True
    except Exception:
        return False




# ---- 写链：flush → 两段判 → 落库 ----

def flush_brain(project_id: str, brain_cfg: Optional[Dict[str, Any]] = None,
                source_task: str = "",
                msgs: Optional[List[Dict[str, str]]] = None,
                allow_write: bool = True) -> Dict[str, int]:
    """任务收尾：语境链 → 关系卡 → L2 维护 → 巩固调度（公共尾段，见 ``_flush_tail``）。

    2026-11-10 起写链为「事件 → 助手的取舍 → 记忆」（worklog lifelike-presence §11/§12）：
    收尾**不再自动提炼 L1 原子**——L1 只走三条助手的选择路径：``memory_remember``（显式记）、
    巩固期 ``promote_observations``（内化助手自己记的观察）、episode→rule 抽象。
    防投毒与拟人取舍同源：世界可以灌进 L0，只有经过助手的手才变成「助手的」。

    Args:
        msgs: 已从 capture 缓冲取走的消息（异步写链在入队时取走，worker 后续消费）。
            ``None`` = 就地 pop（同步路径 / 测试）。
        allow_write: 本轮是否"真的干了活"（did_work），透传给 L2 维护。
    """
    stats: Dict[str, int] = {"stored": 0}
    msgs = pop_capture(project_id) if msgs is None else msgs
    if not enabled() or not brain_cfg or len(msgs) < _FLUSH_MIN_ROLES:
        return stats
    _flush_tail(project_id, brain_cfg, stats, allow_write, msgs=msgs)
    return stats


def _flush_tail(project_id: str, brain_cfg: Optional[Dict[str, Any]],
                stats: Dict[str, int], allow_write: bool,
                msgs: Optional[List[Dict[str, str]]] = None) -> None:
    """收尾公共尾段：L2 维护（受 did_work 门控）+ 巩固调度 + 语境链（M3）。

    三条收尾路径（白卷 / 全被判为一次性 / 正常落库）都走这里——把"该做的事"收成
    一处，避免某条分支忘了触发巩固（历史上白卷分支就漏过，语音会话因此攒不到巩固）。
    L2 的进料是"游标之后的 L1 原子增量"，所以这里不再往下传会话摘要。
    语境链（§5.2）：把本批消息按场合累积通用信号进 context_buffer，达到触发条件
    （条数/间隔）才跑一次 reflection——失败静默，绝不阻断收尾。
    """
    try:
        from omni_core.local import scene_executor
        scene_executor.maybe_maintain(project_id, brain_cfg,
                                      stats=stats, allow_write=allow_write)
    except Exception:
        pass
    try:
        chats: List[str] = []
        by_chat: Dict[str, List[str]] = {}
        for m in (msgs or []):
            cid = str((m or {}).get("context_key") or "").strip()
            if cid:
                by_chat.setdefault(cid, []).append(str(m.get("content") or ""))
        for cid, texts in by_chat.items():
            update_context_buffer(cid, texts, source_kind="chat")
            chats.append(cid)
        if chats:
            run_context_reflections(brain_cfg, chats=chats)
    except Exception:
        pass
    # 关系卡按 L0 直驱，与巩固周期解耦：不产 L1 的轮次同样能更新对人的印象。
    try:
        maybe_maintain_relations(brain_cfg)
    except Exception:
        pass
    maybe_consolidate(brain_cfg)


def _new_id() -> str:
    import uuid
    return f"m_{uuid.uuid4().hex[:12]}"


def _fts_upsert(conn: sqlite3.Connection, record_id: str, content: str) -> None:
    """``atoms_fts`` 的唯一写入通道：一条记忆在索引里**恒为一行**（先删后插）。

    为什么不能直接 ``INSERT OR REPLACE``：FTS5 虚表只有 rowid，``record_id`` 是
    UNINDEXED 普通列、没有唯一约束，``OR REPLACE`` 撞不到任何冲突 → 等价于**追加**。
    巩固期每次 merge 重写文本都留一份陈旧副本，检索的 ``JOIN atoms USING(record_id)``
    于是把同一条记忆返回 N 次（2026-11-03 实测：55 索引行 / 37 条 atoms，6 条 active
    各背 2-6 份旧文本，40 样本里 43% 的 top_k 槽位被陈旧副本占掉，且旧文本措辞与
    现行文本不同，12 字前缀去冗余挡不住）。所有写点统一走这里。
    """
    conn.execute("DELETE FROM atoms_fts WHERE record_id=?", (str(record_id),))
    conn.execute("INSERT INTO atoms_fts(record_id, content) VALUES(?,?)",
                 (str(record_id), content))


def _insert_atom(conn: sqlite3.Connection, rid: str, it: Dict[str, Any],
                 origin: str, source_task: str, now: str) -> None:
    """落一条 atom（四态裁决后的唯一写点）。

    §4.3 出处/时效列：``context_key`` 取 buffer 多数派（来源标注，不过滤）；
    ``source_kind`` 缺省 ``dialogue``；``observed_at``/``provenance``/
    ``review_after`` 只作可选追溯标注，不参与准入与打分（INV-14 / H-5）。
    P2/P4（死维度清理）：``persona`` 列冻结（写入恒空串、形参链已移除，
    §8.5 待补④）；``owner`` 列已随多租户遗产整体移除。
    """
    conn.execute("INSERT OR REPLACE INTO atoms(record_id, content, kind, persona, origin, "
                 "source_task, importance, salience, access_count, last_access, state, "
                 "version, created, updated, peer, context_key, source_kind, "
                 "observed_at, provenance, review_after) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, it["text"], it.get("kind", "fact"), "", origin or "",
                  source_task or "", float(it.get("importance", 0.5)),
                  float(it.get("importance", 0.5)), 0, "", "active", 1, now, now,
                  str(it.get("peer") or ""),
                  str(it.get("context_key") or ""), str(it.get("source_kind") or "dialogue"),
                  str(it.get("observed_at") or ""), str(it.get("provenance") or ""),
                  str(it.get("review_after") or "")))
    _fts_upsert(conn, rid, it["text"])


# ---- 读链：融合排序 + 召回强化 ----

def bm25_rank_to_score(rank: float) -> float:
    """FTS5 bm25 越小越相关（负值）→ 绝对相关性 [0,1)。（P0，2026-11-02，照抄原仓库公式）

    取代旧的「候选集内排名归一」：排名分只适合融合（RRF 用途），不适合做阈值门控——
    旧实现下命中任一高频词即进候选、第 1 名恒 1.0、末位 0.50，inject_floor 形同虚设。
    """
    rel = -float(rank) if rank < 0 else 0.0
    return rel / (1.0 + rel)


def _query_terms(query: str) -> List[str]:
    """query 有效词表（≥2 字中文滑窗 bigram / ≥2 字符英文词，去重）——覆盖门槛用。

    分词口径与 ``_match_expr`` 的 simple 模式一致（CJK 连串 2 字滑窗 + ASCII 整词）。
    """
    import re
    terms: List[str] = []
    seen: set = set()
    for run in re.findall(r"[0-9A-Za-z_㐀-鿿]+", query or ""):
        if run.isascii():
            if len(run) >= 2 and run.lower() not in seen:
                seen.add(run.lower())
                terms.append(run.lower())
        else:
            for i in range(len(run) - 1):
                g = run[i:i + 2]
                if g not in seen:
                    seen.add(g)
                    terms.append(g)
    return terms


#: 新鲜度轮换环（P0）：最近检索用过的 record_id——命中只降权不排除，
#: 避免连续多轮复读同一条（§2.2 的「高频词垄断」的一半来源）。进程内记忆，重启即清。
_recent_ring: deque = deque(maxlen=64)


def search_brain(query: str, k: Optional[int] = None,
                 reinforce: bool = True,
                 conn: Optional[sqlite3.Connection] = None,
                 peer: str = "", context_key: str = "") -> List[Dict[str, Any]]:
    """融合排序检索：relevance(bm25) × importance × recency，active-only，floor 过滤。

    salience 不再衰减（P7，2026-11-02，对齐原仓库 priority 语义）：排序中直接取
    importance 字段值；dormant/archived 状态机已删除，state 只剩 active/tombstone。
    peer（可选，M3）：当前对话对象（结构化键）——关于 TA 的记忆终分加权（×1.25，
    **不是过滤**：全局事实照常召回）；不传 = 行为与无 peer 维度时一致。
    context_key（可选，M2，§6.2）：当前场合键——同场合标注的 atoms 加权 ×(1+w_ctx)、
    与圈层共同话题词面相交的再加 ×(1+w_bg)。**加权不过滤**（INV-3）：跨场合记忆
    照常召回，只是排得靠后；``context_key`` 绝不出现在 SQL WHERE 里。
    """
    if not (query or "").strip():
        return []
    owned = conn is None
    c = conn or open_brain()
    try:
        if not _fts_ok(c):
            return []
        match, like_terms = _match_expr(c, query)
        # trigram 模式下纯 2 字 query 只有 like_terms（无 MATCH）——不能因此直接
        # 返回空，否则 LIKE 兜底永远走不到（2 字中文词召回恒 0 的老坑）
        if not match and not like_terms:
            return []
        k = int(k or _cfg("top_k", 5))
        floor = float(_cfg("inject_floor", 0.30))
        weights = _cfg("rank_weights") or {}
        w_r = float(weights.get("relevance", 0.55))
        w_s = float(weights.get("salience", 0.30))
        w_t = float(weights.get("recency", 0.15))
        w_ctx = max(0.0, float(_ctx_cfg("w_ctx", 0.25)))
        w_bg = max(0.0, float(_ctx_cfg("w_bg", 0.10)))
        # 圈层共同话题词面（R8）：一次取好，逐条比对；取不到 = 无背景加权（词面为空）
        circle_topics: List[str] = []
        if context_key:
            try:
                circle_topics = [str(t).lower() for t in
                                 (get_circle_note(context_key) or {}).get("topics") or [] if t]
            except Exception:
                circle_topics = []
        states = "('active')"
        # P2/P4：persona 过滤已移除（死维度——生产链路恒空，过滤路径从未生效，
        # 形参链亦已移除）；单库单人格（INV-1）。
        rows: List[Any] = []
        if match:
            params: List[Any] = [match, max(k * 3, 15)]
            rows = c.execute(
                f"SELECT f.record_id, a.content, bm25(atoms_fts) AS r, a.kind, a.importance, "
                f"a.access_count, a.updated, a.last_access, a.peer, a.context_key FROM atoms_fts f "
                f"JOIN atoms a ON a.record_id = f.record_id "
                f"WHERE atoms_fts MATCH ? AND a.state IN {states} "
                f"ORDER BY r LIMIT ?", tuple(params)).fetchall()
        hits: List[Dict[str, Any]] = []
        qterms = _query_terms(query)
        df_cache: Dict[str, int] = {}

        def _term_df(term: str) -> int:
            n = df_cache.get(term)
            if n is None:
                try:
                    n = int(c.execute("SELECT COUNT(*) FROM atoms WHERE state='active' "
                                      "AND content LIKE ?", (f"%{term}%",)).fetchone()[0])
                except Exception:
                    n = 0
                df_cache[term] = n
            return n
        # P0（2026-11-02）：相关性=绝对分（照抄原仓库 rel/(1+rel)），不再候选集内排名归一；
        # 控量靠 k + 去冗余 + 轮换，不靠 threshold（plan §2.4）。覆盖门槛只打「 glue 」：
        # 命中仅盖住 query 的一个有效词、该词在 query≥3 词时覆盖率 <0.34、且是库内
        # 高频词（df>3）才丢——单命中低频/专名（白丝、便签）是真主题，留。
        for rid, content, rank, kind, imp, acc, upd, la, pr, ach in rows:
            cl = str(content or "").lower()
            if qterms:
                matched = [t for t in qterms if t in cl]
                if (not matched or (len(matched) == 1 and len(qterms) > 2
                                    and _term_df(matched[0]) > 3)):
                    continue
            rel = bm25_rank_to_score(rank)
            last_touch = upd or la or ""
            sal = float(imp or 0.5)   # P7：salience=importance，零衰减
            rec = pow(2.718281828459045, -_days_since(last_touch) / 180.0)
            final = w_r * rel + w_s * sal + w_t * rec
            ctx_hit = False
            if context_key and str(ach or "") == str(context_key):
                ctx_hit = True
                final = min(final * (1.0 + w_ctx), 1.5)   # 当前场合：加权不过滤（INV-3）
                if circle_topics and any(t in cl for t in circle_topics):
                    final = min(final * (1.0 + w_bg), 1.5)  # 共同背景词面命中（R8）
            if peer and str(pr or "") == peer:
                final = min(1.0, final * 1.25)   # 关于当前对象：加权不过滤（M3）
            if str(rid) in _recent_ring:
                final *= 0.85                    # 新鲜度轮换：只降权不排除
            if final < floor:
                continue
            hits.append({"record_id": rid, "content": content, "score": round(final, 6),
                         "kind": kind, "salience": round(sal, 4), "peer": str(pr or ""),
                         "context_key": str(ach or ""), "ctx_hit": ctx_hit})
        hits.sort(key=lambda h: h["score"], reverse=True)
        # 同主题去冗余：(kind, peer, 主题前缀 12 字) 同组只留最高分——
        # 保证「k 条是 k 个方面」，不是同一条的 k 种说法
        deduped: List[Dict[str, Any]] = []
        seen_groups: set = set()
        for h in hits:
            g = (str(h.get("kind") or ""), str(h.get("peer") or ""),
                 str(h.get("content") or "")[:12])
            if g in seen_groups:
                continue
            seen_groups.add(g)
            deduped.append(h)
            if len(deduped) >= k:
                break
        hits = deduped
        if reinforce and hits:
            now2 = _now()
            for h in hits:
                c.execute("UPDATE atoms SET access_count=access_count+1, last_access=?, "
                          "updated=? WHERE record_id=?", (now2, now2, h["record_id"]))
            c.commit()
            for h in hits:            # 入环：本轮检索用过的条目（轮换降权的依据）
                _recent_ring.append(str(h["record_id"]))
        # LIKE 兜底（2 字词在 trigram 降级模式下）；P2：persona 过滤已移除（死维度）
        if like_terms and len(hits) < k:
            lsql = ("SELECT record_id, content FROM atoms WHERE content LIKE ? "
                    "AND state IN " + states + " LIMIT ?")
            for kw in like_terms:
                lp: List[Any] = [f"%{kw}%", k]
                for rid, content in c.execute(lsql, tuple(lp)).fetchall():
                    if all(str(rid) != str(h["record_id"]) for h in hits):
                        hits.append({"record_id": rid, "content": content, "score": 0.0,
                                     "kind": "fact", "salience": 0.0,
                                     "context_key": "", "ctx_hit": False})
        return hits[:k]
    except Exception:
        return []
    finally:
        if owned:
            try:
                c.close()
            except Exception:
                pass


def inject_text_brain(query: str = "",
                      peer: str = "", context_key: str = "") -> Tuple[str, Dict[str, Any]]:
    """L1 检索文本块（供工具/调试；<relevant-memories> 形态）；零命中也返回 meta（可观测）。

    peer（可选，M3）：由调用方给结构化对象键；
    关于 TA 的记忆加权，条目带「关于 X：」前缀。context_key（可选，M2）：场合加权同源。
    """
    hits: List[Dict[str, Any]] = []
    try:
        hits = search_brain(query,
                            reinforce=True, peer=peer,
                            context_key=context_key)
    except Exception:
        hits = []
    meta = {"hits": len(hits), "chars": sum(len(h["content"]) for h in hits),
            "placement": "tool", "store": "brain",
            "peer": peer, "context_key": context_key,
            "context_hits": sum(1 for h in hits if h.get("ctx_hit"))}
    if not hits:
        return "", meta
    lines = "\n".join(
        f"- 关于 {h['peer']}：{h['content']}" if h.get("peer") else f"- {h['content']}"
        for h in hits)
    text = ("<relevant-memories>\n以下为检索到的历史记忆,仅作参考,不代表当前任务进程:\n"
            + lines + "\n</relevant-memories>")
    return text, meta


def log_recall_observability(project: str = "", source: str = "", calls: int = 0,
                             hits: int = 0, top1_id: str = "", run_id: str = "",
                             context_key: str = "", source_kind: str = "",
                             source_external: bool = False,
                             injected_context: bool = False, context_chars: int = 0,
                             by_tool: Optional[Dict[str, int]] = None,
                             context_hits: int = 0, narrative_cross: int = 0,
                             sent: bool = False, llm_calls_delta: int = 0,
                             evidence: int = 0, bundle_taken: int = 0,
                             quota_blocked: int = 0) -> None:
    """P1 观测（2026-11-02 起步，§8.3 定稿）：每 run 一行观测记录。

    落点 ``~/.omniagent/memory/memory_recall-YYYY-MM.jsonl``（按月分片，不清理历史）；
    主键 ``run_id = task_id:run_key``，与轨迹里的 ``memory_injected`` 两源可关联。
    观测是**量尺**不是记忆：不进注入面、不给 agent 查询工具，只由离线脚本读来算指标。
    任何异常静默（观测不打断主流程）。
    """
    if not enabled():
        return
    try:
        rec = {"run_id": str(run_id or ""), "ts": _now(),
               "project": str(project or ""), "context_key": str(context_key or ""),
               "source_kind": str(source_kind or ""), "source_external": bool(source_external),
               "injected_context": bool(injected_context),
               "context_chars": int(context_chars or 0),
               "by_tool": dict(by_tool or {}),
               "calls": int(calls), "hits": int(hits), "top1_id": str(top1_id or ""),
               "zero_query": int(calls) == 0,
               "context_hits": int(context_hits), "narrative_cross": int(narrative_cross),
               "sent": bool(sent), "llm_calls_delta": int(llm_calls_delta),
               "evidence": int(evidence), "bundle_taken": int(bundle_taken),
               "quota_blocked": int(quota_blocked),
               "any_fetch": int(calls) > 0 or int(bundle_taken) > 0}
        shard = _now()[:7]
        path = os.path.join(os.path.dirname(_BRAIN_PATH),
                            f"memory_recall-{shard}.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---- 叙事索引（§13.2，INV-12：只存指针不存正文；正文文件是唯一真相） ----

_NARR_KINDS = ("diary", "month", "relation")


def _json_list(raw: Any) -> List[str]:
    """TEXT 列里的 JSON 数组 → list[str]（容错；损坏/空 → 空列表）。"""
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if str(x).strip()]
    try:
        v = json.loads(str(raw or ""))
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
    except Exception:
        pass
    return []


def _narr_doc_id(doc_id: str, kind: str, project_id: str = "") -> str:
    """H-4：日记/月记按**日期**全局唯一，``doc_id`` 就是裸文件名。

    旧实现用 ``pid:`` 前缀规避「同一天各项目各写一篇」的重名，本质上是在索引层
    给分身打补丁。统一一日一篇后不再需要；``kind`` / ``project_id`` 保留为调用
    兼容（project_id 仍作为索引行的场合标注列，但不参与键构造）。
    relation 用 uid（全局一份）。
    """
    _ = (kind, project_id)
    return str(doc_id or "").strip()


def upsert_narrative_index(doc_id: str, kind: str, project_id: str = "",
                           date_from: str = "", date_to: str = "",
                           time_from: str = "", time_to: str = "",
                           summary: str = "", topics: Optional[List[str]] = None,
                           context_keys: Optional[List[str]] = None,
                           peers: Optional[List[str]] = None,
                           atmosphere: str = "",
                           date_basis: str = "local") -> bool:
    """``narrative_index`` 的**唯一写入口**（工程侧；LLM 沙箱无权限，INV-12）。

    幂等键 ``doc_id + kind``、覆盖不追加（§13.2.3）；``narrative_fts`` 先删后插
    （与 ``_fts_upsert`` 同款，恒一行）。``search_text = summary + topics +
    atmosphere``（P6-1 可回味词面，触发物/心境可检索），**不存正文**（INV-12）；
    失败静默返回 False（索引失败不阻断写块，正文仍是真相）。
    """
    doc_id = str(doc_id or "").strip()
    kind = str(kind or "").strip()
    if not doc_id or kind not in _NARR_KINDS or not enabled():
        return False
    doc_id = _narr_doc_id(doc_id, kind, project_id)
    topics = _normalize_topics(topics or [])
    try:
        conn = open_brain()
        try:
            conn.execute("DELETE FROM narrative_index WHERE doc_id=? AND kind=?",
                         (doc_id, kind))
            conn.execute(
                "INSERT INTO narrative_index(doc_id, kind, project_id, date_from, date_to, "
                "time_from, time_to, summary, topics, context_keys, peers, atmosphere, "
                "date_basis, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, kind, str(project_id or ""), str(date_from or ""),
                 str(date_to or ""), str(time_from or ""), str(time_to or ""),
                 str(summary or "")[:200], json.dumps(topics, ensure_ascii=False),
                 json.dumps([str(c) for c in (context_keys or [])], ensure_ascii=False),
                 json.dumps([str(p) for p in (peers or [])], ensure_ascii=False),
                 str(atmosphere or "")[:32],
                 str(date_basis or "local"), _now()))
            conn.execute("DELETE FROM narrative_fts WHERE doc_id=? AND kind=?",
                         (doc_id, kind))
            search_text = (str(summary or "") + " " + " ".join(topics) + " "
                           + str(atmosphere or "")).strip()
            if search_text:
                conn.execute("INSERT INTO narrative_fts(search_text, doc_id, kind) "
                             "VALUES(?,?,?)", (search_text[:400], doc_id, kind))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def delete_narrative_index(doc_id: str, kind: str) -> bool:
    """撤一条叙事索引（relation 卡注销/收编用）；正文文件由调用方处理。

    与 upsert 同键口径（``_narr_doc_id``），索引删了检索就不再命中；
    失败静默返回 False（索引失败不阻断注销，正文删除才是主操作）。
    """
    doc_id = str(doc_id or "").strip()
    kind = str(kind or "").strip()
    if not doc_id or kind not in _NARR_KINDS or not enabled():
        return False
    doc_id = _narr_doc_id(doc_id, kind)
    try:
        conn = open_brain()
        try:
            conn.execute("DELETE FROM narrative_index WHERE doc_id=? AND kind=?",
                         (doc_id, kind))
            conn.execute("DELETE FROM narrative_fts WHERE doc_id=? AND kind=?",
                         (doc_id, kind))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def get_narrative_row(doc_id: str, kind: str = "") -> Optional[Dict[str, Any]]:
    """按 doc_id（可选 kind）取索引行（供 narrative_read 拿 project_id / 场合标注）。

    裸文件名解析：跨项目同名时不猜——返回 None，
    让调用方用检索结果里带前缀的 doc_id 重试。
    """
    d = str(doc_id or "").strip()
    try:
        conn = open_brain()
        try:
            sel = ("SELECT doc_id, kind, project_id, date_from, date_to, time_from, "
                   "time_to, summary, topics, context_keys, peers, atmosphere, date_basis, "
                   "updated FROM narrative_index ")
            if kind:
                row = conn.execute(sel + "WHERE doc_id=? AND kind=?",
                                   (d, str(kind))).fetchone()
            else:
                row = conn.execute(sel + "WHERE doc_id=? ORDER BY kind LIMIT 1",
                                   (d,)).fetchone()
            if row is None and ":" not in d and kind in ("diary", "month", ""):
                # 唯一后缀解析：恰好一个项目持有该文件名才解析，同名多个不猜
                rows = conn.execute(
                    sel + "WHERE doc_id LIKE ? AND kind IN ('diary','month')",
                    (f"%:{d}",)).fetchall()
                if len(rows) == 1:
                    row = rows[0]
        finally:
            conn.close()
    except Exception:
        return None
    if not row:
        return None
    keys = ("doc_id", "kind", "project_id", "date_from", "date_to", "time_from",
            "time_to", "summary", "topics", "context_keys", "peers", "atmosphere",
            "date_basis", "updated")
    d = dict(zip(keys, row))
    d["topics"] = _json_list(d.get("topics"))
    d["context_keys"] = _json_list(d.get("context_keys"))
    d["peers"] = _json_list(d.get("peers"))
    return d


def search_narrative(query: str = "", kind: str = "", date_from: str = "",
                     date_to: str = "", peer: str = "", context_key: str = "",
                     limit: int = 8, require_match: bool = False) -> List[Dict[str, Any]]:
    """叙事检索核心（§13.3.1）：只返回条目**不返回正文**（正文走 ``read_narrative``）。

    排序 = bm25 相关性 + 时间近因 + 场合/对象加权；``context_key`` 只加权不过滤
    （INV-3：跨场合叙事照样可召回，只是排得靠后）。日期按**本地日历日**与行的
    ``date_from/date_to`` 区间求交；query 为空时退化为按时间近因浏览（指定 kind /
    日期区间用）。``limit`` 1-20。
    """
    if not enabled():
        return []
    try:
        n = max(1, min(20, int(limit or 8)))
    except Exception:
        n = 8
    kind = str(kind or "").strip().lower()
    kind_sql, kind_params = "", []
    if kind in _NARR_KINDS:
        kind_sql = " AND n.kind = ?"
        kind_params = [kind]
    qf = str(date_from or "").strip()[:10]
    qt = str(date_to or "").strip()[:10]
    try:
        conn = open_brain()
        try:
            cand: List[Tuple[Dict[str, Any], float]] = []
            match, like_terms = _match_expr(conn, query) if (query or "").strip() else (None, [])
            seen: set = set()

            def _load_rows(where: str, params: Tuple[Any, ...]) -> List[Any]:
                return conn.execute(
                    "SELECT n.doc_id, n.kind, n.project_id, n.date_from, n.date_to, "
                    "n.time_from, n.time_to, n.summary, n.topics, n.context_keys, n.peers, "
                    f"n.atmosphere FROM narrative_index n WHERE {where}{kind_sql} "
                    "ORDER BY n.date_from DESC LIMIT 400",
                    params + tuple(kind_params)).fetchall()

            if match:
                try:
                    frows = conn.execute(
                        "SELECT f.doc_id, f.kind, bm25(narrative_fts) AS r "
                        "FROM narrative_fts f WHERE narrative_fts MATCH ? LIMIT 200",
                        (match,)).fetchall()
                except Exception:
                    frows = []
                fmap = {f"{r[0]}|{r[1]}": -float(r[2]) for r in frows}
            else:
                fmap = {}
            # 候选装载：有 query 走 FTS 命中 + LIKE 兜底；无 query 全量（日期区间过滤后按近因排）
            for row in _load_rows("1=1", ()):
                d = dict(zip(("doc_id", "kind", "project_id", "date_from", "date_to",
                              "time_from", "time_to", "summary", "topics", "context_keys",
                              "peers", "atmosphere"), row))
                key = f"{d['doc_id']}|{d['kind']}"
                if key in seen:
                    continue
                d["topics"] = _json_list(d.get("topics"))
                d["context_keys"] = _json_list(d.get("context_keys"))
                d["peers"] = _json_list(d.get("peers"))
                # 日期区间求交（本地日历日；空区间不设限）
                if qf and d["date_to"] and str(d["date_to"]) < qf:
                    continue
                if qt and d["date_from"] and str(d["date_from"]) > qt:
                    continue
                seen.add(key)
                rel = bm25_rank_to_score(-fmap.get(key, 0.0)) if key in fmap else 0.0
                cand.append((d, rel))
            # LIKE 兜底（2 字词等）：不重复计分，rel 按 0 处理（trigram 降级形态）
            if like_terms and not fmap:
                for row in _load_rows(
                        "(" + " OR ".join("n.summary LIKE ?" for _ in like_terms) + ")",
                        tuple(f"%{kw}%" for kw in like_terms)):
                    d = dict(zip(("doc_id", "kind", "project_id", "date_from", "date_to",
                                  "time_from", "time_to", "summary", "topics", "context_keys",
                                  "peers", "atmosphere"), row))
                    key = f"{d['doc_id']}|{d['kind']}"
                    if key in seen:
                        continue
                    d["topics"] = _json_list(d.get("topics"))
                    d["context_keys"] = _json_list(d.get("context_keys"))
                    d["peers"] = _json_list(d.get("peers"))
                    if qf and d["date_to"] and str(d["date_to"]) < qf:
                        continue
                    if qt and d["date_from"] and str(d["date_from"]) > qt:
                        continue
                    seen.add(key)
                    cand.append((d, 0.0))
        finally:
            conn.close()
    except Exception:
        return []

    # 话题词面（共同背景）与 peer 线索来自 query/参数；加权不过滤（§6.2）
    q = str(query or "")
    peer_key = str(peer or "").strip()
    # 认领归一（2026-11-12）：peer 按 **uid** 比对（先经 resolve_peer_uids——
    # 已认领的旧身份对应到主人卡），且卡上标签只取其 uid（昵称改了不裂匹配）。
    peer_uid_set = set()
    if peer_key:
        from omni_core.local.identity import resolve_peer_uids, speaker_uid as _peer_uid
        peer_uid_set = set(resolve_peer_uids(peer_key))
    cid = str(context_key or "").strip()
    qterms = set(_query_terms(q))
    scored: List[Dict[str, Any]] = []
    for d, rel in cand:
        # 读侧候选包用：只要 FTS/LIKE 真命中的条目，排除「按近因浏览」凑数行。
        # 缺省 False —— 叙事工具的浏览语义不变，这是加性能力不是兼容分支。
        if require_match and rel <= 0.0:
            continue
        rec_days = _days_since(d.get("time_to") or d.get("date_to") or "")
        rec = pow(2.718281828459045, -rec_days / 180.0)
        final = 0.6 * rel + 0.4 * rec
        context_keys = d.get("context_keys") or []
        if cid and cid in context_keys:
            final *= (1.0 + _ctx_cfg("w_ctx", 0.25))       # 同场合标注：加权不过滤
        if peer_uid_set and any((_peer_uid(str(p)) or str(p)) in peer_uid_set
                                for p in (d.get("peers") or [])):
            final *= 1.25                                   # 关于当前对象
        if cid and qterms:
            # 共同背景（R8，w_bg）：本场合叙事且正文/主题词面与 query 相交
            face = " ".join([str(d.get("summary") or ""), " ".join(d.get("topics") or []),
                             str(d.get("atmosphere") or "")]).lower()
            if any(t in face for t in qterms):
                final *= (1.0 + _ctx_cfg("w_bg", 0.10))
        if final <= 0.0:
            continue
        scored.append({"doc_id": d["doc_id"], "kind": d["kind"],
                       "project_id": d["project_id"],
                       "date": str(d.get("date_from") or ""),
                       "summary": str(d.get("summary") or ""),
                       "topics": d.get("topics") or [],
                       "atmosphere": str(d.get("atmosphere") or ""),
                       "score": round(min(final, 1.5), 6),
                       "context_keys": context_keys, "peers": d.get("peers") or []})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:n]


def read_narrative(doc_id: str, kind: str = "") -> Dict[str, Any]:
    """读叙事正文（文件是唯一真相；索引只给指针）。kind 空 → 索引推断 → diary 兜底。

    返回 ``{ok, kind, doc_id, project_id, chars, text, summary, context_keys, peers}``；
    出口约束（外部来源只给摘要等）在工具层做——本函数是内核数据面，不做 ACL。
    """
    from omni_core.local import scene_executor

    doc_id = str(doc_id or "").strip()
    kind = str(kind or "").strip().lower()
    if not doc_id:
        return {"ok": False, "error": "doc_id 不能为空"}
    row = get_narrative_row(doc_id, kind)
    if row is None and not kind:
        kind = "diary" if doc_id.startswith("diary-") else "relation"
    kind = kind or str((row or {}).get("kind") or "diary")
    if kind == "relation":
        try:
            from omni_core.local import runtime_paths as _rp

            p = _rp.global_relations() / f"{doc_id}.md"
            raw = p.read_text(encoding="utf-8") if p.is_file() else ""
        except Exception:
            raw = ""
        body = raw.split("-----META-END-----", 1)[-1].strip() if raw else ""
        project_id = ""
    else:
        project_id = str((row or {}).get("project_id") or "")
        # 正文在全局叙事目录，doc_id 即文件名（无旧形态兼容）
        fname = doc_id
        raw = scene_executor.read_scene_block(project_id, fname)
        _, body = scene_executor.parse_scene_block(raw)
    if not body:
        return {"ok": False, "error": f"未找到叙事正文: {doc_id}"}
    return {"ok": True, "kind": kind, "doc_id": doc_id, "project_id": project_id,
            "chars": len(body), "text": body,
            "summary": str((row or {}).get("summary") or ""),
            "context_keys": (row or {}).get("context_keys") or [],
            "peers": (row or {}).get("peers") or []}


_narr_migrate_started = threading.Lock()
_narr_migrate_state: Dict[str, bool] = {"running": False}


# ---- 圈层语境（circle_note 读取端 + 近窗词面；写链/reflection 见 §5.2，M3） ----

def get_circle_note(context_key: str) -> Dict[str, Any]:
    """读一个场合的圈层语境摘要（一行一场合；无 → 空字典）。

    返回 ``{summary, topics[], tone, source_count, source_kinds[], version, updated}``。
    语境预期**不是可引用事实**（INV-5）：消费端只有 ``<current-context>`` 的
    话题/语气字段与检索加权，任何工具都不得把它当返回值原文。
    """
    if not (context_key or "").strip() or not enabled():
        return {}
    try:
        conn = open_brain()
        try:
            row = conn.execute(
                "SELECT summary, topics, tone, source_count, source_kinds, version, "
                "updated, prev_summary FROM circle_note WHERE context_key=?", (str(context_key),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    return {"context_key": str(context_key), "summary": str(row[0] or ""),
            "topics": _json_list(row[1]), "tone": str(row[2] or ""),
            "source_count": int(row[3] or 0), "source_kinds": _json_list(row[4]),
            "version": int(row[5] or 0), "updated": str(row[6] or ""),
            "prev_summary": str(row[7] or "")}


def recent_l0_lines(context_key: str = "", session_id: str = "",
                    limit: int = 40) -> List[str]:
    """近窗聊天原文行（词面提取/语境链取料用；时间倒序，最多 ``limit`` 条）。"""
    if not enabled():
        return []
    try:
        c = _l0_conn()
        cid, sid = str(context_key or ""), str(session_id or "")
        scope, params = "", []
        if cid or sid:
            scope = "WHERE (l.context_key = ? OR l.session_id = ?)"
            params = [cid or sid, sid or cid]
        rows = c.execute(
            f"SELECT l.text FROM l0_messages l {scope} "
            "ORDER BY l.recorded_at DESC LIMIT ?",
            tuple(params + [max(1, min(200, int(limit or 40)))]),
        ).fetchall()
        return [str(r[0] or "") for r in rows if r[0]]
    except Exception:
        return []


# ---- 语境链（§5.2，M3）：buffer 累积 → reflection → circle_note ----
#
# 原料只有**通用信号**（话题词面 / 条数节奏 / 长度分布 / 来源道），
# 不写他人原话（INV-6）、不含领域词表（INV-4）；产物是**语境预期**（这个场合大致
# 聊什么、什么分寸），不是可引用事实——永不进 atoms、不参与四态（INV-5）。
# 失败保留上一版、静默不阻断（§5.6）。

_CIRCLE_SYSTEM = (
    "你是语境归纳器。给你某个场合最近一段时间的**聚合信号**"
    "（高频话题词面、消息条数、说话人标注形态、消息长度分布、来源构成）——"
    "没有任何人的原话。请提炼这个场合的「语境预期」：这里大致在关注什么、"
    "说话是什么分寸氛围。只依据给出的信号归纳，不臆测具体事件、不引用任何人的话。"
    "topics：2-6 个主题词或短语（名词优先，各 ≤12 字），取自词面信号；"
    "没有可靠信号就给空数组。"
    "tone：一句话（≤40 字）描述说话分寸/氛围，没有把握就给空串。"
    '只返回 JSON:{"summary": "这个场合大致聊什么、什么氛围，≤120 字", '
    '"topics": ["...", "..."], "tone": "..."}'
)


def _circle_cfg(key: str, default: Any = None) -> Any:
    """读 circle_note.<key>（语境链参数；§12 examples/config.yaml 同名）。"""
    try:
        import config
        val = config.get_config(f"circle_note.{key}", None)
        return default if val is None else val
    except Exception:
        return default


def _surface_topics_from(texts: List[str], top: int = 8) -> List[str]:
    """一批文本 → 高频 2 字滑窗词面（与检索分词口径一致；零 LLM、零词表）。"""
    df: Dict[str, int] = {}
    for text in texts:
        for g in set(_query_terms(str(text or ""))):
            df[g] = df.get(g, 0) + 1
    if not df:
        return []
    n = max(1, len(texts))
    cands = [(g, c) for g, c in df.items() if c >= max(2, int(n * 0.2))]
    cands.sort(key=lambda kv: (-kv[1], kv[0]))
    return [g for g, _ in cands[:max(1, top)]]


def update_context_buffer(context_key: str, texts: List[str],
                          source_kind: str = "chat") -> Dict[str, Any]:
    """按场合累积通用信号进 ``context_buffer``（**覆盖不追加**；INV-4/INV-6）。

    buffer 只存词面与计数，不存原话；``source_kinds`` 标注来源构成（INV-5）。
    返回累积后的 buffer 概要（观测用）；失败返回空字典（静默）。
    """
    context_key = str(context_key or "").strip()
    if not context_key or not enabled():
        return {}
    now = _now()
    try:
        conn = open_brain()
        try:
            row = conn.execute("SELECT buffer, msg_count FROM context_buffer WHERE context_key=?",
                               (context_key,)).fetchone()
            buf: Dict[str, Any] = {}
            if row:
                try:
                    buf = json.loads(str(row[0] or ""))
                except Exception:
                    buf = {}
            old_count = int(buf.get("msg_count") or (row[1] if row else 0) or 0)
            texts = [str(t or "")[:200] for t in (texts or []) if str(t or "").strip()]
            merged_texts = list(texts) + list(buf.get("recent_surfaces") or [])
            buf["topics"] = _surface_topics_from(merged_texts[:120])
            buf["msg_count"] = old_count + len(texts)
            kinds = set(str(k) for k in (buf.get("source_kinds") or []))
            if texts:
                kinds.add(str(source_kind or "chat"))
            buf["source_kinds"] = sorted(kinds)
            lens = [len(t) for t in texts]
            if lens:
                buf["avg_len"] = int(sum(lens) / len(lens))
            buf["updated"] = now
            conn.execute(
                "INSERT OR REPLACE INTO context_buffer(context_key, buffer, msg_count, updated) "
                "VALUES(?,?,?,?)",
                (context_key, json.dumps(buf, ensure_ascii=False), int(buf["msg_count"]), now))
            conn.commit()
            return buf
        finally:
            conn.close()
    except Exception:
        return {}


def _circle_note_due(context_key: str, buf: Dict[str, Any]) -> bool:
    """触发判定（§5.2）：条数阈值或间隔阈值；每场合每巩固周期 ≤1 次（N-13）。"""
    try:
        conn = open_brain()
        try:
            row = conn.execute("SELECT last_reflection FROM circle_note WHERE context_key=?",
                               (context_key,)).fetchone()
            last = str(row[0] or "") if row else ""
            lastc = str((conn.execute("SELECT v FROM meta WHERE k='last_consolidation'")
                         .fetchone() or [""])[0] or "")
        finally:
            conn.close()
        # 本巩固周期内已反思过 → 不再来一次（条数/间隔都不触发；周期边界=上次巩固时刻）
        if last and (not lastc or last >= lastc):
            return False
    except Exception:
        pass
    msg_count = int(buf.get("msg_count") or 0)
    min_msgs = max(3, int(_circle_cfg("min_msgs", 12) or 12))
    interval = max(60.0, float(_circle_cfg("interval_s", 600.0) or 600.0))
    if msg_count >= min_msgs:
        return True
    try:
        if last and _days_since(last) * 86400.0 >= interval:
            return True
    except Exception:
        pass
    return False


def reflect_circle_note(brain_cfg: Dict[str, Any], context_key: str) -> bool:
    """对一个场合跑一次 reflection，重写（不追加）``circle_note``。

    - 原料 = context_buffer 的聚合信号（无原话，INV-6）；一次 ``chat_json``（§13.10
      的 D-3 思路：产物一次拿全，topics 复用同一次调用）。
    - 重写保留上一版（``prev_summary``）供回滚；失败静默保留旧版（§5.6）。
    - **不写 atoms**（INV-5）、不读叙事正文（INV-10）。
    """
    if not enabled() or not brain_cfg or not (context_key or "").strip():
        return False
    try:
        conn = open_brain()
        try:
            row = conn.execute("SELECT buffer FROM context_buffer WHERE context_key=?",
                               (str(context_key),)).fetchone()
        finally:
            conn.close()
        buf = {}
        if row:
            try:
                buf = json.loads(str(row[0] or ""))
            except Exception:
                buf = {}
        if not buf:
            return False
        user = ("场合键: " + str(context_key) + "\n"
                + "聚合信号（无任何原话）:\n"
                + json.dumps({"topics_词面": buf.get("topics") or [],
                              "累积条数": buf.get("msg_count") or 0,
                              "来源构成": buf.get("source_kinds") or [],
                              "平均长度": buf.get("avg_len")},
                             ensure_ascii=False))
        data = llm_judge.chat_json(brain_cfg, _CIRCLE_SYSTEM, user, timeout=60.0)
        if not isinstance(data, dict):
            return False
        summary = str(data.get("summary") or "").strip()[:160]
        topics = _normalize_topics(data.get("topics"))
        tone = str(data.get("tone") or "").strip()[:60]
        if not summary and not topics and not tone:
            return False
        # 上一版保留；source 标注（INV-5）：本版聚合了多少条、来源构成
        conn = open_brain()
        try:
            old = conn.execute("SELECT summary, version FROM circle_note WHERE context_key=?",
                               (str(context_key),)).fetchone()
            prev_summary = str(old[0] or "") if old else ""
            version = (int(old[1] or 0) if old else 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO circle_note(context_key, summary, topics, tone, "
                "source_count, source_kinds, prev_summary, version, msg_count, "
                "last_reflection, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (str(context_key), summary, json.dumps(topics, ensure_ascii=False),
                 tone, int(buf.get("msg_count") or 0),
                 json.dumps([str(k) for k in (buf.get("source_kinds") or [])],
                            ensure_ascii=False),
                 prev_summary, version, 0, _now(), _now()))
            conn.execute("UPDATE context_buffer SET msg_count=0 WHERE context_key=?",
                         (str(context_key),))
            # buffer JSON 里的计数同步清零（下轮累积从 0 起；词面保留供下次合并）
            try:
                buf["msg_count"] = 0
                conn.execute("UPDATE context_buffer SET buffer=? WHERE context_key=?",
                             (json.dumps(buf, ensure_ascii=False), str(context_key)))
            except Exception:
                pass
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def _circle_reflection_budget_left() -> int:
    """本轮巩固周期剩余的全局反思预算（≤5 次/周期；计数与周期标记都在 meta）。"""
    try:
        conn = open_brain()
        try:
            cnt = int((conn.execute("SELECT v FROM meta WHERE k='circle_reflect_count'")
                       .fetchone() or ["0"])[0] or 0)
        finally:
            conn.close()
        return max(0, int(_circle_cfg("max_per_cycle", 5) or 5) - cnt)
    except Exception:
        return 0


def _circle_reflection_budget_used() -> None:
    try:
        conn = open_brain()
        try:
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('circle_reflect_count',?)",
                         (str(int((conn.execute("SELECT v FROM meta WHERE k='circle_reflect_count'")
                               .fetchone() or ["0"])[0] or 0) + 1),))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def run_context_reflections(brain_cfg: Optional[Dict[str, Any]],
                            chats: Optional[List[str]] = None,
                            coldstart: bool = False) -> int:
    """语境链入口（flush 尾部按批触发 / 巩固期冷启动）。

    - ``chats`` 给定 → 只对这些场合判定触发（条数/间隔 + 周期内 ≤1 次）；
    - ``coldstart=True`` → 从 L0 按场合分组找还没有 circle_note 的场合各跑一次首版
      （受全局预算约束）。
    - 返回完成次数；失败静默（语境链失败保留上一版）。
    """
    if not enabled() or not brain_cfg:
        return 0
    done = 0
    targets: List[str] = []
    if coldstart and chats is None:
        try:
            c = _l0_conn()
            min_c = max(3, int(_circle_cfg("min_msgs", 12) or 12))
            rows = c.execute(
                "SELECT context_key, COUNT(*) FROM l0_messages "
                "WHERE context_key != '' AND source_kind='chat' "
                "GROUP BY context_key HAVING COUNT(*) >= ? ORDER BY 2 DESC LIMIT 12",
                (min_c,)).fetchall()
            have: set = set()
            try:
                conn = open_brain()
                have = {str(r[0]) for r in
                        conn.execute("SELECT context_key FROM circle_note").fetchall()}
                conn.close()
            except Exception:
                pass
            targets = [str(r[0]) for r in rows if str(r[0]) not in have]
        except Exception:
            targets = []
    elif chats:
        targets = [str(c) for c in chats if str(c).strip()]
    for context_key in targets:
        if _circle_reflection_budget_left() <= 0:
            break
        try:
            if coldstart:
                # 冷启动直接跑（无 buffer 时先从近窗攒一份信号）
                if not update_context_buffer(context_key, recent_l0_lines(context_key=context_key,
                                                                      limit=60)):
                    continue
                if reflect_circle_note(brain_cfg, context_key):
                    done += 1
                    _circle_reflection_budget_used()
            else:
                row = {}
                try:
                    conn = open_brain()
                    try:
                        r = conn.execute("SELECT buffer FROM context_buffer WHERE context_key=?",
                                         (str(context_key),)).fetchone()
                        if r:
                            row = json.loads(str(r[0] or "") or "{}")
                    finally:
                        conn.close()
                except Exception:
                    row = {}
                if not _circle_note_due(context_key, row or {}):
                    continue
                if reflect_circle_note(brain_cfg, context_key):
                    done += 1
                    _circle_reflection_budget_used()
        except Exception:
            continue
    return done


# ---- 巩固周期（后台；备份先行） ----

def _consolidation_due() -> bool:
    try:
        conn = open_brain()
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='last_consolidation'").fetchone()
            last = row[0] if row else ""
            cnt = int((conn.execute("SELECT v FROM meta WHERE k='flush_count'").fetchone()
                       or ["0"])[0] or 0)
            # 必须在使用 conn 之后再关：此前 close 后再 execute 会抛 ProgrammingError
            # 被 except 吞掉，导致巩固（去重/抽象/强度重算）永不触发
            total = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        finally:
            conn.close()
        if total == 0:
            return False  # 空库不巩固
        every = int(_cfg("consolidation_every", 20))
        due_time = (not last) or _days_since(last) > 1.0
        return cnt >= every or due_time
    except Exception:
        return False


#: 单条记忆的字数硬线。**在合并这一步它是拒绝线，不是截断线**（2026-11-03 定）：
#: 静默截断会把合并结果的后半句砍掉，而合并本身又是「谁吞掉谁」的判定——两头合起来
#: 就是把同一条 rule 越并越宽，养成"什么都管的大话"（实测最宽的一条 active rule 已到
#: 117 字，而判定提示要求 ≤100 字）。merge 超线一律拒并：两条并存、下一轮再判——对齐
#: 「保不住就不判断，绝不猜」的红线（`doc/agent-control-master-spec.md` §6.4）。
#: 提炼侧与 `remember`/`update_memory` 的单条写入仍按本线截断（那是"模型写长了留前段"，
#: 不涉及吞掉另一条记忆）。
_MAX_ATOM_CHARS = 200

_CONSIST_SYSTEM = (
    "你是记忆一致性评审器。给你「新事实」列表和「已有记忆」候选(带编号)。"
    "对每条新事实判定:"
    "与某条候选表达同一事实 → duplicate(返回 target_id,旧条将强化而非新增);"
    "与某条**同一适用域**(同一场景、同一约束对象)且互补,并成一条后双方的条件与"
    "禁止项都还在 → merge(返回 target_id 与 merged_content,≤100 字);"
    "与某条矛盾且新信息更可信 → conflict(返回 target_id,旧条将作废、新条入库);"
    "是新信息 → novel。"
    "**跨适用域不许 merge**——把某个场合的表达惯例和一条工程事实并成一条，"
    "等于写出谁也用不上的大话;这种情况返回 novel,让它们各自成条。"
    "并完会丢掉某一方的条件、或需要把两条改写成更笼统的一句话才装得下 → novel。"
    "拿不准 → novel。"
    '只返回 JSON:{"decisions": [{"idx": 序号, "action": "novel|duplicate|merge|conflict",'
    ' "target_id": "...", "merged_content": "..."}]}'
)


#: 观察升格步的提炼章程（§4.6）：提炼器不是守门人——不做 keep/reject，量由四态去重
#: 与召回排序管（用户裁定 2026-11-10，不设每日上限）；只有提不出稳定结论的条目按空串跳过。
_PROMOTE_SYSTEM = (
    "你是记忆提炼器。把观察见闻逐条提炼成跨会话成立的结论短句。"
    "规则:1) 每条输入对应一条输出(idx 对齐);2) 结论 ≤60 字陈述句,提炼普遍事实/规律,"
    "不要复述原文细节;3) 提不出稳定结论(纯一次性事件/无信息量转述)时 text 给空串;"
    "4) 执行细节(文件路径/命令行/字段名)不是记忆,text 给空串。"
    "kind:fact(客观事实)|rule(规律/流程);importance:0~1(跨会话有用→高)。"
    '只返回 JSON:{"items": [{"idx": 0, "text": "...", "kind": "fact", "importance": 0.5}]}'
)


def promote_observations(brain_cfg: Optional[Dict[str, Any]],
                         out: Optional[Dict[str, Any]] = None) -> None:
    """观察升格步（§4.6 升格路径落地，巩固期调用）：观察原文不直接写 atoms（INV-6），
    统一提炼为结论短句后走四态一致性入事实层，写齐 ``source_kind/observed_at/provenance``
    追溯标注（INV-14 / H-5）。

    - 幂等：升格原子的 ``source_task`` 记观察 record_id，已升格的观察不再重提；
    - 无每日上限：单轮 60 条只是 LLM 窗口约束，剩余随下一次巩固继续；
    - ``origin`` 取观察 context_key 前缀（如 ``life``）——L2 场景进料按 origin 分组路由；
    - 只取 ``source_external = 0`` 的观察行：该信号由边界层归一、内核不解析场合键。
    """
    stats = out if out is not None else {}
    if not enabled() or not brain_cfg:
        return
    try:
        conn = open_brain()
        try:
            rows = conn.execute(
                "SELECT record_id, context_key, text, recorded_at, provenance "
                "FROM l0_messages WHERE source_kind='observation' AND record_id NOT IN "
                "(SELECT source_task FROM atoms WHERE source_task != '') "
                # 投毒锁（worklog lifelike-presence §9.6）：外部可达来源记下的
                # 见闻只留叙事层，永不升格为世界事实。「是否外部可达」由边界层在
                # run 起始归一成结构信号随行落库，内核只读这一列，不解析场合键
                # 取值或前缀（INV-4）。
                "AND source_external = 0 "
                "ORDER BY recorded_at LIMIT 60").fetchall()
        finally:
            conn.close()
    except Exception:
        return
    if not rows:
        return
    lines = "\n".join(f"[{i}] ({str(r[3])[:10]}) {str(r[2])[:300]}"
                      for i, r in enumerate(rows))
    try:
        data = llm_judge.chat_json(_batch_llm_cfg(brain_cfg), _PROMOTE_SYSTEM,
                                   f"见闻列表:\n{lines}", timeout=90.0)
    except Exception:
        return
    cands: List[Tuple[int, Dict[str, Any]]] = []
    for it in (data or {}).get("items") or []:
        try:
            idx = int(it.get("idx"))
        except Exception:
            continue
        if not (0 <= idx < len(rows)):
            continue
        text = str(it.get("text") or "").strip()
        if not text:
            continue
        kind = str(it.get("kind") or "fact")
        if kind not in ("fact", "rule"):
            kind = "fact"
        try:
            imp = max(0.0, min(1.0, float(it.get("importance", 0.5))))
        except Exception:
            imp = 0.5
        r = rows[idx]
        cands.append((idx, {"text": text[:_MAX_ATOM_CHARS], "kind": kind,
                            "importance": imp, "peer": "",
                            "context_key": str(r[1] or ""),
                            "source_kind": "observation",
                            "observed_at": str(r[3])[:10],
                            "provenance": str(r[4] or "")[:200]}))
    if not cands:
        stats["obs_promoted"] = 0
        return
    conn = open_brain()
    try:
        # 四态一致性：候选 = 各条 top-3 近邻并集，一次批量 LLM（与 flush_brain 同口径）
        cand_pool: Dict[str, str] = {}
        for _, it in cands:
            for h in search_brain(it["text"], k=3, reinforce=False, conn=conn):
                cand_pool[str(h["record_id"])] = h["content"]
        decisions: Dict[int, Dict[str, Any]] = {}
        if cand_pool:
            cand_lines = "\n".join(f"[{rid}] {txt}" for rid, txt in cand_pool.items())
            new_lines = "\n".join(f"[{i}] {it['text']}" for i, (_, it) in enumerate(cands))
            try:
                data = llm_judge.chat_json(_batch_llm_cfg(brain_cfg), _CONSIST_SYSTEM,
                                           f"新事实:\n{new_lines}\n\n已有记忆候选:\n{cand_lines}",
                                           timeout=60.0)
                for d in (data or {}).get("decisions") or []:
                    try:
                        decisions[int(d.get("idx"))] = d
                    except Exception:
                        continue
            except Exception:
                decisions = {}
        now = _now()
        applied = stored = merged_n = conflict_n = dup_n = 0
        for i, (ridx, it) in enumerate(cands):
            obs_rid = str(rows[ridx][0])
            origin = str(it.get("context_key") or "").split(":", 1)[0]
            new_rid = _new_id()
            d = decisions.get(i) or {"action": "novel"}
            action = str(d.get("action") or "novel")
            tgt = str(d.get("target_id") or "")
            if action in ("duplicate", "merge", "conflict"):
                if not tgt or conn.execute("SELECT 1 FROM atoms WHERE record_id=?",
                                           (tgt,)).fetchone() is None:
                    action = "novel"
            if action == "duplicate":
                conn.execute("UPDATE atoms SET access_count=access_count+1, last_access=?, "
                             "updated=?, importance=max(importance, ?) WHERE record_id=?",
                             (now, now, it["importance"], tgt))
                dup_n += 1
            elif action == "merge":
                raw = str(d.get("merged_content") or "").strip()
                if len(raw) > _MAX_ATOM_CHARS:
                    # 超线拒并：与 flush_brain 同口径——旧条不动，新条独立入库
                    _insert_atom(conn, new_rid, it, origin, obs_rid, now)
                    stored += 1
                else:
                    m = raw or it["text"]
                    conn.execute("UPDATE atoms SET content=?, updated=?, "
                                 "importance=max(importance, ?), access_count=access_count+1, "
                                 "last_access=? WHERE record_id=?",
                                 (m, now, it["importance"], now, tgt))
                    _fts_upsert(conn, tgt, m)
                    merged_n += 1
            elif action == "conflict":
                conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? "
                             "WHERE record_id=?", (new_rid, now, tgt))
                _insert_atom(conn, new_rid, it, origin, obs_rid, now)
                conflict_n += 1
            else:
                _insert_atom(conn, new_rid, it, origin, obs_rid, now)
                stored += 1
            applied += 1
        conn.commit()
        stats["obs_promoted"] = applied
        stats["obs_stored"] = stored
        stats["obs_merged"] = merged_n
        stats["obs_conflict"] = conflict_n
        stats["obs_duplicate"] = dup_n
    finally:
        conn.close()


def _batch_llm_cfg(brain_cfg: Optional[Dict[str, Any]], floor: int = 6000) -> Optional[Dict[str, Any]]:
    """批量整理调用的预算托底：reasoning 模型在长素材上会把小 ``max_tokens`` 全烧在
    思考里——``finish_reason='length'``、正文零字被截（2026-11-10 实证：2048 预算
    撑不住日记步 13KB 素材）。浅拷贝抬高 ``request.max_tokens`` 下限，不动调用方原 cfg。

    只用于 memory_brain 内部的整理/日记/升格批量调用；对话链路不经过这里。
    """
    if not brain_cfg:
        return brain_cfg
    req = dict(brain_cfg.get("request") or {})
    try:
        req["max_tokens"] = max(int(req.get("max_tokens") or 0), floor)
    except Exception:
        req["max_tokens"] = floor
    out = dict(brain_cfg)
    out["request"] = req
    return out


def maybe_consolidate(brain_cfg: Optional[Dict[str, Any]] = None) -> None:
    """任务收尾调用：满足触发条件则在后台线程跑巩固（绝不阻塞收尾）。"""
    if not enabled():
        return
    try:
        conn = open_brain()
        cnt = int((conn.execute("SELECT v FROM meta WHERE k='flush_count'").fetchone()
                   or ["0"])[0] or 0) + 1
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('flush_count',?)", (str(cnt),))
        conn.commit()
        conn.close()
    except Exception:
        return
    if not _consolidation_due():
        return
    # 派发闸：_consolidation_lock 由 worker 自己持有——若这里先 acquire 再让 worker
    # 去 with 同一把（非可重入）锁，worker 会永久阻塞、巩固永不执行
    with _dispatch_lock:
        if _dispatch_state["running"]:
            return
        _dispatch_state["running"] = True
    t = threading.Thread(target=_run_consolidation,
                         args=(brain_cfg or {},), daemon=True)
    t.start()


def _run_consolidation(brain_cfg: Dict[str, Any]) -> None:
    try:
        _consolidation_worker(brain_cfg)
    finally:
        with _dispatch_lock:
            _dispatch_state["running"] = False


def _consolidation_worker(brain_cfg: Dict[str, Any]) -> None:
    with _consolidation_lock:
        stats: Dict[str, Any] = {"ts": _now()}
        # 语境链的周期预算从零计（N-13：全局单轮 ≤5 次 reflection 的"轮"= 巩固周期）
        try:
            conn = open_brain()
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('circle_reflect_count','0')")
            conn.commit()
            conn.close()
        except Exception:
            pass
        try:
            os.makedirs(_ARCHIVE_DIR, exist_ok=True)
            import shutil
            backup = os.path.join(_ARCHIVE_DIR,
                                  f"backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.db")
            shutil.copyfile(_BRAIN_PATH, backup)
            stats["backup"] = backup
        except Exception:
            stats["backup"] = ""
        try:
            stats.update(_consolidate_impl(brain_cfg))
        except Exception as e:
            stats["error"] = f"{type(e).__name__}: {e}"
        try:
            conn = open_brain()
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_consolidation',?)",
                         (_now(),))
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('flush_count','0')")
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_consolidation_stats',?)",
                         (json.dumps(stats, ensure_ascii=False)[:2000],))
            conn.commit()
            conn.close()
        except Exception:
            pass
        logger.info(f"memory consolidation: {json.dumps(stats, ensure_ascii=False)[:500]}")


def _consolidate_impl(brain_cfg: Dict[str, Any]) -> Dict[str, Any]:
    conn = open_brain()
    out: Dict[str, Any] = {"dedup_checked": 0, "merged": 0, "merge_rejected": 0,
                           "abstracted": 0, "conflict": 0}
    if not _fts_ok(conn):
        conn.close()
        return out
    rows = conn.execute(
        "SELECT record_id, content, kind, importance, access_count, updated, last_access "
        "FROM atoms WHERE state='active' ORDER BY updated DESC LIMIT 200").fetchall()
    now = _now()

    # 1) 有界去重 + 矛盾检测：近段条目 vs 各自候选（LLM 四态）
    checked = 0
    for rid, content, kind, imp, acc, upd, la in rows[:40]:
        if checked >= 20:   # P5：判定上限 8→20，降低漏检率
            break
        # P5：候选 = 全文 FTS 近邻 top5（旧 content[:12] 精确短语实测候选 0/33）
        cands = _fts_neighbors(conn, content, rid, limit=5)
        if not cands:
            continue
        checked += 1
        cand_lines = "\n".join(f"[{crid}] {ct}" for crid, ct in cands)
        try:
            data = llm_judge.chat_json(_batch_llm_cfg(brain_cfg), _CONSIST_SYSTEM,
                                       f"新事实:\n[{rid}] {content}\n\n已有记忆候选:\n{cand_lines}",
                                       timeout=60.0)
        except Exception:
            continue
        for d in (data or {}).get("decisions") or []:
            act = str(d.get("action") or "")
            tgt = str(d.get("target_id") or "")
            if act == "merge" and tgt and d.get("merged_content"):
                merged = str(d["merged_content"]).strip()
                if len(merged) > _MAX_ATOM_CHARS:
                    # 超线拒并：两条都保持 active，下一轮再判（不静默截断成半句话）
                    out["merge_rejected"] += 1
                    continue
                conn.execute("UPDATE atoms SET content=?, updated=? WHERE record_id=? AND record_id!=?",
                             (merged, now, tgt, rid))
                _fts_upsert(conn, tgt, merged)
                conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? WHERE record_id=?",
                             (tgt, now, rid))
                out["merged"] += 1
            elif act == "conflict" and tgt:
                # 以信息更全的新条为准：目标 tombstone 指向新?此处新=旧条目自身，语义取「目标作废」
                conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? "
                             "WHERE record_id=? AND record_id!=?", (rid, now, tgt, rid))
                out["conflict"] += 1
    out["dedup_checked"] = checked   # 观测：本轮真正确实送进评审器的条数（2026-11-03 补，此前恒 0）

    # 2) 抽象提升：同主题 episode ≥3（peer 分组，M3：同一人的账放一起抽象）→ 1 条 rule（有界）
    episodes = conn.execute(
        "SELECT record_id, content, peer FROM atoms WHERE state='active' AND kind='episode' "
        "ORDER BY updated DESC LIMIT 30").fetchall()
    groups: Dict[str, List[Tuple[str, str]]] = {}
    for rid, content, pr in episodes:
        # M3：**同一人的账放一起**（peer 即分组键）；没有对象的才按主题前缀粗分。
        # 旧实现在两种情况下都拼了 content[:6]，等于要求"同一人 + 同一开头"才算同组，
        # 与注释语义不符（同一人 3 次踩我、开头各不相同 → 永远凑不齐 3 条）。
        peer_key = str(pr or "").strip()
        key = f"{peer_key}|" if peer_key else f"|{content[:6]}"
        groups.setdefault(key, []).append((rid, content))
    abstracted = 0
    for key, grp in groups.items():
        if len(grp) < 3 or abstracted >= 2:
            continue
        lines = "\n".join(f"- {c}" for _, c in grp)
        try:
            data = llm_judge.chat_json(
                brain_cfg,
                "你是记忆抽象器。把同一主题的多条具体事件片段提炼为 1 条通用 rule(≤60 字,陈述句)。"
                "无法提炼(事件间无共同规律)则返回 {\"rule\": \"\"}。"
                '只返回 JSON:{"rule": "..."}',
                lines, timeout=60.0)
            rule = str((data or {}).get("rule") or "").strip()
        except Exception:
            rule = ""
        if not rule:
            continue
        rid = _new_id()
        _insert_atom(conn, rid, {"text": rule, "kind": "rule", "importance": 0.8,
                                 "peer": key.split("|", 1)[0]},
                     "", "consolidation", now)
        for erid, _ in grp:
            # P7：不再 salience*0.5 衰减（salience=importance 是写链定的静态值）
            conn.execute("UPDATE atoms SET kind='episode', updated=? "
                         "WHERE record_id=?", (now, erid))
        abstracted += 1
    out["abstracted"] = abstracted

    conn.commit()
    conn.close()

    # 7) 自我画像重写（L3）：brain.db 新 atoms → self_profile.md（带外信号强制）
    if maintain_profile(brain_cfg):
        out["profile_rewritten"] = 1

    # 7.5) 观察升格（§4.6，2026-11-10 落地）：见闻 → 结论短句 → 四态入事实层（失败静默）。
    #     日记步之前跑：升格产物当轮即可被日记素材的「原子重访」引用。
    try:
        promote_observations(brain_cfg, out)
    except Exception:
        pass

    # 8) 日记步（plan persona-social-memory M2）：助手的一天 → L2 diary 块（失败静默）。
    #    补齐模式（2026-11-03）：不在线的日子由下一次巩固补写，回看 7 天，超窗放弃。
    try:
        diary_n = maintain_diaries(brain_cfg)
        if diary_n:
            out["diary_written"] = diary_n
        if maintain_month_diary(brain_cfg):
            out["month_diary_written"] = 1
    except Exception:
        pass
    # 关系卡由 _flush_tail 按 L0 直驱（不依赖巩固周期）：空 atoms 库时仍会更新。
    # 9.5) 语境链冷启动（M3）：还没有 circle_note 的老场合各跑一次首版 reflection
    # （受周期预算 ≤5 约束；原料 = L0 近窗聚合信号，无原话；失败静默）
    try:
        cs_n = run_context_reflections(brain_cfg, coldstart=True)
        if cs_n:
            out["circle_notes_coldstart"] = cs_n
    except Exception:
        pass
    return out


# ---- agent 自管（工具层入口；落盘权在内核） ----

_DIARY_SYSTEM = (
    "你是助手本人，以第一人称写「这一天」的日记。"
    "输入：这一天的对话记录、你当天的观察见闻、生活流水与完成的任务。记录格式："
    "[user] 行是别人对你说的话，结构化 speaker 字段标明说话人；[assistant] 行是你自己说过的话；"
    "[见闻] 行是你自己当天主动看到/查到的事（你的第一人称见闻，可写进日记）；"
    "[生活] 段是你这一天实际经历的事（上课、刷手机、出门、看到的东西）——"
    "它和对话往来**同等笔墨**写进当天的经历，不是附注；"
    "素材里的引用原话不属于该行说话人，"
    "若引用的是你说过的话，那是对方在接你的话茬。"
    "规则：1) 只记有意义的经历——他人对你的互动（谁做了什么、你怎么应对）、与主人的交流、"
    "值得记住的任务经历；纯寒暄、纯流水账不写；若当天与多个对象/场景都有交流，"
    "各安排笔墨（详略自定），不要漏掉一整个对话对象；"
    "2) 纯工具型任务（跑脚本/采集/分析类）一句话带过，不写步骤细节；"
    "3) 忠于事实，不虚构、不加评价性编造；4) 知识/规律不写（那些走记忆库，日记只收经历）；"
    "5) 聊天对象=出现过的 [user] 行说话人——你自己不算（你的名字哪怕出现在"
    "引用行里也不是聊天对象，别把自己写进「跟谁聊了」）；"
    "6) 这一天没什么值得记 → diary 返回空串，交白卷是正确答案。"
    # 日记归 L0-叙事（不进注入面）后不再受 450 字注入预算约束（plan memory-recall P6）。
    # topics（plan §13.10，D-3）：正文按 INV-12 不进索引，检索词面靠 summary+topics——
    # 首句往往不含正文里聊的主题词，topics 补上（钳制归一化，不是词表，INV-4）。
    "topics：2-5 个可检索的主题词或短语，名词优先，取自本段素材，可含当时的触发物或"
    "气氛词；不要整句、不要评价、不要臆测；没有则空数组。"
    # P6-1（plan §16.3）经历质地：仍是同一篇、同一次调用——多的是现场感
    # 字数帽 900→1800（2026-11-10 用户定）：日记不进注入面无预算约束，
    # 素材里有 [生活]/[见闻] 全天段，900 字装不下助手的一天。
    #（触发物/心境/带说话人的原话片段），只写素材里看得出来的，不臆造。
    "7) 经历质地：挑 1-3 处写「现场感」——当时是什么勾起了这段互动（一句话、一个物件、"
    "一种气氛），以及你当时的心情或反应；8) 原话片段：最多 1-2 处短原话引用，"
    "标清是谁说的（用结构化 speaker 字段）。"
    "atmosphere：≤16 字的当天气氛/情绪底色词面（取自素材；看不出则空串）。"
    "\n只返回 JSON:{\"diary\": \"第一人称日记，≤1800 字；没有则空串\", "
    "\"topics\": [\"...\", \"...\"], \"atmosphere\": \"...\"}"
)

#: 叙事主体的已知名字（本设备运行时登记，非配置非写死）：各渠道在拿到自己账号
#: 已知别名时由边界调 ``note_self_names``——换身份/换渠道随连接自愈，多来源并集
#: 成立（同一个人格的所有表面身份）。只收高置信身份，泛别名由边界自行把握，
#: 登记错误的兜底是日记措辞、不是记忆结构。
_SELF_NAMES: set = set()


def note_self_names(names) -> None:
    """渠道登记叙事主体（助手自己）在本渠道的已知名字/uid（幂集，重复调用无害）。"""
    for n in (names or ()):
        s = str(n or "").strip()
        if s:
            _SELF_NAMES.add(s)


def self_names() -> List[str]:
    return sorted(_SELF_NAMES)


def _we_participants() -> List[str]:
    """P6-2（H-6 §16.3）共同经历参与人：**取值来自配置**，内核零词表（INV-4）。

    主开关 ``knowledge.narrative.we_memory.enabled``（缺省关）+ 参与人列表
    ``knowledge.narrative.we_memory.participants``。只做名字匹配——素材行里出现
    参与人才标「共同经历」；不建 We 库、不做决策机制、不产主动闲聊。
    """
    try:
        import config
        if not bool(config.get_config("knowledge.narrative.we_memory.enabled", False)):
            return []
        raw = config.get_config("knowledge.narrative.we_memory.participants", []) or []
        raw = raw if isinstance(raw, (list, tuple)) else [raw]
        out: List[str] = []
        for x in raw:
            s = str(x or "").strip()[:24]
            if s and s not in out:
                out.append(s)
        return out[:6]
    except Exception:
        return []

_DIARY_MONTH_SYSTEM = (
    "你是助手本人，把「某个月」的多篇第一人称日记压缩成一篇月记。"
    "输入：该月各天的日记正文。"
    "规则：1) 保留关键人物与事件起伏（谁对你做了什么、关系怎么变化），舍弃琐事；"
    "2) 第一人称，≤250 字；3) 忠于日记内容，不虚构。"
    "topics：2-5 个可检索的主题词或短语，名词优先，取自本月素材；"
    "不要整句、不要评价、不要臆测；没有则空数组。"
    '只返回 JSON:{"summary": "一行式本月关键人物/事件地图，≤60 字", '
    '"month_diary": "月记正文", "topics": ["...", "..."]}'
)


#: 日记素材组装（v3，2026-11-01）：全局尾窗 + 每会话保底——机械层只保证每个会话
#: 都「被看见」（不因程序漏），写不写仍由 prompt 的意义判决定（纯寒暄不写）。
_DIARY_TAIL_LINES = 160
_DIARY_FLOOR_LINES = 10
_DIARY_MAX_LINES = 400


def _diary_observation_lines(day: str) -> List[Tuple[str, str]]:
    """当日观察见闻行（P6-1 / N-17，§14.2）：``[见闻] …``，与对话进**同一篇**日记素材。

    来源 ``l0_messages(source_kind='observation')``（INV-13 分道，不伪装成聊天原话）；
    ``recorded_at`` 是 UTC 存储，本地日折算在 Python 侧（D-1 口径，SQL 不认时区），
    回看 2 天 UTC 窗口覆盖任意时区偏移；文本截 300 字与对话行同宽。
    任何失败静默返回空（见闻是增强项，不阻断日记）。
    """
    out: List[Tuple[str, str]] = []
    try:
        from omni_core.local import localtime
        d0 = datetime.fromisoformat(f"{str(day)}T00:00:00+00:00") - timedelta(days=2)
        conn = _l0_conn()
        rows = conn.execute(
            "SELECT text, recorded_at FROM l0_messages "
            "WHERE source_kind='observation' AND recorded_at >= ? "
            "ORDER BY recorded_at", (d0.isoformat(),)).fetchall()
        for text, ts in rows:
            ts = str(ts or "")
            if localtime.local_date(ts) != str(day or ""):
                continue
            body = str(text or "").strip()[:300]
            if body:
                out.append((ts, f"[见闻] {body}"))
    except Exception:
        return []
    return out


def _diary_life_day_lines(day: str) -> List[Tuple[str, str]]:
    """life 事件层素材（worklog lifelike-presence §10，2026-11-10）：各项目
    ``days/<day>.md``（助手这天的客观流水，life 侧产出）→ ``[生活]`` 素材段。

    与 ``[见闻]`` 同理只作素材不落库；伪 ts 取当天 UTC 正午（= 本地晚八点），
    排序上落在白天对话之后、深夜闲聊之前。任何失败静默返回空（增强项）。
    """
    out: List[Tuple[str, str]] = []
    try:
        from omni_core.local import scene_executor as _se

        for f in sorted(rp.projects_root().glob(f"*/days/{day}.md")):
            try:
                _meta, body = _se.parse_scene_block(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            body = str(body or "").strip()
            if not body:
                continue
            ts = f"{day}T12:00:00+00:00"
            for para in [p.strip() for p in body.split("\n\n") if p.strip()]:
                out.append((ts, f"[生活] {para}"))
    except Exception:
        return []
    return out


def _revisit_atom_lines(day: str) -> List[Tuple[str, str]]:
    """H-5：``review_after`` 到期只用于**整理叙事**（提示重看这段见闻）。

    不判定真假、不升格为事实、不改任何状态或分数（INV-14）；产出仅作为
    ``[回看]`` 素材行并进当天日记，由叙事本身承载「后来有没有被补充/推翻」。
    """
    out: List[Tuple[str, str]] = []
    try:
        conn = open_brain()
        try:
            rows = conn.execute(
                "SELECT content FROM atoms WHERE state='active' "
                "AND review_after != '' AND review_after <= ? LIMIT 10", (day,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return out
    for (content,) in rows:
        body = str(content or "").strip()
        if body:
            out.append((day, f"[回看] {body[:200]}"))
    return out


def maintain_diary(brain_cfg: Optional[Dict[str, Any]],
                   day: str = "") -> int:
    """日记步（plan persona-social-memory M2）：巩固期把「助手的一天」写成第一人称日记。

    每个当日有活动的项目一篇，落 L2 ``diary-<日期>.md``（独立配额、维护 agent 只读）。
    写**昨日**（D+1 语义，2026-11-01 用户确认）：巩固期何时跑都不缺素材——当天尾部
    永远完整（旧「写今天」语义会把晚间素材永久关在日记外）。
    输入 = 该日会话记录（跨会话文件合并；全局尾窗 + 每会话保底，见 _DIARY_* 常数）
           + 该日完成任务标题；身份消歧走记录结构 + 登记的名字锚——零写死，
           换账号/换渠道不失效。知识面零重复——日记只收经历叙事，白卷（空串）是正确答案。
    门控 ``knowledge.diary.enabled``（缺省开）；幂等：该日已有日记则跳过。
    ``day`` 为空 = 昨日（正常巩固路径）；显式传 ``YYYY-MM-DD`` 用于**历史补跑**
    （已有 3 天素材时一次性把旧日记补出来，不必等每天巩固）。
    返回写出的篇数；任何失败静默（巩固链不可被日记拖垮）。

    D-1（2026-11-06）：日期统一**本地日历日**（``localtime.local_date()`` 折算）——
    存储 ts 仍 UTC，主人傍晚的经历不再被 UTC 日界拆进"昨天"。素材的真实 ts 首尾
    折成时间区间，随写块进 META 并 upsert 叙事索引（场合/对象标注 best-effort）。
    """
    if not enabled() or not brain_cfg:
        return 0
    try:
        import config
        if not bool(config.get_config("knowledge.diary.enabled", True)):
            return 0
    except Exception:
        pass
    from omni_core.local import localtime
    from omni_core.local import scene_executor

    day = (day or "").strip() or localtime.local_days_ago(1)
    written = 0
    # 该日会话记录按项目分组：**跨会话文件合并**（一个项目可有多个 peer 会话文件，
    # 逐文件覆盖赋值会把同项目其它会话的素材整段丢掉——2026-11-01 实测踩过）；
    # 尾部优先扫描；同时记每文件尾部若干行（会话保底用）。
    # D-1：素材按 local_date(ts) 归日；同时收真实 ts 首尾（时间区间）与
    # context_key / speaker（场合与对象标注，进 META 与叙事索引）。
    by_project: Dict[str, List[Tuple[str, str]]] = {}
    file_tails: Dict[str, Dict[str, List[Tuple[str, str]]]] = {}
    span: Dict[str, Dict[str, Any]] = {}
    try:
        for f in rp.projects_root().glob("*/s_*.jsonl"):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            recs: List[Tuple[str, str]] = []
            meta_acc: Dict[str, Any] = {"ts_min": "", "ts_max": "",
                                        "context_keys": set(), "peers": set()}
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = str(rec.get("ts") or "")
                if localtime.local_date(ts) != day:
                    if recs:
                        break       # 已收集到该日的、遇到更早的即停（新记录在尾部）
                    continue
                role = str(rec.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                content = str(rec.get("content") or "").strip()[:300]
                if content:
                    spk = str(rec.get("speaker") or "")
                    # P6-1：原话片段带结构化说话人，让共同回忆有「谁说的」的质感
                    line = f"[{role}] {spk}: {content}" if spk else f"[{role}] {content}"
                    recs.append((ts, line))
                    if ts:
                        meta_acc["ts_min"] = min(meta_acc["ts_min"] or ts, ts)
                        meta_acc["ts_max"] = max(meta_acc["ts_max"] or ts, ts)
                    cid = str(rec.get("context_key") or "").strip()
                    if cid:
                        meta_acc["context_keys"].add(cid)
                    if role == "user" and spk:
                        meta_acc["peers"].add(spk)
            if recs:
                pid = f.parent.name
                by_project.setdefault(pid, []).extend(recs)
                file_tails.setdefault(pid, {})[f.name] = recs[-_DIARY_FLOOR_LINES:]
                acc = span.setdefault(pid, {"ts_min": "", "ts_max": "",
                                            "context_keys": set(), "peers": set()})
                acc["ts_min"] = min(acc["ts_min"] or meta_acc["ts_min"], meta_acc["ts_min"]) \
                    if meta_acc["ts_min"] else acc["ts_min"]
                acc["ts_max"] = max(acc["ts_max"] or meta_acc["ts_max"], meta_acc["ts_max"]) \
                    if meta_acc["ts_max"] else acc["ts_max"]
                acc["context_keys"].update(meta_acc["context_keys"])
                acc["peers"].update(meta_acc["peers"])
    except Exception:
        pass
    # P6-1（§16.3）/ N-17（§14.2）：当日观察见闻进素材——「我今天自己去看了什么」
    # 与对话经历同一篇、第一人称；纯观察日（没聊天但有事做）也算有素材的一天。
    obs_items = _diary_observation_lines(day) + _revisit_atom_lines(day)
    # life 事件层交接（worklog lifelike-presence §10，2026-11-10）：项目 days/<day>.md
    # 是助手这一天的客观流水（life 侧产出、给人看），巩固期读进来由助手取舍写进
    # 主观日记——不逐字入库、不占日记键，只是素材。
    life_items = _diary_life_day_lines(day)
    if not by_project and not obs_items and not life_items:
        return 0
    # 该日完成任务（标题级一句话；任务明细走轨迹，日记只收经历）
    try:
        from omni_core.local.task_store import TaskStore

        done_day = [t for t in TaskStore.list()
                    if localtime.local_date(str(t.get("finished_at") or "")) == day]
    except Exception:
        done_day = []
    # H-4（§11）：跨项目聚合成「助手的一天」——一次 LLM、全局一篇，不再按 pid 各写一篇
    try:
        if (scene_executor.diary_dir() / f"diary-{day}.md").exists():
            return 0                                    # 一日一篇，幂等
        all_items = [x for items in by_project.values() for x in items] + obs_items
        # 素材 v3：全局尾窗 ∪ 每会话保底（保证被看见），按 ts 排序、硬顶封 token
        chosen = set(sorted(all_items, key=lambda x: x[0])[-_DIARY_TAIL_LINES:])
        for tails in file_tails.values():
            for tail in tails.values():
                chosen.update(tail)
        recs = [line for _, line in sorted(chosen, key=lambda x: x[0])]
        recs = recs[-_DIARY_MAX_LINES:]
        material = "\n".join(recs)
        life_lines = "\n".join(line for _, line in life_items)
        if life_lines:
            # 生活流水整段必进素材（不进尾窗裁剪）——这是「模拟生活带上」的入料口
            material += "\n\n[今天的生活]\n" + life_lines
        task_lines = "\n".join(
            f"- 完成任务：{str(t.get('title') or t.get('objective') or '')[:60]}"
            for t in done_day)
        if task_lines:
            material += "\n\n[当日完成任务]\n" + task_lines
        if len(material.strip()) < 80:
            return 0                                    # 素材太薄，白卷
        system = _DIARY_SYSTEM + (
            "\n今天的事可能发生在几个不同场合。写**一篇**连贯的"
            "第一人称日记，按场合自然分段，别写成互不相干的流水账，也不要标项目名。")
        known = self_names()
        if known:
            system += (
                "\n身份锚：以下是「你」在本设备的已知名字（任一出现都指你自己，"
                f"不是聊天对象）：{'、'.join(known)}。")
        # 字数帽 1800 + reasoning 思考余量：日记调用预算单独抬到 8000
        data = llm_judge.chat_json(_batch_llm_cfg(brain_cfg, floor=8000), system, material, timeout=120.0)
        diary = str((data or {}).get("diary") or "").strip()
        if not diary:
            return 0
        topics = _normalize_topics((data or {}).get("topics"))
        # P6-1「可回味」元数据：气氛词面（同一次调用顺带产出；非字符串按空处理，≤24 字）
        _atm = (data or {}).get("atmosphere")
        atmosphere = (_atm if isinstance(_atm, str) else "").strip()[:24]
        # 场合/对象/时间区间跨项目合并——索引仍是一行（键 = 日期）
        context_keys: set = set()
        peers: set = set()
        ts_min = ts_max = ""
        for acc0 in span.values():
            context_keys.update(acc0.get("context_keys") or [])
            peers.update(acc0.get("peers") or [])
            if acc0.get("ts_min"):
                ts_min = min(ts_min or acc0["ts_min"], acc0["ts_min"])
            if acc0.get("ts_max"):
                ts_max = max(ts_max or acc0["ts_max"], acc0["ts_max"])
        # P6-2：共同经历标记——素材行出现配置参与人的名字才标（取值来自配置，INV-4；
        # 不进 atoms、不参与四态，只是叙事 META/索引标注）。
        we_hit = sorted({n for n in _we_participants()
                         if any(n in ln for ln in recs)})[:6]
        if scene_executor.write_diary_block(
                "", day, diary, topics=topics,
                context_keys=sorted(context_keys), peers=sorted(peers),
                time_from=ts_min, time_to=ts_max,
                atmosphere=atmosphere, we_participants=we_hit):
            written += 1
            # summary 与 META 同源（回读刚写的块，取 parse 出的首句摘要）
            _raw = scene_executor.read_scene_block("", f"diary-{day}.md")
            _meta, _ = scene_executor.parse_scene_block(_raw)
            upsert_narrative_index(
                f"diary-{day}.md", "diary", project_id="",
                date_from=day, date_to=day,
                time_from=ts_min, time_to=ts_max,
                summary=str(_meta.get("summary") or ""),
                topics=topics,
                context_keys=sorted(context_keys), peers=sorted(peers),
                atmosphere=atmosphere,
                date_basis="local")
    except Exception:
        return 0
    return written


#: 日记补齐回看窗口（2026-11-03 用户定）：巩固期除昨日外，把窗口内缺日记的日子一并补写；
#: 超窗的缺口放弃——时效已过，且 L0-消息不受影响（conversation_search 仍可查回原话）。
#: 背景：用户不会时刻开着 agent，整日不在线时「只写昨日」会让那天的日记永久缺失
#: （2026-11-02 本地实测确认的洞；10-29/10-30 两篇当时就是靠显式 day 人工补跑出来的）。
_DIARY_CATCHUP_DAYS = 7


def _catchup_diary_days(now: Optional[datetime] = None) -> List[str]:
    """巩固期日记补齐的日期表（**本地日**，D-1）：昨日优先，共 ``_DIARY_CATCHUP_DAYS`` 天。

    显式补跑（直接调 ``maintain_diary(day=...)``）不受此窗口限制——本函数只服务巩固路径。
    """
    from omni_core.local import localtime

    return [localtime.local_days_ago(i) for i in range(1, _DIARY_CATCHUP_DAYS + 1)]


def maintain_diaries(brain_cfg: Optional[Dict[str, Any]]) -> int:
    """日记步总入口（巩固期第 8 步，补齐模式）：对窗口内每天独立尝试 ``maintain_diary``。

    已有日记幂等跳过、素材太薄白卷跳过（两者都不调 LLM）；只有「缺日记且有素材」的
    日子才产生一次 LLM 调用，单次巩固最多 ``_DIARY_CATCHUP_DAYS`` 次。
    """
    written = 0
    for day in _catchup_diary_days():
        try:
            written += maintain_diary(brain_cfg, day=day)
        except Exception:
            continue
    return written


_REL_SYSTEM = (
    "你是助手本人。下面这些「他说过的话」来自你与某个人的往来（可能跨多个场合）。"
    "写你对这个人的印象：第一人称、口语、三行以内；只写材料里能看出来的东西，"
    "不编造、不给 TA 的人格下判决。\n"
    "注意：材料里可能出现同名对象，出现的名字不一定指你；"
    "只根据说话内容判断，别把称呼当成你自己。\n"
    "哪怕只有几轮往来也尽量写出能看出的东西；确实看不出才整卡写「还不熟」。\n"
    "格式：\n最近交手：<这段时间发生过什么，一两句>\n"
    "他对我的态度：用你自己的话概括\n我对他的态度：用你自己的话概括\n"
    "topics：2-5 个可检索的主题词或短语（我们之间的事、共同的梗），名词优先，"
    "取自往来素材；不要整句、不要评价；没有则空数组。"
    '只返回 JSON:{"card": "...", "topics": ["...", "..."]}'
)

#: 主人卡专用模板（2026-11-11）：主人跨表面出现（控制台/单聊/多人场合），且画像需要
#: **稳定背景区**（设备/项目/长期偏好）——滚动印象不冲掉背景（背景保留由代码兜底，
#: 不只靠 prompt：LLM 丢了背景时代码补写回）。
_REL_MASTER_SYSTEM = (
    "你是助手本人。下面这些「他说过的话」来自你与**主人**的往来（可能跨多个"
    "场合混在一起），另附一份「现有稳定背景」。\n"
    "写你对主人的印象卡：稳定背景**完整保留**（只在素材明确支持时增补或修正个别"
    "条目，绝不删漏），印象部分只写材料里能看出来的东西，不编造、不下判决。\n"
    "格式：\n【稳定背景】\n<逐条保留并按需增补>\n"
    "【最近交手】<这段时间发生过什么，一两句>\n"
    "【他对我的态度】用你自己的话概括\n【我对他的态度】用你自己的话概括\n"
    "topics：2-5 个可检索的主题词或短语，名词优先；没有则空数组。"
    "素材里关于称呼与身份的内容（他叫我什么、他自我介绍自己是谁、我们怎么认识的）"
    "属于稳定背景，写进【稳定背景】；name 取素材里最新的对助手的称呼"
    "（没有明确的称呼变化则给空串）。"
    '只返回 JSON:{"card": "...", "topics": ["..."], "name": ""}'
)

#: topics 钳制上限（§13.10.2）：项数与单条长度。这是钳制不是词表（INV-4）——
#: 任何值都收，超长截断、脏值丢弃，语义判断权在生成侧。
_TOPICS_MAX_ITEMS = 5
_TOPICS_ITEM_CHARS = 24


def _normalize_topics(raw: Any) -> List[str]:
    """模型返回的 topics → 干净短语列表（≤5 项、每项 ≤24 字）；异常返回空列表。

    只收 list/tuple；逐项 str().strip() 截断，丢空串与非字符串；去重保序。
    脏值只影响索引词面，不阻断写块（§13.10.3）。
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        t = item.strip()[:_TOPICS_ITEM_CHARS]
        if t and t not in out:
            out.append(t)
        if len(out) >= _TOPICS_MAX_ITEMS:
            break
    return out

#: 关系卡步的边界：每人至少这么多条往来才写卡；每轮最多更新几人；每个会话文件只扫尾部
_REL_MIN_LINES = 3
_REL_MAX_PEERS = 5
_REL_TAIL_LINES = 600
#: 主人卡正文上限比普通卡宽（稳定背景区 + 印象三行）
_REL_MASTER_MAX_CHARS = 1400


def _card_meta_name(path) -> str:
    """读关系卡 META 的 ``name:``（无卡/无字段/异常返回空——调用方自行回退）。"""
    try:
        if not path.exists():
            return ""
        meta = path.read_text(encoding="utf-8").split("-----META-END-----", 1)[0]
        for ln in meta.splitlines():
            s = ln.strip()
            if s.startswith("name:"):
                return s[len("name:"):].strip()[:40]
    except Exception:
        pass
    return ""


def _card_stable_background(path) -> str:
    """读现有主人卡的【稳定背景】段；无卡/无段/异常返回空。

    段以 ``【稳定背景】`` 行起，到下一个 ``【`` 开头的段首或正文末尾止。
    """
    try:
        if not path.exists():
            return ""
        body = path.read_text(encoding="utf-8").split("-----META-END-----", 1)[-1]
        out: List[str] = []
        inside = False
        for ln in body.splitlines():
            s = ln.strip()
            if s.startswith("【稳定背景】"):
                inside = True
                continue
            if inside:
                if s.startswith("【"):
                    break
                out.append(ln)
        return "\n".join(out).strip()
    except Exception:
        return ""


def _peer_lines(limit_days: int = 7) -> Dict[str, Dict[str, Any]]:
    """按结构化 speaker 汇总「某人说过的话」——跨场合、跨会话合并。

    speaker 由边界层写入 ``l0_messages``（``name(uid)`` 形式由适配层归一）；
    内核只按 uid 分组，不解析任何文本格式（INV-4）。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, limit_days))
              ).strftime("%Y-%m-%d")
    out: Dict[str, Dict[str, Any]] = {}
    try:
        conn = open_brain()
        try:
            rows = conn.execute(
                "SELECT speaker, text, recorded_at FROM l0_messages "
                "WHERE role='user' AND speaker != '' AND recorded_at >= ? "
                "ORDER BY recorded_at DESC LIMIT 400", (cutoff,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return out
    for speaker, text, _ts in rows:
        value = str(speaker or "").strip()
        if "(" not in value or not value.endswith(")"):
            continue
        name, _, uid = value.rpartition("(")
        uid = uid.rstrip(")")
        if not uid or not name:
            continue
        item = out.setdefault(uid, {"name": name.strip(), "lines": []})
        body = str(text or "").strip()
        if body:
            item["lines"].append(body[:160])
    return out


def list_speakers(limit_days: int = 7, min_lines: int = 3, q: str = "",
                  limit: int = 20, exclude_uids=None) -> List[Dict[str, Any]]:
    """认领候选：按 uid 聚合的说话人列表（name/uid/条数/最近活跃时间）。

    - ``q`` 为空：近 ``limit_days`` 窗口、≥ ``min_lines`` 条的**活跃**说话人，
      按条数降序（与关系卡维护同门槛——聊一次就消失的人不进列表，零噪音）；
    - ``q`` 非空：**全量不限时窗**，按 name/uid 前缀匹配（认领列表外找旧人）。
    ``exclude_uids`` 里的 uid 不返回（已认领名单 + 主人 key）。
    uid 解析口径与 :func:`_peer_lines` 一致（``name(uid)`` 后缀，Python 侧比对）。
    """
    from omni_core.local.identity import speaker_uid
    try:
        exclude = {str(u).strip() for u in (exclude_uids or []) if str(u or "").strip()}
        cutoff = ""
        if not q:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=max(1, limit_days))).strftime("%Y-%m-%d")
        conn = open_brain()
        try:
            if cutoff:
                rows = conn.execute(
                    "SELECT speaker, count(*), max(recorded_at) FROM l0_messages "
                    "WHERE role='user' AND speaker != '' AND recorded_at >= ? "
                    "GROUP BY speaker", (cutoff,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT speaker, count(*), max(recorded_at) FROM l0_messages "
                    "WHERE role='user' AND speaker != '' "
                    "GROUP BY speaker").fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    agg: Dict[str, Dict[str, Any]] = {}
    for speaker, cnt, last_at in rows:
        s = str(speaker or "")
        uid = speaker_uid(s)
        if not uid or uid in exclude:
            continue
        name = s.rpartition("(")[0].strip() or uid
        item = agg.setdefault(uid, {"name": name, "uid": uid, "lines": 0, "last": ""})
        item["lines"] += int(cnt or 0)
        if str(last_at or "") > item["last"]:
            item["last"] = str(last_at or "")
    out = list(agg.values())
    qk = str(q or "").strip().lower()
    if qk:
        out = [x for x in out
               if x["name"].lower().startswith(qk) or x["uid"].lower().startswith(qk)]
    else:
        out = [x for x in out if x["lines"] >= max(1, min_lines)]
    out.sort(key=lambda x: x["lines"], reverse=True)
    return out[:max(1, limit)]


def maintain_relations(brain_cfg: Optional[Dict[str, Any]],
                       limit_days: int = 7) -> int:
    """关系卡步：按**人**把这段时间的往来压成一张印象卡（全局一份，不按群/项目拆）。

    落点 ``~/.omniagent/memory/relations/<uid>.md``（META: name/uid/updated + 正文三行；
    主人卡 uid 为合成 key ``master``，见 local/identity.py）。只写"记录里能看出来"
    的东西，做得越久越像关系而不是账本；信息不足写「还不熟」。
    返回更新张数；任何失败静默（巩固链不可被关系卡拖垮）。
    """
    if not enabled() or not brain_cfg:
        return 0
    try:
        import config
        if not bool(config.get_config("knowledge.relations.enabled", True)):
            return 0
    except Exception:
        pass
    peers = _peer_lines(limit_days)
    cand = sorted(((u, d) for u, d in peers.items() if len(d["lines"]) >= _REL_MIN_LINES),
                  key=lambda kv: len(kv[1]["lines"]), reverse=True)[:_REL_MAX_PEERS]
    if not cand:
        return 0
    d = rp.global_relations()
    d.mkdir(parents=True, exist_ok=True)
    written = 0
    for uid, info in cand:
        if _maintain_one(brain_cfg, d, uid, info):
            written += 1
    return written


def maintain_master_card(brain_cfg: Optional[Dict[str, Any]],
                         limit_days: int = 90) -> bool:
    """只重写主人卡（认领后的宽窗重建 / Web 单独触发用），不碰别人的卡。

    与 :func:`maintain_relations` 同一套写卡逻辑，但素材窗可放宽（认领时回填过的
    历史不白费——不用等主人再攒 3 句新话，卡片立刻有血肉）。
    """
    if not enabled() or not brain_cfg:
        return False
    try:
        import config
        if not bool(config.get_config("knowledge.relations.enabled", True)):
            return False
    except Exception:
        pass
    from omni_core.local.identity import MASTER_KEY, master_speaker
    peers = _peer_lines(limit_days)
    info = peers.get(MASTER_KEY)
    if not info or len(info.get("lines") or []) < _REL_MIN_LINES:
        return False
    if not info.get("name"):
        info["name"] = master_speaker().rpartition("(")[0]
    d = rp.global_relations()
    d.mkdir(parents=True, exist_ok=True)
    return _maintain_one(brain_cfg, d, MASTER_KEY, info)


def _maintain_one(brain_cfg: Dict[str, Any], d, uid: str, info: Dict[str, Any]) -> bool:
    """单张关系卡的维护体（maintain_relations 循环体提取；主人分支按合成 key 判定）。"""
    try:
        material = "\n".join(f"- {t}" for t in info["lines"][-40:])
        # 主人卡（2026-11-11）：跨表面聚合 + 稳定背景区。素材带现有背景，
        # LLM 丢了背景时代码补写回（背景保留是硬要求，不只靠 prompt）。
        is_master = is_master_card_uid(uid)
        if is_master:
            bg = _card_stable_background(d / f"{uid}.md")
            existing_name = _card_meta_name(d / f"{uid}.md")
            system = _REL_MASTER_SYSTEM
            prompt = (f"现有稳定背景（必须完整保留，只增不删）：\n{bg or '（暂无）'}\n\n"
                      f"主人说过的话（按时间，最多 40 条，跨表面）：\n{material}")
        else:
            bg = ""
            system = _REL_SYSTEM.replace("{name}", str(info.get("name") or uid))
            prompt = f"对方说过的话（按时间，最多 40 条）：\n{material}"
        # 注意：模板里含 JSON 花括号，不能用 str.format（会把 {"card": ...} 当占位符）
        data = llm_judge.chat_json(brain_cfg, system, prompt, timeout=60.0)
        card = str((data or {}).get("card") or "").strip()
        if not card:
            return False
        if is_master and bg and "【稳定背景】" not in card:
            card = f"【稳定背景】\n{bg}\n" + card
        topics = _normalize_topics((data or {}).get("topics"))
        # 主人卡称呼（2026-11-12）：名字的唯一宿主是这张卡——素材里有明确称呼时
        # 维护者采纳进 META（identity 署名标签读这里）；没介绍/没给出 = 原样保留
        #（程序一致性：从不介绍 = 恒为缺省称呼）。regular 卡称呼仍由平台承载。
        card_name = info.get("name") or uid
        if is_master:
            new_name = str((data or {}).get("name") or "").strip()[:40]
            card_name = new_name or existing_name or card_name
        meta_lines = (f"name: {card_name}\nuid: {uid}\n"
                      f"updated: {_now()}\n")
        if topics:
            meta_lines += f"topics: {json.dumps(topics, ensure_ascii=False)}\n"
        # P6-2：关系卡上的共同经历标记——素材里出现配置参与人才标（INV-4，取值
        # 来自配置；不进 atoms，只是卡片 META 标注）。
        we_hit = [n for n in _we_participants() if n in material][:6]
        if we_hit:
            meta_lines += f"we: {json.dumps(we_hit, ensure_ascii=False)}\n"
        body = ("-----META-START-----\n" + meta_lines + "-----META-END-----\n"
                + card[:_REL_MASTER_MAX_CHARS if is_master else 600])
        (d / f"{uid}.md").write_text(body, encoding="utf-8")
        peer_label = f"{card_name}({uid})"
        # 索引摘要只放可被外部场合看到的中性信息（称呼/标识/往来量/话题），
        # 不含印象正文：外部来源读 relation 只给 summary（N-10），摘要同时是
        # 检索词面，让按名字/标识的 narrative_search 能命中这张卡。
        digest = f"与 {peer_label} 的往来印象卡；近段 {len(info['lines'])} 条往来"
        if topics:
            digest += "；话题：" + "、".join(topics[:3])
        upsert_narrative_index(uid, "relation", project_id="",
                               summary=digest, topics=topics,
                               context_keys=[], peers=[peer_label],
                               date_basis="local")
        return True
    except Exception:
        return False


def is_master_card_uid(uid: str) -> bool:
    """uid 是否为主人卡（合成 key；薄封装，避免调用方 import identity 内部细节）。"""
    from omni_core.local.identity import is_master_key
    return is_master_key(uid)


def restamp_speaker_as_master(uid: str) -> int:
    """认领回填：该 uid 的历史 user 行 speaker 重盖章为 ``主人(master)``。

    只动 l0 检索表（关系卡素材源；回填后旧消息立即并入主人分组）；会话 jsonl 是
    文件真相、显示名保留原样。uid 匹配口径与 :func:`_peer_lines` 一致——
    ``name(uid)`` 后缀解析，在 Python 侧精确比对（不用 SQL LIKE，免转义坑）。
    返回重盖章行数；异常返回 0（回填失败不阻断认领，后续新消息照常归一）。
    """
    from omni_core.local.identity import master_speaker, speaker_uid
    uid = str(uid or "").strip()
    if not uid:
        return 0
    target = master_speaker()
    try:
        conn = open_brain()
        try:
            rows = conn.execute(
                "SELECT DISTINCT speaker FROM l0_messages "
                "WHERE role='user' AND speaker != ''").fetchall()
            hits = [str(s) for (s,) in rows if speaker_uid(str(s)) == uid]
            if not hits:
                return 0
            n = 0
            for s in hits:
                cur = conn.execute(
                    "UPDATE l0_messages SET speaker=? WHERE speaker=? AND role='user'",
                    (target, s))
                n += cur.rowcount or 0
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception:
        return 0


def retire_relation_card(uid: str) -> bool:
    """注销/收编一张关系卡：删正文文件 + 撤叙事索引（admin 动作，不进 agent 写路径）。

    主人卡（合成 key）不允许从这里删——删了身份锚就断了；重置主人卡走认领解除
    之外的人工路径。返回是否删了正文文件（索引撤除尽力而为）。
    """
    uid = str(uid or "").strip()
    if not uid or is_master_card_uid(uid):
        return False
    deleted = False
    try:
        p = rp.global_relations() / f"{uid}.md"
        if p.exists():
            p.unlink()
            deleted = True
    except Exception:
        pass
    delete_narrative_index(uid, "relation")
    return deleted


#: 关系卡直驱节流：与写链同款（新增带结构化 speaker 的 L0 行数 / 距上次维护的间隔）。
#: 游标与时间戳存 meta（k/v，无新表、无迁移）；首轮只记基线，避免冷启动灌全库进 LLM。
_REL_MIN_NEW_LINES = 12
_REL_MIN_INTERVAL_SEC = 600.0
_REL_META_CURSOR = "relations_l0_cursor"
_REL_META_LAST = "last_relations_maintenance"


def maybe_maintain_relations(brain_cfg: Optional[Dict[str, Any]]) -> int:
    """按 L0 直驱关系卡维护，不依赖 L1 原子与巩固周期。

    素材是 ``l0_messages`` 里带结构化 speaker 的 user 行（跨场合合并），
    所以不产原子的轮次也能持续更新印象卡。节流按新增行数 + 间隔；
    失败静默，不阻断收尾。
    """
    import time as _time

    if not enabled() or not brain_cfg:
        return 0
    try:
        conn = open_brain()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM l0_messages WHERE role='user' AND speaker != ''"
            ).fetchone()[0]
            row = conn.execute("SELECT v FROM meta WHERE k=?",
                               (_REL_META_CURSOR,)).fetchone()
            cursor = int(row[0]) if row else None
            row = conn.execute("SELECT v FROM meta WHERE k=?",
                               (_REL_META_LAST,)).fetchone()
            last = float(row[0]) if row else 0.0
        finally:
            conn.close()
        now = _time.time()
        if cursor is None:
            written = 0
        elif total - cursor >= _REL_MIN_NEW_LINES and now - last >= _REL_MIN_INTERVAL_SEC:
            written = maintain_relations(brain_cfg)
        else:
            return 0
        conn = open_brain()
        try:
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                         (_REL_META_CURSOR, str(total)))
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                         (_REL_META_LAST, str(now)))
            conn.commit()
        finally:
            conn.close()
        return written
    except Exception:
        return 0


def maintain_month_diary(brain_cfg: Optional[Dict[str, Any]]) -> int:
    """月记步（plan persona-social-memory M2.5）：把**已结束月份**的日记压缩为月记块。

    月记 ``diary-month-<月份>.md`` 是消费地图（META.summary = 关键人物/事件一行式）——
    助手据此知道该翻哪几篇旧日记。规则：只压已结束月份；<3 篇不压；已有月记幂等跳过；
    失败静默。门控同日记步。月份口径 D-1：**本地日**的当月（``localtime.local_today``）。
    """
    if not enabled() or not brain_cfg:
        return 0
    try:
        import config
        if not bool(config.get_config("knowledge.diary.enabled", True)):
            return 0
    except Exception:
        pass
    from omni_core.local import localtime
    from omni_core.local import scene_executor

    current_month = localtime.local_today()[:7]
    written = 0
    # H-4：月记也按**月份**全局一篇，不再按 project 各压一份
    by_month: Dict[str, List[str]] = {}
    try:
        for d in scene_executor.diary_dir().glob("diary-2*.md"):
            name = d.name
            if name.startswith("diary-month-"):
                continue
            ym = name[len("diary-"):len("diary-") + 7]   # diary-YYYY-MM-DD.md → YYYY-MM
            if len(ym) != 7 or ym >= current_month:
                continue                                  # 未结束月份不压
            by_month.setdefault(ym, []).append(name)
    except Exception:
        pass
    for ym, files in sorted(by_month.items()):
        try:
            if len(files) < 3:
                continue                                  # 单薄月份不成篇
            parts: List[str] = []
            for name in sorted(files):
                raw = scene_executor.read_scene_block("", name)
                _, body = scene_executor.parse_scene_block(raw)
                if body:
                    parts.append(body[:400])
            material = "\n\n".join(parts)
            if len(material) < 120:
                continue
            data = llm_judge.chat_json(_batch_llm_cfg(brain_cfg), _DIARY_MONTH_SYSTEM, material, timeout=90.0)
            summary = str((data or {}).get("summary") or "").strip()[:80]
            body = str((data or {}).get("month_diary") or "").strip()
            if not body:
                continue
            topics = _normalize_topics((data or {}).get("topics"))
            d1, d2 = localtime.local_month_bounds(ym)
            content = f"{summary}\n\n{body}" if summary else body
            if scene_executor.write_diary_block("", f"month-{ym}", content, topics=topics):
                written += 1
                _raw = scene_executor.read_scene_block("", f"diary-month-{ym}.md")
                _meta, _ = scene_executor.parse_scene_block(_raw)
                upsert_narrative_index(f"diary-month-{ym}.md", "month", project_id="",
                                       date_from=d1, date_to=d2,
                                       summary=str(_meta.get("summary") or ""),
                                       topics=topics, date_basis="local")
        except Exception:
            continue
    return written


def remember(text: str, kind: str = "fact", importance: float = 0.8,
             brain_cfg: Optional[Dict[str, Any]] = None,
             origin: str = "", source_task: str = "remember") -> str:
    """显式写入一条记忆（agent/用户主动）。返回 record_id；失败返回空串。"""
    text = (text or "").strip()[:_MAX_ATOM_CHARS]
    if not text or not enabled():
        return ""
    kind = kind if kind in ("fact", "preference", "rule", "episode") else "fact"
    try:
        importance = max(0.0, min(1.0, float(importance)))
    except Exception:
        importance = 0.5
    conn = open_brain()
    try:
        now = _now()
        rid = _new_id()
        _insert_atom(conn, rid, {"text": text, "kind": kind, "importance": importance},
                     origin, source_task, now)
        conn.commit()
        return rid
    except Exception:
        return ""
    finally:
        conn.close()


def update_memory(record_id: str, new_text: str) -> str:
    """更正记忆：旧条 tombstone(superseded_by=新条)，新条入库。返回新 record_id 或空串。"""
    new_text = (new_text or "").strip()[:_MAX_ATOM_CHARS]
    if not new_text or not enabled():
        return ""
    conn = open_brain()
    try:
        row = conn.execute("SELECT record_id FROM atoms WHERE record_id=? "
                           "AND state='active'", (record_id,)).fetchone()
        if row is None:
            return ""
        now = _now()
        rid = _new_id()
        _insert_atom(conn, rid, {"text": new_text, "kind": "fact", "importance": 0.7},
                     "", "update", now)
        conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? "
                     "WHERE record_id=?", (rid, now, record_id))
        conn.commit()
        return rid
    except Exception:
        return ""
    finally:
        conn.close()


def forget(record_id: str, reason: str = "") -> bool:
    """主动遗忘：state → tombstone（保留内容与原因，审计可溯）。"""
    if not enabled():
        return False
    conn = open_brain()
    try:
        cur = conn.execute(
            "UPDATE atoms SET state='tombstone', superseded_by=?, updated=? "
            "WHERE record_id=? AND state='active'",
            (f"forget:{(reason or '')[:160]}", _now(), record_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        conn.close()


# ---- 自检 / 工具支撑 ----

def overview_brain() -> Dict[str, Any]:
    conn = open_brain()
    try:
        total = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM atoms WHERE state='active'").fetchone()[0]
        by_kind: Dict[str, int] = {}
        for kd, n in conn.execute("SELECT kind, COUNT(*) FROM atoms "
                                  "WHERE state='active' GROUP BY kind"):
            by_kind[kd] = n
        last = (conn.execute("SELECT v FROM meta WHERE k='last_consolidation'").fetchone()
                or [""])[0]
        recent = [r[0] for r in conn.execute(
            "SELECT content FROM atoms WHERE state='active' ORDER BY updated DESC LIMIT 5")]
        return {"total": total, "active": active, "by_kind": by_kind,
                "last_consolidation": last, "recent": recent}
    finally:
        conn.close()


def dump_atoms(limit: int = 200) -> List[Dict[str, Any]]:
    """调试/人工面：导出 atoms（状态/强度全字段，按更新时间倒序）。"""
    conn = open_brain()
    try:
        rows = conn.execute(
            "SELECT record_id, content, kind, persona, origin, source_task, importance, "
            "salience, access_count, state, superseded_by, updated FROM atoms "
            "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        keys = ("record_id", "content", "kind", "persona", "origin", "source_task",
                "importance", "salience", "access_count", "state", "superseded_by", "updated")
        return [dict(zip(keys, r)) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def atoms_since(cursor: str = "", project_id: str = "",
                limit: int = 40) -> List[Dict[str, Any]]:
    """L2 进料：上次游标之后新增/被更新过的 **active** L1 原子（updated 升序）。

    **一池增量、不分组**（INV-9，P2 废除 ``origin`` 过滤）：L2 进料取一池原子增量，
    场合标签（``context_key``）只作渲染元信息，不用于过滤批次。``project_id`` 参数保留
    仅作调用兼容，不再参与路由——存储是一池，路由只发生在消费端（§4.3/§11）。

    游标为**复合键** ``updated|record_id``（P1）：同戳批次 + LIMIT 会静默丢料——
    按单一 ``updated`` 划界时，同一时刻落库的多条只有前几条进批、其余被游标跳过。
    老游标（裸 ts）兼容读取。游标空 = 全量起点。
    按 ``updated`` 而非 ``created`` 划界：合并/改写过的一条（裁决结果变了）要重新进一次块；
    被打 tombstone 的旧条不再进（``state='active'`` 过滤）——这条同时保证了
    "旧判词退场"能传达到 L2：被推翻的条款不会再来喂块。
    """
    cur_ts, cur_rid = "", ""
    if cursor:
        parts = str(cursor).split("|", 1)
        cur_ts = parts[0]
        cur_rid = parts[1] if len(parts) > 1 else ""
    conn = open_brain()
    try:
        rows = conn.execute(
            "SELECT record_id, content, kind, importance, created, updated, context_key, "
            "source_kind FROM atoms WHERE state='active' "
            "AND (updated > ? OR (updated = ? AND record_id > ?)) "
            "ORDER BY updated ASC, record_id ASC LIMIT ?",
            (cur_ts, cur_ts, cur_rid, limit)).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [{"id": r[0], "content": r[1], "kind": r[2],
             "importance": r[3], "created_at": r[4], "updated_at": r[5],
             "context_key": r[6] or "", "source_kind": r[7] or "dialogue"} for r in rows]


def cursor_at(atom: Dict[str, Any]) -> str:
    """复合游标值（``updated|record_id``）——``maybe_maintain`` 推进水位用。"""
    return f"{atom.get('updated_at') or ''}|{atom.get('id') or ''}"


def hard_delete(record_id: str) -> bool:
    """人工物理删除（唯一物理删除通道；不进 agent 写入路径）。"""
    if not record_id:
        return False
    conn = open_brain()
    try:
        cur = conn.execute("DELETE FROM atoms WHERE record_id=?", (record_id,))
        conn.execute("DELETE FROM atoms_fts WHERE record_id=?", (record_id,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        return False
    finally:
        conn.close()


# ---- L3 自我画像（brain.db 驱动；人工 PUT /profile 可覆盖） ----
# 「用户画像」概念随多表面拟人部署退役（2026-11-11）：她自己是主体（本块），
# 对话者（含主人）归关系卡，操作者环境归主人卡的稳定背景区。

_PROFILE_AUTO = "knowledge.profile.auto_maintain"        # 缺省开
_PROFILE_TRIGGER = "knowledge.profile.trigger_every_n"   # 每 N 条新 atoms 触发
_PROFILE_MAX_CHARS = 2000
_PROFILE_TS_KEY = "last_profile_ts"
_PROFILE_SIGNAL_KEY = "persona_update_request"

_PROFILE_SYSTEM = (
    "你是角色本人的自我画像维护器。给你「当前自我画像」和「最近沉淀的新事实」。"
    "任务:输出更新后的完整自我画像——保留仍然成立的内容,把新事实中关于"
    "**角色她自己**的部分合并进去:她的兴趣/品味/审美、生活史与习惯、身体特征、"
    "她的能力与工具面;剔除已过时的部分,矛盾的以新事实为准。不要虚构,"
    "没有可合并的新信息就原样返回。"
    "\n类别边界:画像只记录关于角色她自己的事实。以下内容一律剔除,不得合并:"
    "关于对话者/其他人的事实(他们的偏好/身份/经历——那属于关系卡),"
    "关于主人或操作者环境的事实(设备/项目/账号——那属于主人卡),"
    "assistant 的应对策略/拒绝规则/边界设定(属于场景块/角色卡层),"
    "以及**无法判断是关于她还是关于别人的条目**(宁可漏记,不可错记)。"
    "即使新事实以「优先处理」或带外信号标记出现,也照此过滤。"
    "只返回 JSON:{\"profile\": \"<完整画像全文,≤2000字符>\"}"
)


def _meta_get(conn: sqlite3.Connection, key: str) -> str:
    try:
        row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, value))


def request_profile_update(reason: str) -> None:
    """L2→L3 带外信号：落 brain meta，下次巩固强制画像重写（场景 agent 视野最完整）。"""
    reason = (reason or "").strip()
    if not reason:
        return
    conn = open_brain()
    try:
        _meta_set(conn, _PROFILE_SIGNAL_KEY, reason[:500])
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def maintain_profile(brain_cfg: Optional[Dict[str, Any]]) -> bool:
    """L3 自我画像重写：上次水位之后的新 atoms → LLM 增量合并 → self_profile.md（旧版 .bak）。

    - gate ``knowledge.profile.auto_maintain``（缺省开）
    - 触发：新增 active atoms ≥ ``knowledge.profile.trigger_every_n``（缺省 20），
      或 L2 带外信号（``persona_update_request``）强制一次
    - LLM 失败/输出为空 → 不动现文件；信号仍消费（避免永久绕过节流）
    """
    try:
        import config
        if not bool(config.get_config(_PROFILE_AUTO, True)):
            return False
        trigger = int(config.get_config(_PROFILE_TRIGGER, 20) or 20)
        conn = open_brain()
        try:
            signal = _meta_get(conn, _PROFILE_SIGNAL_KEY)
            last_ts = _meta_get(conn, _PROFILE_TS_KEY)
            rows = conn.execute(
                "SELECT content FROM atoms WHERE state='active' AND updated > ? "
                "ORDER BY updated DESC LIMIT 40", (last_ts,)).fetchall()
            if not signal and len(rows) < trigger:
                return False
            if signal:
                _meta_set(conn, _PROFILE_SIGNAL_KEY, "")
                conn.commit()
        finally:
            conn.close()
        pp = rp.self_profile()
        old_profile = pp.read_text(encoding="utf-8") if pp.exists() else ""
        new_facts = "\n".join(f"- {c}" for (c,) in rows)
        if signal:
            new_facts = f"[场景整理请求画像更新(优先处理)]\n{signal}\n\n{new_facts}"
        data = llm_judge.chat_json(
            brain_cfg, _PROFILE_SYSTEM,
            f"当前自我画像:\n{old_profile or '(空)'}\n\n最近新事实:\n{new_facts}",
            timeout=60.0)
        text = str((data or {}).get("profile") or "").strip()
        if not text:
            return False
        pp.parent.mkdir(parents=True, exist_ok=True)
        if pp.exists():
            pp.replace(pp.with_suffix(".md.bak"))  # 上一版画像 → .bak
        pp.write_text(text[:_PROFILE_MAX_CHARS], encoding="utf-8")
        conn2 = open_brain()
        try:
            _meta_set(conn2, _PROFILE_TS_KEY, _now())
            conn2.commit()
        finally:
            conn2.close()
        return True
    except Exception:
        return False
