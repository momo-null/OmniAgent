"""记忆层（单一方案；现行架构见 doc/plans/implemented/memory-architecture.md）。

四层分工：
- L0 会话：``capture`` 收 task_store 落库的消息（进程内 buffer，任务收尾一次性提炼）；
- L1 原子记忆：``flush_brain`` 提炼 → 价值/kind/importance 两段判 → 一致性四态
  （novel/duplicate/merge/conflict）→ brain.db；``search_brain`` 融合排序
  （relevance × importance × recency，active-only，inject_floor）+ 召回即强化；
  ``inject_text_brain`` 出 ``<relevant-memories>`` 文本（仅供工具/调试；每轮自动注入
  已下线，见 plan memory-recall-2026-11-02 P1）；
- L2 场景块：机制与触发在 ``local/scene_executor.py``（本模块只在收尾时调 ``maybe_maintain``）；
- L3 画像：``maintain_profile`` 由巩固周期驱动，把 brain.db 新 atoms 增量重写进
  ``user_profile.md``（读取在 local/knowledge_inject.py）。

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
  updated     TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS atoms_fts USING fts5(
  content, record_id UNINDEXED, tokenize='{tokenizer}');
CREATE INDEX IF NOT EXISTS idx_atoms_state ON atoms(state, salience DESC);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

# P2（2026-11-02，plan memory-recall §P2）：L0 逐字检索副本，对齐原仓库 ``l0_conversations``
# ——「写入时同步插一行到主库」，不是独立索引库。原会话 jsonl 继续作为文件真相，本表只是
# 检索副本（可重建）；``kind`` 区分通道消息与任务输入（两者同为 role=user，防检索串味）。
# 与原仓库的两处有意差异：① 多一列 ``session_id``——Web/API 会话没有 IM 的 chat_id，
# 靠它定位「当前会话」；② 不建 ``timestamp``（消息原始时间戳）——我们只把消息落盘一次，
# ``recorded_at`` 就是那个时间，再存一份空列是死字段。
_L0_SCHEMA = """
CREATE TABLE IF NOT EXISTS l0_messages(
  record_id   TEXT PRIMARY KEY,
  chat_id     TEXT NOT NULL DEFAULT '',
  session_id  TEXT NOT NULL DEFAULT '',
  kind        TEXT NOT NULL DEFAULT 'chat',
  role        TEXT NOT NULL DEFAULT 'user',
  speaker     TEXT NOT NULL DEFAULT '',
  text        TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_l0_chat_time ON l0_messages(chat_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_l0_session ON l0_messages(session_id, recorded_at);
CREATE VIRTUAL TABLE IF NOT EXISTS l0_fts USING fts5(
  text, record_id UNINDEXED, tokenize='{tokenizer}');
"""

_LTM_DEFAULTS: Dict[str, Any] = {
    "persona": "",
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
        target = f"{tok}-1"
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
                "SELECT f.record_id, f.content FROM atoms_fts f "
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


# ---- L0 会话缓冲 → L1 提炼原料 ----
_FLUSH_MIN_ROLES = 2        # buffer 少于此条不触发提炼(无内容可提炼)
_FLUSH_MAX_CHARS = 8000     # 提炼原料(对话片段)截断
_MAX_ATOMS_PER_FLUSH = 5
# buffer 上限（plan §3.3）：barge-in 频繁时（打断轮不 flush，且可能有并发通道）缓冲
# 会无限增长。超限即**滚动切段**——丢最旧的，只保留最近窗口，并记 dropped 计数。
# 之所以不是"超限就强制 flush"：flush 要跑 3 次 LLM，把它塞进消息路径等于把
# 尾延迟搬回主链路，正是本轮要去掉的东西。
_CAPTURE_MAX_MSGS = 80
_CAPTURE_MAX_CHARS = 24000
_buf_lock = threading.RLock()
_buffers: Dict[str, List[Dict[str, str]]] = {}  # pid -> [{role, content}]
_buf_dropped: Dict[str, int] = {}               # pid -> 被切掉的消息条数


def capture(project_id: str, role: str, content: str) -> None:
    """会话消息入 buffer(不提炼;提炼在任务收尾 flush_brain 时批量做)。"""
    if not enabled() or not project_id:
        return
    with _buf_lock:
        buf = _buffers.setdefault(project_id, [])
        buf.append({"role": role, "content": content})
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
_L0_SPEAKER_RE = None       # 惰性编译（见 _l0_speaker）
_L0_CONN: Dict[str, Any] = {"path": "", "conn": None}   # 读写复用连接（见 _l0_conn）


def _l0_speaker(text: str, role: str) -> str:
    """群消息行首的「名片(uid)」「谁(1234): …」说话人标注 → speaker；其余为空。

    assistant 不打 speaker（那是她自己）；私聊/任务输入也没有行首标注。
    """
    global _L0_SPEAKER_RE
    if role != "user" or not text:
        return ""
    import re
    if _L0_SPEAKER_RE is None:
        _L0_SPEAKER_RE = re.compile(r"^\s*[「]?([^「」():\n]{1,32})[」]?\((\d{4,})\)\s*[:：]")
    m = _L0_SPEAKER_RE.match(text)
    return f"{m.group(1)}({m.group(2)})" if m else ""


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


def index_l0_message(session_id: str, role: str, text: str, *, chat_id: str = "",
                     kind: str = "chat", recorded_at: str = "") -> str:
    """会话消息入 L0 检索表（写入即入表；任何异常静默——索引不打断主流程）。

    ``kind``：``chat``=通道消息，``task``=任务输入（cron/手动派发写进会话的那条 user 气泡）。
    返回 record_id；未启用/空文本/失败返回空串。
    """
    text = str(text or "").strip()
    if not text or not enabled() or not (session_id or chat_id):
        return ""
    kind = kind if kind in ("chat", "task") else "chat"
    role = role if role in ("user", "assistant", "system") else "user"
    now = recorded_at or _now()
    body = text[:_L0_TEXT_MAX]
    rid = _l0_id(str(session_id or ""), now, role, body)
    try:
        with _lock:
            conn = _l0_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO l0_messages(record_id, chat_id, session_id, kind, "
                "role, speaker, text, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (rid, str(chat_id or ""), str(session_id or ""), kind, role,
                 _l0_speaker(body, role), body, now))
            if cur.rowcount:
                conn.execute("INSERT OR IGNORE INTO l0_fts(record_id, text) VALUES(?,?)",
                             (rid, body))
                conn.commit()
        return rid
    except Exception:
        return ""


def _l0_cutoff(days: Any) -> str:
    try:
        d = max(1, int(days))
    except Exception:
        d = 30
    return (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()


def search_l0(keyword: str, *, chat_id: str = "", session_id: str = "",
              days: Any = 30, limit: int = 8) -> List[Dict[str, Any]]:
    """L0 逐字检索（FTS + 时间窗）→ ``[{when, who, role, text, chat_id, kind}]``，时间正序。

    范围（ACL 由调用方决定，本函数只认参数）：传 ``chat_id`` / ``session_id`` = 只查该聊天窗口
    （两个键取并集：实时写入行两个都有，回填行只有 session_id）；两者皆空 = 全部会话。
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
        # 「当前会话」有两个键：IM 的真实 chat_id（group:123/private:100）与会话文件名 session_id。
        # 回填的老行只有 session_id、实时写入两个都有——范围条件取并集，否则回填历史查不到。
        cid = str(chat_id or "")
        sid = str(session_id or "")
        k1 = cid or sid
        k2 = sid or k1
        scope, params = "", []
        if k1:
            scope = "AND (l.chat_id = ? OR l.session_id = ?)"
            params = [k1, k2]
        base = (f"SELECT l.record_id, l.chat_id, l.session_id, l.kind, l.role, l.speaker, "
                f"l.text, l.recorded_at FROM l0_messages l "
                f"JOIN l0_fts f ON f.record_id = l.record_id "
                f"WHERE l0_fts MATCH ? AND l.recorded_at >= ? {scope} "
                f"ORDER BY bm25(l0_fts) LIMIT ?")
        rows: List[Any] = []
        if match:
            try:
                rows = c.execute(base, tuple([match, cutoff] + params + [max(n * 3, 20)])).fetchall()
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
                        "text": row[6], "chat_id": row[1], "kind": row[3]})

        for row in rows:
            _push(row)
        # LIKE 兜底：2 字词在 trigram 降级模式下 MATCH 无结果（同 atoms 的老坑）
        for kw in like_terms:
            lsql = ("SELECT l.record_id, l.chat_id, l.session_id, l.kind, l.role, l.speaker, "
                    "l.text, l.recorded_at FROM l0_messages l WHERE l.text LIKE ? "
                    "AND l.recorded_at >= ? " + scope + " ORDER BY l.recorded_at DESC LIMIT ?")
            try:
                for row in c.execute(lsql, tuple([f"%{kw}%", cutoff] + params + [n])).fetchall():
                    _push(row)
            except Exception:
                continue
        out.sort(key=lambda d: str(d.get("when") or ""))
        return out[-n:]
    except Exception:
        return []


def backfill_l0_from_jsonl(force: bool = False) -> Dict[str, int]:
    """一次性回填：把既有会话 jsonl 灌进 l0_messages（幂等，可重复跑）。

    ``record_id`` 由 (session, ts, role, text) 派生，重复行自动忽略；跑完记 meta 标记，
    下次调用（``force=False``）直接跳过。返回 ``{"rows": 新增数, "sessions": 会话数,
    "skipped": 已回填过的跳过标记}``。
    """
    if not enabled():
        return {"rows": 0, "sessions": 0, "skipped": 0}
    if not force:
        try:
            row = _l0_conn().execute("SELECT v FROM meta WHERE k='l0_backfilled'").fetchone()
            if row and str(row[0]) == "1":
                return {"rows": 0, "sessions": 0, "skipped": 1}
        except Exception:
            pass
    sessions = 0
    try:
        from omni_core.local import runtime_paths as _P

        root = _P.projects_root()
        # 会话 jsonl 的落点与 ProjectStore.list_sessions 同一口径：projects/<pid>/*.jsonl
        sdirs = [d for d in root.glob("*/*.jsonl")] if root.is_dir() else []
    except Exception:
        return {"rows": 0, "sessions": 0, "skipped": 0}

    def _count() -> int:
        try:
            return int(_l0_conn().execute("SELECT COUNT(*) FROM l0_messages").fetchone()[0])
        except Exception:
            return 0

    n_before = _count()
    for path in sdirs:
        try:
            sid = path.stem
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                content = str(rec.get("content") or "").strip()
                ex = rec.get("extra")
                if not content or (isinstance(ex, dict) and ex.get("partial")):
                    continue   # 半截增量过程记录不进索引（同一段回答会重复命中）
                index_l0_message(sid, str(rec.get("role") or "user"), content,
                                 recorded_at=str(rec.get("ts") or ""))
            sessions += 1
        except Exception:
            continue
    added = max(0, _count() - n_before)
    try:
        conn = _l0_conn()
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('l0_backfilled','1')")
        conn.commit()
    except Exception:
        pass
    logger.info(f"backfill_l0: 会话 {sessions} 个，入表 {added} 行")
    return {"rows": added, "sessions": sessions, "skipped": 0}


_l0_backfill_started = threading.Lock()
_l0_backfill_state: Dict[str, bool] = {"running": False}


def start_l0_backfill() -> bool:
    """后台发起一次性 L0 回填（服务启动调用；已在跑/已回填过则直接返回 False）。

    MB 量级扫描不能压在启动路径上（首请求延迟），也不能压在消息路径上；
    meta 里的 ``l0_backfilled`` 让它天然只跑一次。异常静默——索引缺失只是
    conversation_search 查不到旧记录，不影响对话本身。
    """
    if not enabled():
        return False
    with _l0_backfill_started:
        if _l0_backfill_state["running"]:
            return False
        _l0_backfill_state["running"] = True

    def _go():
        try:
            backfill_l0_from_jsonl()
        except Exception:
            pass
        finally:
            _l0_backfill_state["running"] = False

    threading.Thread(target=_go, name="l0-backfill", daemon=True).start()
    return True


_EXTRACT_SYSTEM = (
    "你是记忆提炼器。从对话片段提取关于环境规律、用户长期偏好、工具/系统行为的陈述句事实。"
    "规则:1) 1-5 条,每条一句陈述 ≤60 字;2) 只写会跨会话仍然成立的内容,"
    "一次性的操作过程/闲聊/纯主观感受不写;3) 观察不到规律就交白卷(facts 为空数组)"
    "——交白卷是正确答案,凑数写出的假规律是错误答案。"
    "\n类别纪律:只记「发生了什么、观察到什么事实」;禁止产出对系统内部机制的因果推断"
    "(「系统为什么这样/被拒的原因/策略如何」),禁止记录 assistant 自己的拒绝行为与应对过程"
    "(如「拒绝生成某内容/按某规则处理了某请求」——拒绝规则属场景块/角色卡层,不是记忆事实);"
    "对用户行为的描述只写客观事实,不带评判标签。"
    '只返回 JSON:{"facts": [{"text": "..."}]}'
)


def _extract_detail(digest: str, brain_cfg) -> Tuple[List[Dict[str, Any]], str]:
    """提炼 → ([{text}], 原因说明)；空列表时原因写明是白卷还是失败。"""
    try:
        # timeout=60s：reasoning 模型提炼思考常超 30s，默认 10s 会随机超时→假白卷
        data = llm_judge.chat_json(brain_cfg, _EXTRACT_SYSTEM, digest, timeout=60.0)
        if data is None:
            return [], "提炼失败(无模型/请求失败/解析失败)"
        out: List[Dict[str, Any]] = []
        for f in (data.get("facts") or []):
            text = str((f.get("text") if isinstance(f, dict) else f) or "").strip()
            if text:
                out.append({"text": text[:200]})
        if not out:
            return [], "白卷(模型判无可沉淀的跨会话事实)"
        return out, ""
    except Exception:
        return [], "提炼失败(异常)"


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


def open_brain() -> sqlite3.Connection:
    """打开/初始化 brain.db（schema v2，分词器随全局配置；重建自动回填）。"""
    os.makedirs(os.path.dirname(_BRAIN_PATH), exist_ok=True)
    conn = sqlite3.connect(_BRAIN_PATH, check_same_thread=False)
    conn.executescript("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
    tok = _fts_tokenizer()
    if tok == "simple":
        _ensure_simple_ext(conn)
    conn.executescript(_SCHEMA_V2.format(tokenizer=tok))
    conn.executescript(_L0_SCHEMA.format(tokenizer=tok))
    _ensure_owner_column(conn)
    _ensure_peer_column(conn)
    _l0_fts_ok(conn)
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


def _ensure_peer_column(conn: sqlite3.Connection) -> None:
    """peer 归属列（plan persona-social-memory M3）：这条记忆「关于谁」——「名片(uid)」。

    空 = 无特定对象（全局事实，读链不加权）。蒸馏写链从 L0 消息自带的「名片(uid): 」
    标注解析（judge 输出）；解析失败保守置空（= 退化为现行为）。老库 ALTER 补列。
    """
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(atoms)").fetchall()}
        if "peer" not in cols:
            conn.execute("ALTER TABLE atoms ADD COLUMN peer TEXT NOT NULL DEFAULT ''")
            conn.commit()
    except Exception:
        pass


_PEER_RE = None  # 惰性编译（见 extract_peer）


def extract_peer(text: str) -> str:
    """从文本里解析最近的「名片(uid)」来源标注（M1 格式）→ peer 键。

    读链召回的 peer 线索来源：检索 query 里出现谁，关于 TA 的记忆加权。
    解析不到返回空串（= 不加权，行为同无 peer 维度）。取**最后一个**命中——
    消息结尾处的对象最可能是当前交流对象。
    """
    global _PEER_RE
    if _PEER_RE is None:
        import re
        _PEER_RE = re.compile(r"[「]?([^「」():\n]{1,32})[」]?\((\d{4,})\)")
    last = ""
    for m in _PEER_RE.finditer(text or ""):
        last = f"{m.group(1)}({m.group(2)})"
    return last


def _ensure_owner_column(conn: sqlite3.Connection) -> None:
    """owner 三分档列（plan §5.2-4）：``user`` / ``persona`` / ``world``。

    本轮**只加列与默认值**，判定逻辑后置——先让写链有地方落归属，等多人格/
    worker 隔离真需要时再补判据，避免现在猜一套规则、以后又推翻重来。
    老库（schema v2 早期）没有该列 → ALTER 补上，默认 ``world``（环境与任务事实）。
    """
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(atoms)").fetchall()}
        if "owner" not in cols:
            conn.execute("ALTER TABLE atoms ADD COLUMN owner TEXT NOT NULL DEFAULT 'world'")
            conn.commit()
    except Exception:
        pass


# ---- 写链：flush → 两段判 → 落库 ----

_VALUE_SYSTEM = (
    "你是记忆价值评审器。给你一段对话摘要和从中初提的事实候选列表。"
    "对每条候选独立判定:"
    "keep:一次性操作过程/寒暄问候/纯主观感受/对系统内部机制的因果推断"
    "(「系统为什么这样/被拒的原因/策略如何」) → keep=false;"
    "值得跨会话保留的事实/偏好/规律/事件 → keep=true 并给 kind 与 importance。"
    "kind:fact(客观事实)|preference(用户长期偏好)|rule(规律/纪律/流程)|episode(具体事件片段)。"
    "importance:0~1(影响世界/跨会话成立/用户强偏好→高;临时性/局部细节→低)。"
    "peer:这条事实关于谁——对话摘要里带「名片(uid): 」标注的发言人,提取该事实所指对象的"
    "「名片(uid)」原文(如「成员甲(999)」);assistant 自己的话不打 peer;"
    "泛指多人/关于用户本人整体/无明确对象 → 留空 \"\"。"
    '只返回 JSON:{"items": [{"idx": 序号, "keep": true, "kind": "fact", "importance": 0.5, "peer": ""}]}'
)

_CONSIST_SYSTEM = (
    "你是记忆一致性评审器。给你「新事实」列表和「已有记忆」候选(带编号)。"
    "对每条新事实判定:"
    "与某条候选表达同一事实 → duplicate(返回 target_id,旧条将强化而非新增);"
    "与某条互补可合并为更完整一条 → merge(返回 target_id 与 merged_content,≤100 字,不丢关键信息);"
    "与某条矛盾且新信息更可信 → conflict(返回 target_id,旧条将作废、新条入库);"
    "是新信息 → novel。拿不准 → novel。"
    '只返回 JSON:{"decisions": [{"idx": 序号, "action": "novel|duplicate|merge|conflict",'
    ' "target_id": "...", "merged_content": "..."}]}'
)


def flush_brain(project_id: str, brain_cfg: Optional[Dict[str, Any]] = None,
                source_task: str = "", persona: str = "",
                msgs: Optional[List[Dict[str, str]]] = None,
                allow_write: bool = True,
                allow_atoms: bool = True) -> Dict[str, int]:
    """任务收尾：buffer 摘要 → 提炼 → 价值/kind/importance → 一致性四态 → brain.db。

    Args:
        msgs: 已从 capture 缓冲取走的消息（异步写链在入队时取走，worker 后续消费）。
            ``None`` = 就地 pop（同步路径 / 测试）。
        allow_write: 本轮是否"真的干了活"（did_work），透传给 L2 维护。
        allow_atoms: 是否允许产 L1 原子。**社交流水（群聊等）传 False**：他人可控文本
            不进长期事实库（防闲聊淹没、玩笑被当事实、以及提示注入沉淀成记忆投毒），
            只走叙事层沉淀（场景块 + 巩固 → 日记）。顺带省掉 3 次 LLM。
    """
    persona = persona or str(_cfg("persona") or "")
    stats: Dict[str, int] = {"extracted": 0, "kept": 0, "stored": 0, "duplicated": 0,
                             "merged": 0, "conflict": 0, "dropped": 0}
    msgs = pop_capture(project_id) if msgs is None else msgs
    if not enabled() or not brain_cfg or len(msgs) < _FLUSH_MIN_ROLES:
        return stats
    digest = "\n".join(f"[{m['role']}] {m['content']}" for m in msgs)[-_FLUSH_MAX_CHARS:]
    if not allow_atoms:
        # 只沉淀叙事层：场景块维护 + 巩固调度（日记从这里来），L1 一字不写。
        stats["l1_skipped"] = 1
        _flush_tail(project_id, brain_cfg, digest, source_task, stats, persona, allow_write)
        return stats
    try:
        atoms, _note = _extract_detail(digest, brain_cfg)
    except Exception:
        atoms, _note = [], "extract error"
    atoms = atoms[:_MAX_ATOMS_PER_FLUSH]
    stats["extracted"] = len(atoms)
    if not atoms:
        # 白卷也是"收尾了一次"：巩固的 flush 计数与触发不能只挂在非空提炼上，
        # 否则语音/闲聊为主的会话永远攒不到巩固条件
        _flush_tail(project_id, brain_cfg, digest, source_task, stats, persona, allow_write)
        return stats

    # 第一段：价值 / kind / importance（一次批量 LLM）
    cand_lines = "\n".join(f"[{i}] {a.get('text', '')}" for i, a in enumerate(atoms))
    items: List[Dict[str, Any]] = []
    try:
        data = llm_judge.chat_json(brain_cfg, _VALUE_SYSTEM,
                                   f"对话摘要:\n{digest[:2000]}\n\n事实候选:\n{cand_lines}",
                                   timeout=60.0)
        items = list((data or {}).get("items") or [])
    except Exception:
        items = []
    if not items:
        # LLM 失败 → 宁保守保留（kind=fact, importance=0.5），不丢
        items = [{"idx": i, "keep": True, "kind": "fact", "importance": 0.5}
                 for i in range(len(atoms))]
    kept: List[Tuple[int, Dict[str, Any]]] = []
    for it in items:
        try:
            idx = int(it.get("idx"))
        except Exception:
            continue
        if not (0 <= idx < len(atoms)) or not it.get("keep", True):
            stats["dropped"] += 1
            continue
        kind = str(it.get("kind") or "fact")
        if kind not in ("fact", "preference", "rule", "episode"):
            kind = "fact"
        try:
            imp = max(0.0, min(1.0, float(it.get("importance", 0.5))))
        except Exception:
            imp = 0.5
        # peer 归属（关于谁，M3）：LLM 给了就收（截断防脏长），没给/给歪了保守置空
        peer = str(it.get("peer") or "").strip()[:80]
        kept.append((idx, {"text": str(atoms[idx].get("text", ""))[:200],
                           "kind": kind, "importance": imp, "peer": peer}))
    stats["kept"] = len(kept)
    if not kept:
        # 一条都没留下也要走收尾：L2 维护与巩固的触发不该被"这轮没沉淀事实"卡住
        # （闲聊/全部被判为一次性 也是"收尾了一次"，同白卷分支口径）。
        _flush_tail(project_id, brain_cfg, digest, source_task, stats, persona, allow_write)
        return stats

    # 第二段：一致性四态（候选 = 各条 top-3 并集，一次批量 LLM）
    conn = open_brain()
    cand_pool: Dict[str, str] = {}
    for _, it in kept:
        for h in search_brain(it["text"], persona=persona, k=3, reinforce=False, conn=conn):
            cand_pool[str(h["record_id"])] = h["content"]
    decisions: Dict[int, Dict[str, Any]] = {}
    if cand_pool:
        cand_lines = "\n".join(f"[{rid}] {txt}" for rid, txt in cand_pool.items())
        new_lines = "\n".join(f"[{i}] {it['text']}" for i, it in kept)
        try:
            data = llm_judge.chat_json(brain_cfg, _CONSIST_SYSTEM,
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
    applied = 0
    for i, it in kept:
        d = decisions.get(i) or {"action": "novel"}
        action = str(d.get("action") or "novel")
        rid = _new_id()
        if action in ("duplicate", "merge", "conflict"):
            target = conn.execute("SELECT record_id FROM atoms WHERE record_id=?",
                                  (str(d.get("target_id") or ""),)).fetchone()
            if target is None:
                action = "novel"
        if action == "duplicate":
            conn.execute("UPDATE atoms SET access_count=access_count+1, last_access=?, "
                         "updated=?, importance=max(importance, ?) WHERE record_id=?",
                         (now, now, it["importance"], str(d.get("target_id"))))
            stats["duplicated"] += 1
        elif action == "merge":
            merged = str(d.get("merged_content") or it["text"])[:200]
            conn.execute("UPDATE atoms SET content=?, updated=?, importance=max(importance, ?), "
                         "access_count=access_count+1, last_access=? WHERE record_id=?",
                         (merged, now, it["importance"], now, str(d.get("target_id"))))
            conn.execute("INSERT OR REPLACE INTO atoms_fts(record_id, content) VALUES(?,?)",
                         (str(d.get("target_id")), merged))
            stats["merged"] += 1
        elif action == "conflict":
            conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? "
                         "WHERE record_id=?", (rid, now, str(d.get("target_id"))))
            _insert_atom(conn, rid, it, persona, project_id, source_task, now)
            stats["conflict"] += 1
        else:
            _insert_atom(conn, rid, it, persona, project_id, source_task, now)
            stats["stored"] += 1
        applied += 1
    conn.commit()
    conn.close()
    stats["applied"] = applied

    _flush_tail(project_id, brain_cfg, digest, source_task, stats, persona, allow_write)
    return stats


def _flush_tail(project_id: str, brain_cfg: Optional[Dict[str, Any]], digest: str,
                source_task: str, stats: Dict[str, int], persona: str,
                allow_write: bool) -> None:
    """收尾公共尾段：L2 维护（受 did_work 门控）+ 巩固调度。

    三条收尾路径（白卷 / 全被判为一次性 / 正常落库）都走这里——把"该做的事"收成
    一处，避免某条分支忘了触发巩固（历史上白卷分支就漏过，语音会话因此攒不到巩固）。
    """
    try:
        from omni_core.local import scene_executor
        scene_executor.maybe_maintain(project_id, brain_cfg, digest,
                                      task_id=source_task, stats=stats,
                                      allow_write=allow_write)
    except Exception:
        pass
    maybe_consolidate(brain_cfg, persona)


def _new_id() -> str:
    import uuid
    return f"m_{uuid.uuid4().hex[:12]}"


def _insert_atom(conn: sqlite3.Connection, rid: str, it: Dict[str, Any],
                 persona: str, origin: str, source_task: str, now: str,
                 owner: str = "world") -> None:
    conn.execute("INSERT OR REPLACE INTO atoms(record_id, content, kind, persona, origin, "
                 "source_task, importance, salience, access_count, last_access, state, "
                 "version, created, updated, owner, peer) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, it["text"], it.get("kind", "fact"), persona or "", origin or "",
                  source_task or "", float(it.get("importance", 0.5)),
                  float(it.get("importance", 0.5)), 0, "", "active", 1, now, now,
                  owner or "world", str(it.get("peer") or "")))
    conn.execute("INSERT OR REPLACE INTO atoms_fts(record_id, content) VALUES(?,?)",
                 (rid, it["text"]))


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


def search_brain(query: str, persona: str = "", k: Optional[int] = None,
                 reinforce: bool = True,
                 conn: Optional[sqlite3.Connection] = None,
                 peer: str = "") -> List[Dict[str, Any]]:
    """融合排序检索：relevance(bm25) × importance × recency，active-only，floor 过滤。

    salience 不再衰减（P7，2026-11-02，对齐原仓库 priority 语义）：排序中直接取
    importance 字段值；dormant/archived 状态机已删除，state 只剩 active/tombstone。
    peer（可选，M3）：当前对话对象「名片(uid)」——关于 TA 的记忆终分加权（×1.25，
    **不是过滤**：全局事实照常召回）；不传 = 行为与无 peer 维度时一致。
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
        states = "('active')"
        pfilter = "AND (a.persona='' OR a.persona=?)" if persona else ""
        rows: List[Any] = []
        if match:
            params: List[Any] = [match]
            if persona:
                params.append(persona)
            params.append(max(k * 3, 15))
            rows = c.execute(
                f"SELECT f.record_id, f.content, bm25(atoms_fts) AS r, a.kind, a.importance, "
                f"a.access_count, a.updated, a.last_access, a.peer FROM atoms_fts f "
                f"JOIN atoms a ON a.record_id = f.record_id "
                f"WHERE atoms_fts MATCH ? AND a.state IN {states} {pfilter} "
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
        for rid, content, rank, kind, imp, acc, upd, la, pr in rows:
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
            if peer and str(pr or "") == peer:
                final = min(1.0, final * 1.25)   # 关于当前对象：加权不过滤（M3）
            if str(rid) in _recent_ring:
                final *= 0.85                    # 新鲜度轮换：只降权不排除
            if final < floor:
                continue
            hits.append({"record_id": rid, "content": content, "score": round(final, 6),
                         "kind": kind, "salience": round(sal, 4), "peer": str(pr or "")})
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
        # LIKE 兜底（2 字词在 trigram 降级模式下）；persona 过滤与主路一致，
        # 否则兜底会绕过画像隔离把别人的记忆捞进来
        if like_terms and len(hits) < k:
            lsql = ("SELECT record_id, content FROM atoms WHERE content LIKE ? "
                    "AND state IN " + states)
            if persona:
                lsql += " AND (persona='' OR persona=?)"
            lsql += " LIMIT ?"
            for kw in like_terms:
                lp: List[Any] = [f"%{kw}%"]
                if persona:
                    lp.append(persona)
                lp.append(k)
                for rid, content in c.execute(lsql, tuple(lp)).fetchall():
                    if all(str(rid) != str(h["record_id"]) for h in hits):
                        hits.append({"record_id": rid, "content": content, "score": 0.0,
                                     "kind": "fact", "salience": 0.0})
        return hits[:k]
    except Exception:
        return []
    finally:
        if owned:
            try:
                c.close()
            except Exception:
                pass


def inject_text_brain(persona: str = "", query: str = "",
                      peer: str = "") -> Tuple[str, Dict[str, Any]]:
    """L1 检索文本块（供工具/调试；<relevant-memories> 形态）；零命中也返回 meta（可观测）。

    peer（可选，M3）：不显式传时从 query 里解析最近的「名片(uid)」标注（M1 约定）；
    关于 TA 的记忆加权，条目带「关于 X：」前缀。
    """
    hits: List[Dict[str, Any]] = []
    try:
        hits = search_brain(query, persona=persona,
                            reinforce=True, peer=peer or extract_peer(query))
    except Exception:
        hits = []
    meta = {"hits": len(hits), "chars": sum(len(h["content"]) for h in hits),
            "placement": "tool", "store": "brain",
            "peer": peer or extract_peer(query)}
    if not hits:
        return "", meta
    lines = "\n".join(
        f"- 关于 {h['peer']}：{h['content']}" if h.get("peer") else f"- {h['content']}"
        for h in hits)
    text = ("<relevant-memories>\n以下为检索到的历史记忆,仅作参考,不代表当前任务进程:\n"
            + lines + "\n</relevant-memories>")
    return text, meta


def log_recall_observability(project: str = "", source: str = "", calls: int = 0,
                             hits: int = 0, top1_id: str = "") -> None:
    """P1 观测（2026-11-02）：每 run 一行 ``memory_recall.jsonl``。

    ``{ts, project, source, calls, hits, top1_id, zero_query}``——「该查而没查」的
    run（zero_query=true）可与轨迹对照人工评；任何异常静默（观测不打断主流程）。
    """
    if not enabled():
        return
    try:
        rec = {"ts": _now(), "project": str(project or ""), "source": str(source or ""),
               "calls": int(calls), "hits": int(hits), "top1_id": str(top1_id or ""),
               "zero_query": int(calls) == 0}
        path = os.path.join(os.path.dirname(_BRAIN_PATH), "memory_recall.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


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


def maybe_consolidate(brain_cfg: Optional[Dict[str, Any]] = None, persona: str = "") -> None:
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
                         args=(brain_cfg or {}, persona), daemon=True)
    t.start()


def _run_consolidation(brain_cfg: Dict[str, Any], persona: str) -> None:
    try:
        _consolidation_worker(brain_cfg, persona)
    finally:
        with _dispatch_lock:
            _dispatch_state["running"] = False


def _consolidation_worker(brain_cfg: Dict[str, Any], persona: str) -> None:
    with _consolidation_lock:
        stats: Dict[str, Any] = {"ts": _now()}
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
            stats.update(_consolidate_impl(brain_cfg, persona))
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


def _consolidate_impl(brain_cfg: Dict[str, Any], persona: str) -> Dict[str, Any]:
    conn = open_brain()
    out: Dict[str, Any] = {"dedup_checked": 0, "merged": 0, "abstracted": 0,
                           "conflict": 0}
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
            data = llm_judge.chat_json(brain_cfg, _CONSIST_SYSTEM,
                                       f"新事实:\n[{rid}] {content}\n\n已有记忆候选:\n{cand_lines}",
                                       timeout=60.0)
        except Exception:
            continue
        for d in (data or {}).get("decisions") or []:
            act = str(d.get("action") or "")
            tgt = str(d.get("target_id") or "")
            if act == "merge" and tgt and d.get("merged_content"):
                merged = str(d["merged_content"])[:200]
                conn.execute("UPDATE atoms SET content=?, updated=? WHERE record_id=? AND record_id!=?",
                             (merged, now, tgt, rid))
                conn.execute("INSERT OR REPLACE INTO atoms_fts(record_id, content) VALUES(?,?)", (tgt, merged))
                conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? WHERE record_id=?",
                             (tgt, now, rid))
                out["merged"] += 1
            elif act == "conflict" and tgt:
                # 以信息更全的新条为准：目标 tombstone 指向新?此处新=旧条目自身，语义取「目标作废」
                conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? "
                             "WHERE record_id=? AND record_id!=?", (rid, now, tgt, rid))
                out["conflict"] += 1

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
                     persona, "", "consolidation", now)
        for erid, _ in grp:
            # P7：不再 salience*0.5 衰减（salience=importance 是写链定的静态值）
            conn.execute("UPDATE atoms SET kind='episode', updated=? "
                         "WHERE record_id=?", (now, erid))
        abstracted += 1
    out["abstracted"] = abstracted

    conn.commit()
    conn.close()

    # 7) 画像重写（L3）：brain.db 新 atoms → user_profile.md（带外信号强制）
    if maintain_profile(brain_cfg, persona):
        out["profile_rewritten"] = 1

    # 8) 日记步（plan persona-social-memory M2）：她的一天 → L2 diary 块（失败静默）
    try:
        if maintain_diary(brain_cfg, persona):
            out["diary_written"] = 1
        if maintain_month_diary(brain_cfg, persona):
            out["month_diary_written"] = 1
        # 9) 关系卡步：按**人**把近期往来压成印象卡（全局一份，跨群/跨项目）
        rel = maintain_relations(brain_cfg, persona)
        if rel:
            out["relations_written"] = rel
    except Exception:
        pass
    return out


# ---- agent 自管（工具层入口；落盘权在内核） ----

_DIARY_SYSTEM = (
    "你是助手本人，以第一人称写「这一天」的日记。"
    "输入：这一天的对话记录与完成的任务。记录格式：[user] 行是别人对你说的话，"
    "行首「名片(uid): 」是该行说话人；[assistant] 行是你自己说过的话；"
    "行内出现的「[引用] X: …」是被引用消息的原话——X 不是这行的说话人，"
    "若引用的是你说过的话，那是对方在接你的话茬。"
    "规则：1) 只记有意义的经历——他人对你的互动（谁做了什么、你怎么应对）、与主人的交流、"
    "值得记住的任务经历；纯寒暄、纯流水账不写；若当天与多个对象/场景都有交流，"
    "各安排笔墨（详略自定），不要漏掉一整个对话对象；2) 任务经历一句话带过，不写步骤细节；"
    "3) 忠于事实，不虚构、不加评价性编造；4) 知识/规律不写（那些走记忆库，日记只收经历）；"
    "5) 聊天对象=出现过的 [user] 行说话人——你自己不算（你的名字/群名片哪怕出现在"
    "引用行里也不是聊天对象，别把自己写进「跟谁聊了」）；"
    "6) 这一天没什么值得记 → diary 返回空串，交白卷是正确答案。"
    # 日记归 L0-叙事（不进注入面）后不再受 450 字注入预算约束（plan memory-recall P6）。
    '只返回 JSON:{"diary": "第一人称日记，≤900 字；没有则空串"}'
)

#: 叙事主体的已知名字（本设备运行时登记，非配置非写死）：各渠道在拿到自己账号
#: 的昵称/群名片时调 ``note_self_names``——换账号/换渠道随连接自愈，多渠道并集
#: 成立（同一个人格的所有表面身份）。只收高置信身份（精确昵称/名片），泛别名
#: （如群里的撞名泛称）由渠道自行把握，登记错误的兜底是日记措辞、不是记忆结构。
_SELF_NAMES: set = set()


def note_self_names(names) -> None:
    """渠道登记叙事主体（她自己）在本渠道的已知名字/uid（幂集，重复调用无害）。"""
    for n in (names or ()):
        s = str(n or "").strip()
        if s:
            _SELF_NAMES.add(s)


def self_names() -> List[str]:
    return sorted(_SELF_NAMES)

_DIARY_MONTH_SYSTEM = (
    "你是助手本人，把「某个月」的多篇第一人称日记压缩成一篇月记。"
    "输入：该月各天的日记正文。"
    "规则：1) 保留关键人物与事件起伏（谁对你做了什么、关系怎么变化），舍弃琐事；"
    "2) 第一人称，≤250 字；3) 忠于日记内容，不虚构。"
    '只返回 JSON:{"summary": "一行式本月关键人物/事件地图，≤60 字", "month_diary": "月记正文"}'
)


#: 日记素材组装（v3，2026-11-01）：全局尾窗 + 每会话保底——机械层只保证每个会话
#: 都「被看见」（不因程序漏），写不写仍由 prompt 的意义判决定（纯寒暄不写）。
_DIARY_TAIL_LINES = 160
_DIARY_FLOOR_LINES = 10
_DIARY_MAX_LINES = 400


def maintain_diary(brain_cfg: Optional[Dict[str, Any]], persona: str = "",
                   day: str = "") -> int:
    """日记步（plan persona-social-memory M2）：巩固期把「她的一天」写成第一人称日记。

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
    """
    if not enabled() or not brain_cfg:
        return 0
    try:
        import config
        if not bool(config.get_config("knowledge.diary.enabled", True)):
            return 0
    except Exception:
        pass
    from omni_core.local import scene_executor

    day = (day or "").strip() or (datetime.now(timezone.utc)
                                  - timedelta(days=1)).strftime("%Y-%m-%d")
    written = 0
    # 该日会话记录按项目分组：**跨会话文件合并**（一个项目可有多个 peer 会话文件，
    # 逐文件覆盖赋值会把同项目其它会话的素材整段丢掉——2026-11-01 实测踩过）；
    # 尾部优先扫描；同时记每文件尾部若干行（会话保底用）。
    by_project: Dict[str, List[Tuple[str, str]]] = {}
    file_tails: Dict[str, Dict[str, List[Tuple[str, str]]]] = {}
    try:
        for f in rp.projects_root().glob("*/s_*.jsonl"):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            recs: List[Tuple[str, str]] = []
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = str(rec.get("ts") or "")
                if not ts.startswith(day):
                    if recs:
                        break       # 已收集到该日的、遇到更早的即停（新记录在尾部）
                    continue
                role = str(rec.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                content = str(rec.get("content") or "").strip()[:300]
                if content:
                    recs.append((ts, f"[{role}] {content}"))
            if recs:
                by_project.setdefault(f.parent.name, []).extend(recs)
                file_tails.setdefault(f.parent.name, {})[f.name] = \
                    recs[-_DIARY_FLOOR_LINES:]
    except Exception:
        pass
    if not by_project:
        return 0
    # 该日完成任务（标题级一句话；任务明细走轨迹，日记只收经历）
    try:
        from omni_core.local.task_store import TaskStore

        done_day = [t for t in TaskStore.list()
                    if str(t.get("finished_at") or "").startswith(day)]
    except Exception:
        done_day = []
    for pid, items in by_project.items():
        try:
            from pathlib import Path as _P

            if (scene_executor.scene_blocks_dir(pid) / f"diary-{day}.md").exists():
                continue                        # 一日一篇，幂等
            # 素材 v3：全局尾窗 ∪ 每会话保底（保证被看见），按 ts 排序、硬顶封 token
            chosen = set(sorted(items, key=lambda x: x[0])[-_DIARY_TAIL_LINES:])
            for tail in file_tails.get(pid, {}).values():
                chosen.update(tail)
            recs = [line for _, line in sorted(chosen, key=lambda x: x[0])]
            recs = recs[-_DIARY_MAX_LINES:]
            material = "\n".join(recs)
            task_lines = "\n".join(
                f"- 完成任务：{str(t.get('title') or t.get('objective') or '')[:60]}"
                for t in done_day if str(t.get("project_id") or "") == pid)
            if task_lines:
                material += "\n\n[当日完成任务]\n" + task_lines
            if len(material.strip()) < 80:
                continue                        # 素材太薄，白卷
            system = _DIARY_SYSTEM
            known = self_names()
            if known:
                system += (
                    "\n身份锚：以下是「你」在本设备的已知名字（任一出现都指你自己，"
                    f"不是聊天对象）：{'、'.join(known)}。")
            data = llm_judge.chat_json(brain_cfg, system, material, timeout=90.0)
            diary = str((data or {}).get("diary") or "").strip()
            if not diary:
                continue
            if scene_executor.write_diary_block(pid, day, diary):
                written += 1
        except Exception:
            continue
    return written


_REL_SYSTEM = (
    "你是助手本人。下面这些「他说过的话」来自你与某个人的往来（可能跨群、也可能私聊）。"
    "写你对这个人的印象：第一人称、口语、三行以内；只写材料里能看出来的东西，"
    "不编造、不给 TA 的人格下判决。\n"
    "注意：群里可能有与你同名的其他人（同名 agent），材料里出现的名字不一定指你；"
    "只根据说话内容判断，别把称呼当成你自己。\n"
    "哪怕只有几轮往来也尽量写出能看出的东西；确实看不出才整卡写「还不熟」。\n"
    "格式：\n最近交手：<这段时间发生过什么，一两句>\n"
    "他对我的态度：<试探/起哄/客气/熟络/冷淡…>\n我对他的态度：<警惕/冷淡/愿意接话/松快…>\n"
    "只返回 JSON:{\"card\": \"...\"}"
)

#: 关系卡步的边界：每人至少这么多条往来才写卡；每轮最多更新几人；每个会话文件只扫尾部
_REL_MIN_LINES = 3
_REL_MAX_PEERS = 5
_REL_TAIL_LINES = 600


def _peer_lines(limit_days: int = 7) -> Dict[str, Dict[str, Any]]:
    """按 uid 汇总「某人说过的话」——**跨项目、跨会话合并**（人是跨场景的）。

    只认带来源标注的 user 行（``名片(uid): 文本``，通道层写入）；同一 uid 在不同群/
    私聊的记录会并到一起，这正是「一个助手在回忆同一个人」。
    """
    import re

    pat = re.compile(r"「?([^」():：]{1,24})」?\((\d{4,12})\)[:：]\s*(.+)")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, limit_days))
              ).strftime("%Y-%m-%d")
    out: Dict[str, Dict[str, Any]] = {}
    for f in rp.projects_root().glob("*/s_*.jsonl"):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()[-_REL_TAIL_LINES:]
        except Exception:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = str(rec.get("ts") or "")
            if ts and ts[:10] < cutoff:
                continue
            if str(rec.get("role")) != "user":
                continue
            m = pat.search(str(rec.get("content") or ""))
            if not m:
                continue
            name, uid, text = m.group(1).strip(), m.group(2), m.group(3).strip()
            item = out.setdefault(uid, {"name": name, "lines": []})
            if not item.get("name"):
                item["name"] = name
            if text:
                item["lines"].append(text[:160])
    return out


def maintain_relations(brain_cfg: Optional[Dict[str, Any]], persona: str = "",
                       limit_days: int = 7) -> int:
    """关系卡步：按**人**把这段时间的往来压成一张印象卡（全局一份，不按群/项目拆）。

    落点 ``~/.omniagent/memory/relations/<uid>.md``（META: name/uid/updated + 正文三行）。
    只写"记录里能看出来"的东西，做得越久越像关系而不是账本；信息不足写「还不熟」。
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
        try:
            material = "\n".join(f"- {t}" for t in info["lines"][-40:])
            # 注意：模板里含 JSON 花括号，不能用 str.format（会把 {"card": ...} 当占位符）
            data = llm_judge.chat_json(
                brain_cfg, _REL_SYSTEM.replace("{name}", str(info.get("name") or uid)),
                f"对方说过的话（按时间，最多 40 条）：\n{material}", timeout=60.0)
            card = str((data or {}).get("card") or "").strip()
            if not card:
                continue
            body = ("-----META-START-----\n"
                    f"name: {info.get('name') or uid}\nuid: {uid}\nupdated: {_now()}\n"
                    "-----META-END-----\n" + card[:600])
            (d / f"{uid}.md").write_text(body, encoding="utf-8")
            written += 1
        except Exception:
            continue
    return written


def maintain_month_diary(brain_cfg: Optional[Dict[str, Any]], persona: str = "") -> int:
    """月记步（plan persona-social-memory M2.5）：把**已结束月份**的日记压缩为月记块。

    月记 ``diary-month-<月份>.md`` 是消费地图（META.summary = 关键人物/事件一行式）——
    她据此知道该翻哪几篇旧日记。规则：只压已结束月份；<3 篇不压；已有月记幂等跳过；
    失败静默。门控同日记步。
    """
    if not enabled() or not brain_cfg:
        return 0
    try:
        import config
        if not bool(config.get_config("knowledge.diary.enabled", True)):
            return 0
    except Exception:
        pass
    from omni_core.local import scene_executor

    current_month = _now()[:7]
    written = 0
    by_key: Dict[Tuple[str, str], List[str]] = {}
    try:
        for d in rp.projects_root().glob("*/memory/scene_blocks/diary-2*.md"):
            name = d.name
            if name.startswith("diary-month-"):
                continue
            ym = name[len("diary-"):len("diary-") + 7]   # diary-YYYY-MM-DD.md → YYYY-MM
            if len(ym) != 7 or ym >= current_month:
                continue                                  # 未结束月份不压
            pid = d.parent.parent.parent.name             # scene_blocks 上三级 = 项目
            by_key.setdefault((pid, ym), []).append(name)
    except Exception:
        pass
    for (pid, ym), files in sorted(by_key.items()):
        try:
            if len(files) < 3:
                continue                                  # 单薄月份不成篇
            parts: List[str] = []
            for name in sorted(files):
                raw = scene_executor.read_scene_block(pid, name)
                _, body = scene_executor.parse_scene_block(raw)
                if body:
                    parts.append(body[:400])
            material = "\n\n".join(parts)
            if len(material) < 120:
                continue
            data = llm_judge.chat_json(brain_cfg, _DIARY_MONTH_SYSTEM, material, timeout=90.0)
            summary = str((data or {}).get("summary") or "").strip()[:80]
            body = str((data or {}).get("month_diary") or "").strip()
            if not body:
                continue
            content = f"{summary}\n\n{body}" if summary else body
            if scene_executor.write_diary_block(pid, f"month-{ym}", content):
                written += 1
        except Exception:
            continue
    return written


def remember(text: str, kind: str = "fact", importance: float = 0.8,
             persona: str = "", brain_cfg: Optional[Dict[str, Any]] = None,
             origin: str = "", source_task: str = "remember") -> str:
    """显式写入一条记忆（agent/用户主动）。返回 record_id；失败返回空串。"""
    text = (text or "").strip()[:200]
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
                     persona or str(_cfg("persona") or ""), origin, source_task, now)
        conn.commit()
        return rid
    except Exception:
        return ""
    finally:
        conn.close()


def update_memory(record_id: str, new_text: str, persona: str = "") -> str:
    """更正记忆：旧条 tombstone(superseded_by=新条)，新条入库。返回新 record_id 或空串。"""
    new_text = (new_text or "").strip()[:200]
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
                     persona or str(_cfg("persona") or ""), "", "update", now)
        conn.execute("UPDATE atoms SET state='tombstone', superseded_by=?, updated=? "
                     "WHERE record_id=?", (rid, now, record_id))
        conn.commit()
        return rid
    except Exception:
        return ""
    finally:
        conn.close()


def forget(record_id: str, reason: str = "", persona: str = "") -> bool:
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
            "salience, access_count, state, superseded_by, updated, owner FROM atoms "
            "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        keys = ("record_id", "content", "kind", "persona", "origin", "source_task",
                "importance", "salience", "access_count", "state", "superseded_by", "updated",
                "owner")
        return [dict(zip(keys, r)) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


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


# ---- L3 画像（brain.db 驱动；人工 PUT /profile 可覆盖） ----

_PROFILE_AUTO = "knowledge.profile.auto_maintain"        # 缺省开
_PROFILE_TRIGGER = "knowledge.profile.trigger_every_n"   # 每 N 条新 atoms 触发
_PROFILE_MAX_CHARS = 2000
_PROFILE_TS_KEY = "last_profile_ts"
_PROFILE_SIGNAL_KEY = "persona_update_request"

_PROFILE_SYSTEM = (
    "你是用户画像维护器。给你「当前用户画像」和「最近沉淀的新事实」。"
    "任务:输出更新后的完整用户画像——保留仍然成立的内容,把新事实中关于"
    "用户本人/用户长期偏好/用户环境的部分合并进去,剔除已过时或与用户无关的条目;"
    "矛盾的以新事实为准。不要虚构,没有可合并的新信息就原样返回。"
    "\n类别边界:画像只记录关于用户本人的客观事实/长期偏好/习惯/背景。"
    "以下内容一律视为「与用户无关」剔除,不得合并进画像:assistant 的应对策略、"
    "拒绝规则、边界设定,以及对用户行为的评判/模式判定条款(如「用户反复索取某类"
    "内容……此类内容不生成」)——这类内容属于场景块/角色卡层;即使新事实以"
    "「优先处理」或带外信号标记出现,也照此过滤。"
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


def maintain_profile(brain_cfg: Optional[Dict[str, Any]], persona: str = "") -> bool:
    """L3 画像重写：上次水位之后的新 atoms → LLM 增量合并 → user_profile.md（旧版 .bak）。

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
        pp = rp.user_profile()
        old_profile = pp.read_text(encoding="utf-8") if pp.exists() else ""
        new_facts = "\n".join(f"- {c}" for (c,) in rows)
        if signal:
            new_facts = f"[场景整理请求画像更新(优先处理)]\n{signal}\n\n{new_facts}"
        data = llm_judge.chat_json(
            brain_cfg, _PROFILE_SYSTEM,
            f"当前用户画像:\n{old_profile or '(空)'}\n\n最近新事实:\n{new_facts}",
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
