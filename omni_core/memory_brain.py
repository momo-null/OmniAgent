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
  chat_ids   TEXT DEFAULT '',
  peers      TEXT DEFAULT '',
  date_basis TEXT DEFAULT '',
  updated    TEXT DEFAULT '',
  PRIMARY KEY(doc_id, kind)
);
CREATE VIRTUAL TABLE IF NOT EXISTS narrative_fts USING fts5(
  search_text, doc_id UNINDEXED, kind UNINDEXED, tokenize='{tokenizer}');
CREATE INDEX IF NOT EXISTS idx_narr_kind_date ON narrative_index(kind, date_from);
"""

# 圈层语境链（plan §4.2 / §5.2，M3）：context_buffer 按 chat_id 累积通用信号（覆盖不
# 追加）；circle_note 一行一场合（**索引键，非物理分区**，INV-2）——语境预期（聊什么/
# 什么分寸/共同话题词面），不是可引用事实：永不进 atoms、不参与四态（INV-5）。
_CONTEXT_SCHEMA = """
CREATE TABLE IF NOT EXISTS context_buffer(
  chat_id   TEXT PRIMARY KEY,
  buffer    TEXT DEFAULT '',
  msg_count INTEGER DEFAULT 0,
  updated   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS circle_note(
  chat_id        TEXT PRIMARY KEY,
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


def capture(project_id: str, role: str, content: str, chat_id: str = "") -> None:
    """会话消息入 buffer(不提炼;提炼在任务收尾 flush_brain 时批量做)。

    ``chat_id``（§4.3）：这条消息来自哪个聊天窗口——只作 atoms 的**来源标注**
    （「在哪儿听来的」线索，供加权与共同背景召回），不参与过滤（INV-3）。
    """
    if not enabled() or not project_id:
        return
    with _buf_lock:
        buf = _buffers.setdefault(project_id, [])
        buf.append({"role": role, "content": content, "chat_id": str(chat_id or "")})
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
                     kind: str = "chat", recorded_at: str = "",
                     source_kind: str = "", provenance: str = "") -> str:
    """会话消息入 L0 检索表（写入即入表；任何异常静默——索引不打断主流程）。

    ``kind``：``chat``=通道消息，``task``=任务输入（cron/手动派发写进会话的那条 user 气泡）。
    ``source_kind``（§4.6 进料道，INV-13）：``chat`` / ``observation`` / ``task``——
    空则从 ``kind`` 推导（老调用语义不变）；观察原文必须经 ``capture_observation()``
    进入，不得伪装成聊天原话。``provenance`` 为结构化来源指针（不含真实标识）。
    返回 record_id；未启用/空文本/失败返回空串。
    """
    text = str(text or "").strip()
    if not text or not enabled() or not (session_id or chat_id):
        return ""
    kind = kind if kind in ("chat", "task") else "chat"
    if source_kind not in ("chat", "observation", "task"):
        source_kind = "task" if kind == "task" else "chat"
    role = role if role in ("user", "assistant", "system") else "user"
    now = recorded_at or _now()
    body = text[:_L0_TEXT_MAX]
    rid = _l0_id(str(session_id or ""), now, role, body)
    try:
        with _lock:
            conn = _l0_conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO l0_messages(record_id, chat_id, session_id, kind, "
                "role, speaker, text, recorded_at, source_kind, provenance) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rid, str(chat_id or ""), str(session_id or ""), kind, role,
                 _l0_speaker(body, role), body, now,
                 source_kind, str(provenance or "")[:200]))
            if cur.rowcount:
                conn.execute("INSERT OR IGNORE INTO l0_fts(record_id, text) VALUES(?,?)",
                             (rid, body))
                conn.commit()
        return rid
    except Exception:
        return ""


def capture_observation(context_key: str, text: str, provenance: str = "",
                        observed_at: str = "", source_kind: str = "observation") -> str:
    """统一观察进料道**唯一入口**（§4.6 / §5.5，R8）：所有非对话来源（主动阅读 /
    检索结果 / 工具产出）都走它写入 ``l0_messages(source_kind=observation)``。

    - 观察不直接写 atoms（INV-6）：升格只发生在巩固期价值判 / 四态，且写齐
      §4.3 四字段（INV-14）；观察原文不进 ``conversation_search``（INV-13，
      ``search_l0`` 只查 ``source_kind=chat``）。
    - ``context_key`` 是结构化场合键（``group:<id>`` / ``private:<id>`` / 任务键），
      内核不解析其语义（INV-4）；``provenance`` 是结构化来源指针，不含真实账号 /
      URL 明文标识。
    - 返回 record_id；未启用 / 空文本 / 失败返回空串（观察是增强项，静默）。
    """
    text = str(text or "").strip()
    if not text or not enabled() or not (context_key or "").strip():
        return ""
    if source_kind not in ("observation", "task"):
        source_kind = "observation"
    return index_l0_message("", "system", text, chat_id=str(context_key),
                            kind="chat", recorded_at=observed_at or "",
                            source_kind=source_kind, provenance=provenance)


def _l0_cutoff(days: Any) -> str:
    try:
        d = max(1, int(days))
    except Exception:
        d = 30
    return (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()


def search_l0(keyword: str, *, chat_id: str = "", session_id: str = "",
              days: Any = 30, limit: int = 8, source_kind: str = "chat") -> List[Dict[str, Any]]:
    """L0 逐字检索（FTS + 时间窗）→ ``[{when, who, role, text, chat_id, kind}]``，时间正序。

    范围（ACL 由调用方决定，本函数只认参数）：传 ``chat_id`` / ``session_id`` = 只查该聊天窗口
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
        sk = str(source_kind or "").strip().lower()
        sk_sql, sk_params = "", []
        if sk in ("chat", "observation", "task"):
            sk_sql = "AND l.source_kind = ?"
            sk_params = [sk]
        base = (f"SELECT l.record_id, l.chat_id, l.session_id, l.kind, l.role, l.speaker, "
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
                        "text": row[6], "chat_id": row[1], "kind": row[3]})

        for row in rows:
            _push(row)
        # LIKE 兜底：2 字词在 trigram 降级模式下 MATCH 无结果（同 atoms 的老坑）
        for kw in like_terms:
            lsql = ("SELECT l.record_id, l.chat_id, l.session_id, l.kind, l.role, l.speaker, "
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
                out.append({"text": text[:_MAX_ATOM_CHARS]})
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
    conn.executescript(_NARRATIVE_SCHEMA.format(tokenizer=tok))
    conn.executescript(_CONTEXT_SCHEMA)
    _ensure_peer_column(conn)
    _ensure_atom_source_columns(conn)
    _ensure_l0_source_columns(conn)
    _ensure_dead_dims_cleaned(conn)
    _l0_fts_ok(conn)
    _narr_fts_ok(conn)
    conn.commit()
    return conn


def _ensure_dead_dims_cleaned(conn: sqlite3.Connection) -> None:
    """P2 死维度一次性清理（plan §4.3 / §7.2-8；meta ``p2_dead_dims_cleaned`` 只跑一次）：

    - ``owner`` 列：多租户遗产（三分档全写同一值且从不读）→ **整列移除**；
    - ``persona``：生产链路恒空 → 冻结（存量值清空；列保留保 schema 稳定，写入恒空）。
    """
    try:
        row = conn.execute("SELECT v FROM meta WHERE k='p2_dead_dims_cleaned'").fetchone()
        if row and str(row[0]) == "1":
            return
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(atoms)").fetchall()}
        if "owner" in cols:
            conn.execute("ALTER TABLE atoms DROP COLUMN owner")
        conn.execute("UPDATE atoms SET persona='' WHERE persona != ''")
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('p2_dead_dims_cleaned','1')")
        conn.commit()
    except Exception:
        pass


#: §4.3 出处/时效五列（NP-0 结构，P5 才有采集器来喂）：全部可空、老数据按缺省。
#: ``chat_id`` 是来源标注列，只用于加权与共同背景召回，不用于过滤（INV-3，
#: **禁止**写成 SQL 的 WHERE 条件）；``source_kind`` 区分知识来源（owner 显式 /
#: dialogue 对话提炼 / observation 外部观察升格）；``observed_at``/``provenance``/
#: ``review_after`` 是观察型事实的时效与追溯四件套（INV-14，provenance 是结构化
#: 指针，不含真实账号 / URL 明文标识）。
_ATOM_SOURCE_COLUMNS = ("chat_id", "source_kind", "observed_at", "provenance", "review_after")
_L0_SOURCE_COLUMNS = ("source_kind", "provenance")


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: Dict[str, str]) -> None:
    """按需 ALTER 补列（幂等；失败静默——schema 迁移不得阻断打开路径）。"""
    try:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.commit()
    except Exception:
        pass


def _ensure_atom_source_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(conn, "atoms", {
        "chat_id": "TEXT NOT NULL DEFAULT ''",
        "source_kind": "TEXT NOT NULL DEFAULT 'dialogue'",
        "observed_at": "TEXT DEFAULT ''",
        "provenance": "TEXT DEFAULT ''",
        "review_after": "TEXT DEFAULT ''",
    })


def _ensure_l0_source_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(conn, "l0_messages", {
        "source_kind": "TEXT NOT NULL DEFAULT 'chat'",
        "provenance": "TEXT DEFAULT ''",
    })


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
    search_text = summary + topics（topics 从 JSON 列解析；**不存正文**，INV-12）。
    """
    try:
        tok = _fts_tokenizer()
        ver = None
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='narr_fts_schema_version'").fetchone()
            ver = row[0] if row else None
        except Exception:
            pass
        target = f"{tok}-1"
        if ver != target:
            conn.execute("DROP TABLE IF EXISTS narrative_fts")
            conn.execute("CREATE VIRTUAL TABLE narrative_fts USING fts5("
                         "search_text, doc_id UNINDEXED, kind UNINDEXED, "
                         "tokenize='" + tok + "')")
            try:
                rows = conn.execute(
                    "SELECT doc_id, kind, summary, topics FROM narrative_index").fetchall()
                for doc_id, kind, summary, topics in rows:
                    tlist = _json_list(topics)
                    search_text = (str(summary or "") + " " + " ".join(tlist)).strip()
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
    "**跨适用域不许 merge**——把「群聊里怎么说话」和「本机某个工程事实」并成一条，"
    "等于写出谁也用不上的大话;这种情况返回 novel,让它们各自成条。"
    "并完会丢掉某一方的条件、或需要把两条改写成更笼统的一句话才装得下 → novel。"
    "拿不准 → novel。"
    '只返回 JSON:{"decisions": [{"idx": 序号, "action": "novel|duplicate|merge|conflict",'
    ' "target_id": "...", "merged_content": "..."}]}'
)


def _majority_chat_id(msgs: List[Dict[str, str]]) -> str:
    """buffer 消息的多数派 ``chat_id``（atoms 来源标注，§4.3）。

    一轮 flush 的消息可能来自同一窗口（群聊/私聊）；个别消息缺标注（Web/cron）
    不影响多数派。没有可用的返回空串（= 无场合线索，读链不加权）。
    """
    counts: Dict[str, int] = {}
    for m in msgs or []:
        cid = str((m or {}).get("chat_id") or "").strip()
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: kv[1])[0]


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
                             "merged": 0, "merge_rejected": 0, "conflict": 0, "dropped": 0}
    msgs = pop_capture(project_id) if msgs is None else msgs
    if not enabled() or not brain_cfg or len(msgs) < _FLUSH_MIN_ROLES:
        return stats
    digest = "\n".join(f"[{m['role']}] {m['content']}" for m in msgs)[-_FLUSH_MAX_CHARS:]
    if not allow_atoms:
        # 只沉淀叙事层：巩固调度（日记从这里来），L1 一字不写。这一轮不产原子，
        # 所以 L2 场景维护天然没有进料——maybe_maintain 取到零增量就直接跳过，不调 LLM。
        stats["l1_skipped"] = 1
        _flush_tail(project_id, brain_cfg, stats, persona, allow_write, msgs=msgs)
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
        _flush_tail(project_id, brain_cfg, stats, persona, allow_write, msgs=msgs)
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
    _atom_chat = _majority_chat_id(msgs)   # 来源标注（INV-3：只标注不过滤）
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
        kept.append((idx, {"text": str(atoms[idx].get("text", ""))[:_MAX_ATOM_CHARS],
                           "kind": kind, "importance": imp, "peer": peer,
                           "chat_id": _atom_chat, "source_kind": "dialogue"}))
    stats["kept"] = len(kept)
    if not kept:
        # 一条都没留下也要走收尾：巩固的触发不该被"这轮没沉淀事实"卡住
        # （闲聊/全部被判为一次性 也是"收尾了一次"，同白卷分支口径）。
        # L2 这趟通常空转（本轮无新原子），但历史积压的增量要靠这里被带走。
        _flush_tail(project_id, brain_cfg, stats, persona, allow_write, msgs=msgs)
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
            raw = str(d.get("merged_content") or "").strip()
            if len(raw) > _MAX_ATOM_CHARS:
                # 超线 = 模型把两个域并成了一条大话。静默截断会砍掉后半句，所以拒并：
                # 旧条一字不动、新条独立入库，下一轮巩固再判（2026-11-03，见 §5 不变量 6）
                _insert_atom(conn, rid, it, persona, project_id, source_task, now)
                stats["stored"] += 1
                stats["merge_rejected"] += 1
            else:
                merged = raw or it["text"]
                conn.execute("UPDATE atoms SET content=?, updated=?, importance=max(importance, ?), "
                             "access_count=access_count+1, last_access=? WHERE record_id=?",
                             (merged, now, it["importance"], now, str(d.get("target_id"))))
                _fts_upsert(conn, str(d.get("target_id")), merged)
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

    _flush_tail(project_id, brain_cfg, stats, persona, allow_write, msgs=msgs)
    return stats


def _flush_tail(project_id: str, brain_cfg: Optional[Dict[str, Any]],
                stats: Dict[str, int], persona: str, allow_write: bool,
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
            cid = str((m or {}).get("chat_id") or "").strip()
            if cid:
                by_chat.setdefault(cid, []).append(str(m.get("content") or ""))
        for cid, texts in by_chat.items():
            update_context_buffer(cid, texts, source_kind="chat")
            chats.append(cid)
        if chats:
            run_context_reflections(brain_cfg, persona, chats=chats)
    except Exception:
        pass
    maybe_consolidate(brain_cfg, persona)


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
                 persona: str, origin: str, source_task: str, now: str) -> None:
    """落一条 atom（四态裁决后的唯一写点）。

    §4.3 出处/时效列：``chat_id`` 取 buffer 多数派（来源标注，不过滤）；
    ``source_kind`` 缺省 ``dialogue``（观察升格走 ``_insert_observation_atom``）；
    ``observed_at``/``provenance``/``review_after`` 观察型事实才带。
    P2（死维度清理）：``persona`` 冻结（写入恒空串；形参保留仅为调用兼容），
    ``owner`` 列已随多租户遗产整体移除。
    """
    conn.execute("INSERT OR REPLACE INTO atoms(record_id, content, kind, persona, origin, "
                 "source_task, importance, salience, access_count, last_access, state, "
                 "version, created, updated, peer, chat_id, source_kind, "
                 "observed_at, provenance, review_after) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, it["text"], it.get("kind", "fact"), "", origin or "",
                  source_task or "", float(it.get("importance", 0.5)),
                  float(it.get("importance", 0.5)), 0, "", "active", 1, now, now,
                  str(it.get("peer") or ""),
                  str(it.get("chat_id") or ""), str(it.get("source_kind") or "dialogue"),
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


def search_brain(query: str, persona: str = "", k: Optional[int] = None,
                 reinforce: bool = True,
                 conn: Optional[sqlite3.Connection] = None,
                 peer: str = "", chat_id: str = "") -> List[Dict[str, Any]]:
    """融合排序检索：relevance(bm25) × importance × recency，active-only，floor 过滤。

    salience 不再衰减（P7，2026-11-02，对齐原仓库 priority 语义）：排序中直接取
    importance 字段值；dormant/archived 状态机已删除，state 只剩 active/tombstone。
    peer（可选，M3）：当前对话对象「名片(uid)」——关于 TA 的记忆终分加权（×1.25，
    **不是过滤**：全局事实照常召回）；不传 = 行为与无 peer 维度时一致。
    chat_id（可选，M2，§6.2）：当前场合键——同场合标注的 atoms 加权 ×(1+w_ctx)、
    与圈层共同话题词面相交的再加 ×(1+w_bg)。**加权不过滤**（INV-3）：跨场合记忆
    照常召回，只是排得靠后；``chat_id`` 绝不出现在 SQL WHERE 里。
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
        if chat_id:
            try:
                circle_topics = [str(t).lower() for t in
                                 (get_circle_note(chat_id) or {}).get("topics") or [] if t]
            except Exception:
                circle_topics = []
        states = "('active')"
        # P2：persona 过滤已移除（死维度——生产链路恒空，过滤路径从未生效）；
        # ``persona`` 形参保留仅为调用兼容，不再参与任何 SQL。
        rows: List[Any] = []
        if match:
            params: List[Any] = [match, max(k * 3, 15)]
            rows = c.execute(
                f"SELECT f.record_id, a.content, bm25(atoms_fts) AS r, a.kind, a.importance, "
                f"a.access_count, a.updated, a.last_access, a.peer, a.chat_id FROM atoms_fts f "
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
            if chat_id and str(ach or "") == str(chat_id):
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
                         "chat_id": str(ach or ""), "ctx_hit": ctx_hit})
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
                                     "chat_id": "", "ctx_hit": False})
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
                      peer: str = "", chat_id: str = "") -> Tuple[str, Dict[str, Any]]:
    """L1 检索文本块（供工具/调试；<relevant-memories> 形态）；零命中也返回 meta（可观测）。

    peer（可选，M3）：不显式传时从 query 里解析最近的「名片(uid)」标注（M1 约定）；
    关于 TA 的记忆加权，条目带「关于 X：」前缀。chat_id（可选，M2）：场合加权同源。
    """
    hits: List[Dict[str, Any]] = []
    try:
        hits = search_brain(query, persona=persona,
                            reinforce=True, peer=peer or extract_peer(query),
                            chat_id=chat_id)
    except Exception:
        hits = []
    meta = {"hits": len(hits), "chars": sum(len(h["content"]) for h in hits),
            "placement": "tool", "store": "brain",
            "peer": peer or extract_peer(query), "chat_id": chat_id,
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
                             sent: bool = False, llm_calls_delta: int = 0) -> None:
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
               "sent": bool(sent), "llm_calls_delta": int(llm_calls_delta)}
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
                           chat_ids: Optional[List[str]] = None,
                           peers: Optional[List[str]] = None,
                           date_basis: str = "local") -> bool:
    """``narrative_index`` 的**唯一写入口**（工程侧；LLM 沙箱无权限，INV-12）。

    幂等键 ``doc_id + kind``、覆盖不追加（§13.2.3）；``narrative_fts`` 先删后插
    （与 ``_fts_upsert`` 同款，恒一行）。``search_text = summary + topics``，
    **不存正文**（INV-12）；失败静默返回 False（索引失败不阻断写块，正文仍是真相）。
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
                "time_from, time_to, summary, topics, chat_ids, peers, date_basis, updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, kind, str(project_id or ""), str(date_from or ""),
                 str(date_to or ""), str(time_from or ""), str(time_to or ""),
                 str(summary or "")[:200], json.dumps(topics, ensure_ascii=False),
                 json.dumps([str(c) for c in (chat_ids or [])], ensure_ascii=False),
                 json.dumps([str(p) for p in (peers or [])], ensure_ascii=False),
                 str(date_basis or "local"), _now()))
            conn.execute("DELETE FROM narrative_fts WHERE doc_id=? AND kind=?",
                         (doc_id, kind))
            search_text = (str(summary or "") + " " + " ".join(topics)).strip()
            if search_text:
                conn.execute("INSERT INTO narrative_fts(search_text, doc_id, kind) "
                             "VALUES(?,?,?)", (search_text[:400], doc_id, kind))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False


def get_narrative_row(doc_id: str, kind: str = "") -> Optional[Dict[str, Any]]:
    """按 doc_id（可选 kind）取索引行（供 narrative_read 拿 project_id / 场合标注）。

    裸文件名（不带 ``pid:`` 前缀）兼容解析：跨项目同名时不猜——返回 None，
    让调用方用检索结果里带前缀的 doc_id 重试。
    """
    d = str(doc_id or "").strip()
    try:
        conn = open_brain()
        try:
            sel = ("SELECT doc_id, kind, project_id, date_from, date_to, time_from, "
                   "time_to, summary, topics, chat_ids, peers, date_basis, updated "
                   "FROM narrative_index ")
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
            "time_to", "summary", "topics", "chat_ids", "peers", "date_basis", "updated")
    d = dict(zip(keys, row))
    d["topics"] = _json_list(d.get("topics"))
    d["chat_ids"] = _json_list(d.get("chat_ids"))
    d["peers"] = _json_list(d.get("peers"))
    return d


def search_narrative(query: str = "", kind: str = "", date_from: str = "",
                     date_to: str = "", peer: str = "", chat_id: str = "",
                     limit: int = 8) -> List[Dict[str, Any]]:
    """叙事检索核心（§13.3.1）：只返回条目**不返回正文**（正文走 ``read_narrative``）。

    排序 = bm25 相关性 + 时间近因 + 场合/对象加权；``chat_id`` 只加权不过滤
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
                    "n.time_from, n.time_to, n.summary, n.topics, n.chat_ids, n.peers "
                    f"FROM narrative_index n WHERE {where}{kind_sql} "
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
                              "time_from", "time_to", "summary", "topics", "chat_ids",
                              "peers"), row))
                key = f"{d['doc_id']}|{d['kind']}"
                if key in seen:
                    continue
                d["topics"] = _json_list(d.get("topics"))
                d["chat_ids"] = _json_list(d.get("chat_ids"))
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
                                  "time_from", "time_to", "summary", "topics", "chat_ids",
                                  "peers"), row))
                    key = f"{d['doc_id']}|{d['kind']}"
                    if key in seen:
                        continue
                    d["topics"] = _json_list(d.get("topics"))
                    d["chat_ids"] = _json_list(d.get("chat_ids"))
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
    peer_key = str(peer or "").strip() or extract_peer(q)
    cid = str(chat_id or "").strip()
    qterms = set(_query_terms(q))
    scored: List[Dict[str, Any]] = []
    for d, rel in cand:
        rec_days = _days_since(d.get("time_to") or d.get("date_to") or "")
        rec = pow(2.718281828459045, -rec_days / 180.0)
        final = 0.6 * rel + 0.4 * rec
        chat_ids = d.get("chat_ids") or []
        if cid and cid in chat_ids:
            final *= (1.0 + _ctx_cfg("w_ctx", 0.25))       # 同场合标注：加权不过滤
        if peer_key and any(peer_key == str(p) for p in (d.get("peers") or [])):
            final *= 1.25                                   # 关于当前对象
        if cid and qterms:
            # 共同背景（R8，w_bg）：本场合叙事且正文/主题词面与 query 相交
            face = " ".join([str(d.get("summary") or ""), " ".join(d.get("topics") or [])]).lower()
            if any(t in face for t in qterms):
                final *= (1.0 + _ctx_cfg("w_bg", 0.10))
        if final <= 0.0:
            continue
        scored.append({"doc_id": d["doc_id"], "kind": d["kind"],
                       "project_id": d["project_id"],
                       "date": str(d.get("date_from") or ""),
                       "summary": str(d.get("summary") or ""),
                       "topics": d.get("topics") or [],
                       "score": round(min(final, 1.5), 6),
                       "chat_ids": chat_ids, "peers": d.get("peers") or []})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:n]


def read_narrative(doc_id: str, kind: str = "") -> Dict[str, Any]:
    """读叙事正文（文件是唯一真相；索引只给指针）。kind 空 → 索引推断 → diary 兜底。

    返回 ``{ok, kind, doc_id, project_id, chars, text, summary, chat_ids, peers}``；
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
        # H-4：doc_id 是裸文件名，正文在全局叙事目录；旧 ``pid:`` 行做一次性兼容剥离
        fname = doc_id.split(":", 1)[1] if ":" in doc_id else doc_id
        raw = scene_executor.read_scene_block(project_id, fname)
        _, body = scene_executor.parse_scene_block(raw)
    if not body:
        return {"ok": False, "error": f"未找到叙事正文: {doc_id}"}
    return {"ok": True, "kind": kind, "doc_id": doc_id, "project_id": project_id,
            "chars": len(body), "text": body,
            "summary": str((row or {}).get("summary") or ""),
            "chat_ids": (row or {}).get("chat_ids") or [],
            "peers": (row or {}).get("peers") or []}


def _sid_project_map() -> Dict[str, str]:
    """会话文件名 → 项目目录名（l0 回填「项目 + 会话文件 + 日期」的连接键）。"""
    out: Dict[str, str] = {}
    try:
        for p in rp.projects_root().glob("*/*.jsonl"):
            out[p.stem] = p.parent.name
    except Exception:
        pass
    return out


def _diary_material_facts(conn: sqlite3.Connection, sids: List[str], utc_day: str) -> Dict[str, Any]:
    """某 UTC 日、本项目会话在 L0 里的素材事实：本地日多数派 / 时间区间 / 场合 / 对象。"""
    out: Dict[str, Any] = {"local_days": {}, "chat_ids": set(), "peers": set(),
                           "ts_min": "", "ts_max": ""}
    if not sids:
        return out
    marks = ",".join("?" for _ in sids)
    try:
        rows = conn.execute(
            f"SELECT recorded_at, chat_id, speaker FROM l0_messages "
            f"WHERE session_id IN ({marks}) AND recorded_at >= ? AND recorded_at < ?",
            tuple(sids) + (f"{utc_day}T00:00:00", f"{utc_day}T24:00")).fetchall()
    except Exception:
        return out
    from omni_core.local import localtime

    for ts, cid, spk in rows:
        d = localtime.local_date(str(ts or ""))
        if d:
            out["local_days"][d] = out["local_days"].get(d, 0) + 1
        if cid:
            out["chat_ids"].add(str(cid))
        if spk:
            out["peers"].add(str(spk))
        t = str(ts or "")
        if t:
            out["ts_min"] = min(out["ts_min"] or t, t)
            out["ts_max"] = max(out["ts_max"] or t, t)
    return out


def migrate_narrative_once(force: bool = False) -> Dict[str, int]:
    """一次性迁移（§13.5 第 3-5 步，幂等；meta ``narrative_index_built`` 已建即跳过）：

    1. 扫各工作项目 ``diary-*.md`` 与全局关系卡，重建索引（META 缺新键按空处理）；
    2. 老日记缺时间区间/场合标注时，从 ``l0_messages`` 按「项目 + 会话文件 + 日期」
       best-effort 回填；``date_from/date_to`` 一律经 ``local_date()`` 折算，失败退化为
       「文件名日期当日」与空数组并标 ``date_basis=legacy``——**不猜测、不改写正文**；
    3. 切换日撞名：素材真实本地日 D ≠ 文件名日期 且 ``diary-D.md`` 不存在 → 重命名
       并同步 ``scene_index``；已存在 → 保留老文件、索引标 ``legacy``，不合并不覆盖。

    只在迁移期执行一次；运行期由巩固期 upsert 增量维护。
    """
    out = {"indexed": 0, "renamed": 0, "legacy": 0, "relations": 0}
    if not enabled():
        return out
    try:
        row = open_brain().execute(
            "SELECT v FROM meta WHERE k='narrative_index_built'").fetchone()
        if row and str(row[0]) == "1" and not force:
            out["skipped"] = 1
            return out
    except Exception:
        pass
    # 全量重建：正文文件是唯一真相——先清空索引（含历史脏行/裸文件名旧行）再回填
    try:
        c = open_brain()
        c.execute("DELETE FROM narrative_index")
        c.execute("DELETE FROM narrative_fts")
        c.commit()
        c.close()
    except Exception:
        pass
    from omni_core.local import localtime, scene_executor

    sid_map = _sid_project_map()
    pid_sids: Dict[str, List[str]] = {}
    for sid, pid in sid_map.items():
        pid_sids.setdefault(pid, []).append(sid)
    conn = None
    try:
        conn = open_brain()
    except Exception:
        conn = None
    renamed_projects: set = set()
    # H-4：全局叙事目录优先；旧项目目录仅作迁移期兼容来源（同一 doc_id 只索引一次）
    diary_sources = [(p, "") for p in sorted(scene_executor.diary_dir().glob("diary-2*.md"))]
    diary_sources += [
        (p, p.parent.parent.parent.name)
        for p in sorted(rp.projects_root().glob("*/memory/scene_blocks/diary-2*.md"))
    ]
    seen_docs: set = set()
    try:
        for p, pid in diary_sources:
            try:
                name = p.name
                if name.startswith("diary-month-"):
                    continue
                if name in seen_docs:
                    continue
                seen_docs.add(name)
                fname_day = name[len("diary-"):-3]           # diary-YYYY-MM-DD.md
                meta, _body = scene_executor.parse_scene_block(
                    p.read_text(encoding="utf-8"))
                # 全局目录由新代码按本地日写出，无需 L0 回填；旧目录才做 best-effort
                facts = (conn and pid and _diary_material_facts(
                    conn, pid_sids.get(pid, []), fname_day)) or {}
                local_days: Dict[str, int] = facts.get("local_days") or {}
                target_day = ""
                date_basis = "local" if not pid else "legacy"
                if local_days:
                    target_day = max(local_days.items(), key=lambda kv: kv[1])[0]
                if target_day and target_day != fname_day:
                    dst = p.parent / f"diary-{target_day}.md"
                    if not dst.exists():
                        try:
                            p.rename(dst)
                            out["renamed"] += 1
                            renamed_projects.add(pid)
                            name, fname_day = dst.name, target_day
                            date_basis = "local"
                        except Exception:
                            date_basis = "legacy"
                    else:
                        date_basis = "legacy"                 # 撞名：保留老文件，标降级
                elif target_day:
                    date_basis = "local"
                # 索引行（文件名/日期以最终落点为准；失败退化=文件名当日 + legacy）
                final_day = target_day if date_basis == "local" else fname_day
                topics = _normalize_topics(_json_list(_meta_extra_keys(
                    (p.parent / name).read_text(encoding="utf-8")).get("topics")))
                ok = upsert_narrative_index(
                    name, "diary", project_id="",
                    date_from=final_day, date_to=final_day,
                    time_from=str(facts.get("ts_min") or ""),
                    time_to=str(facts.get("ts_max") or ""),
                    summary=str(meta.get("summary") or ""),
                    topics=topics,
                    chat_ids=sorted(facts.get("chat_ids") or []),
                    peers=sorted(facts.get("peers") or []),
                    date_basis=date_basis)
                if ok:
                    out["indexed"] += 1
                    if date_basis == "legacy":
                        out["legacy"] += 1
            except Exception:
                continue
        # 月记：月份边界由日历给出；存量项目月记标 legacy
        month_sources = list(scene_executor.diary_dir().glob("diary-month-2*.md"))
        month_sources += list(rp.projects_root().glob(
            "*/memory/scene_blocks/diary-month-2*.md"))
        seen_months: set = set()
        for p in sorted(month_sources):
            try:
                if p.name in seen_months:
                    continue
                seen_months.add(p.name)
                ym = p.name[len("diary-month-"):-3]
                meta, _ = scene_executor.parse_scene_block(p.read_text(encoding="utf-8"))
                d1, d2 = localtime.local_month_bounds(ym)
                topics = _normalize_topics(_json_list(_meta_extra_keys(
                    p.read_text(encoding="utf-8")).get("topics")))
                is_global = p.parent == scene_executor.diary_dir()
                if upsert_narrative_index(p.name, "month", project_id="",
                                          date_from=d1, date_to=d2,
                                          summary=str(meta.get("summary") or ""),
                                          topics=topics,
                                          date_basis="local" if is_global else "legacy"):
                    out["indexed"] += 1
                    if not is_global:
                        out["legacy"] += 1
            except Exception:
                continue
        # 关系卡：全局一份、按 uid；对象标注 = 名片(uid)（与 extract_peer 同形）
        try:
            reldir = rp.global_relations()
            import re as _re

            for p in sorted(reldir.glob("*.md")):
                raw = p.read_text(encoding="utf-8")
                m = _re.search(r"^name:\s*(.+)$", raw.split("-----META-END-----", 1)[0],
                               _re.M)
                uid_m = _re.search(r"^uid:\s*(.+)$", raw.split("-----META-END-----", 1)[0],
                                   _re.M)
                name = (m.group(1).strip() if m else "")
                uid = (uid_m.group(1).strip() if uid_m else p.stem)
                meta_keys = _meta_extra_keys(raw)
                peer_label = f"{name}({uid})" if name else uid
                if upsert_narrative_index(p.stem, "relation", project_id="",
                                          summary="", topics=_normalize_topics(
                                              _json_list(meta_keys.get("topics"))),
                                          chat_ids=[], peers=[peer_label],
                                          date_basis="local"):
                    out["relations"] += 1
                    out["indexed"] += 1
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    for pid in renamed_projects:
        try:
            scene_executor.sync_scene_index(pid)
        except Exception:
            pass
    try:
        c = open_brain()
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('narrative_index_built','1')")
        c.commit()
        c.close()
    except Exception:
        pass
    return out


def _meta_extra_keys(raw: str) -> Dict[str, str]:
    """读场景块 META 区的**全部** key: value 行（含 parse_scene_block 不认识的新键）。"""
    import re as _re

    out: Dict[str, str] = {}
    try:
        start = raw.find("-----META-START-----")
        end = raw.find("-----META-END-----")
        if start == -1 or end == -1 or end < start:
            return out
        for ln in raw[start + len("-----META-START-----"):end].splitlines():
            m = _re.match(r"^([A-Za-z_]+):\s*(.*)$", ln.strip())
            if m:
                out[m.group(1)] = m.group(2).strip()
    except Exception:
        pass
    return out


_narr_migrate_started = threading.Lock()
_narr_migrate_state: Dict[str, bool] = {"running": False}


def start_narrative_migration() -> bool:
    """后台发起一次性叙事索引迁移（服务启动调用；meta 标记让它天然只跑一次）。"""
    if not enabled():
        return False
    with _narr_migrate_started:
        if _narr_migrate_state["running"]:
            return False
        _narr_migrate_state["running"] = True

    def _go():
        try:
            migrate_narrative_once()
        except Exception:
            pass
        finally:
            _narr_migrate_state["running"] = False

    threading.Thread(target=_go, name="narrative-migration", daemon=True).start()
    return True


# ---- 圈层语境（circle_note 读取端 + 近窗词面；写链/reflection 见 §5.2，M3） ----

def get_circle_note(chat_id: str) -> Dict[str, Any]:
    """读一个场合的圈层语境摘要（一行一场合；无 → 空字典）。

    返回 ``{summary, topics[], tone, source_count, source_kinds[], version, updated}``。
    语境预期**不是可引用事实**（INV-5）：消费端只有 ``<current-context>`` 的
    话题/语气字段与检索加权，任何工具都不得把它当返回值原文。
    """
    if not (chat_id or "").strip() or not enabled():
        return {}
    try:
        conn = open_brain()
        try:
            row = conn.execute(
                "SELECT summary, topics, tone, source_count, source_kinds, version, "
                "updated, prev_summary FROM circle_note WHERE chat_id=?", (str(chat_id),)).fetchone()
        finally:
            conn.close()
    except Exception:
        return {}
    if not row:
        return {}
    return {"chat_id": str(chat_id), "summary": str(row[0] or ""),
            "topics": _json_list(row[1]), "tone": str(row[2] or ""),
            "source_count": int(row[3] or 0), "source_kinds": _json_list(row[4]),
            "version": int(row[5] or 0), "updated": str(row[6] or ""),
            "prev_summary": str(row[7] or "")}


def recent_l0_lines(chat_id: str = "", session_id: str = "",
                    limit: int = 40) -> List[str]:
    """近窗聊天原文行（词面提取/语境链取料用；时间倒序，最多 ``limit`` 条）。"""
    if not enabled():
        return []
    try:
        c = _l0_conn()
        cid, sid = str(chat_id or ""), str(session_id or "")
        scope, params = "", []
        if cid or sid:
            scope = "WHERE (l.chat_id = ? OR l.session_id = ?)"
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
# 原料只有**通用信号**（话题词面 / 条数节奏 / 称呼形态 / 长度分布 / 来源道），
# 不写他人原话（INV-6）、不含领域词表（INV-4）；产物是**语境预期**（这个场合大致
# 聊什么、什么分寸），不是可引用事实——永不进 atoms、不参与四态（INV-5）。
# 失败保留上一版、静默不阻断（§5.6）。

_CIRCLE_SYSTEM = (
    "你是圈子氛围观察器。给你某个聊天场合最近一段时间的**聚合信号**"
    "（高频话题词面、消息条数、说话人标注形态、消息长度分布、来源构成）——"
    "没有任何人的原话。请提炼这个场合的「语境预期」：大家大致在聊什么话题、"
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


def update_context_buffer(chat_id: str, texts: List[str],
                          source_kind: str = "chat") -> Dict[str, Any]:
    """按场合累积通用信号进 ``context_buffer``（**覆盖不追加**；INV-4/INV-6）。

    buffer 只存词面与计数，不存原话；``source_kinds`` 标注来源构成（INV-5）。
    返回累积后的 buffer 概要（观测用）；失败返回空字典（静默）。
    """
    chat_id = str(chat_id or "").strip()
    if not chat_id or not enabled():
        return {}
    now = _now()
    try:
        conn = open_brain()
        try:
            row = conn.execute("SELECT buffer, msg_count FROM context_buffer WHERE chat_id=?",
                               (chat_id,)).fetchone()
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
            import re as _re

            addr = sum(1 for t in texts if _re.search(r"[「]?[^「」():\n]{1,32}[」]?\(\d{4,}\)", t))
            buf["addr_ratio"] = round(addr / len(texts), 2) if texts else buf.get("addr_ratio", 0.0)
            buf["updated"] = now
            conn.execute(
                "INSERT OR REPLACE INTO context_buffer(chat_id, buffer, msg_count, updated) "
                "VALUES(?,?,?,?)",
                (chat_id, json.dumps(buf, ensure_ascii=False), int(buf["msg_count"]), now))
            conn.commit()
            return buf
        finally:
            conn.close()
    except Exception:
        return {}


def _circle_note_due(chat_id: str, buf: Dict[str, Any]) -> bool:
    """触发判定（§5.2）：条数阈值或间隔阈值；每场合每巩固周期 ≤1 次（N-13）。"""
    try:
        conn = open_brain()
        try:
            row = conn.execute("SELECT last_reflection FROM circle_note WHERE chat_id=?",
                               (chat_id,)).fetchone()
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


def reflect_circle_note(brain_cfg: Dict[str, Any], chat_id: str,
                        persona: str = "") -> bool:
    """对一个场合跑一次 reflection，重写（不追加）``circle_note``。

    - 原料 = context_buffer 的聚合信号（无原话，INV-6）；一次 ``chat_json``（§13.10
      的 D-3 思路：产物一次拿全，topics 复用同一次调用）。
    - 重写保留上一版（``prev_summary``）供回滚；失败静默保留旧版（§5.6）。
    - **不写 atoms**（INV-5）、不读叙事正文（INV-10）。
    """
    if not enabled() or not brain_cfg or not (chat_id or "").strip():
        return False
    try:
        conn = open_brain()
        try:
            row = conn.execute("SELECT buffer FROM context_buffer WHERE chat_id=?",
                               (str(chat_id),)).fetchone()
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
        user = ("场合键: " + str(chat_id) + "\n"
                + "聚合信号（无任何原话）:\n"
                + json.dumps({"topics_词面": buf.get("topics") or [],
                              "累积条数": buf.get("msg_count") or 0,
                              "来源构成": buf.get("source_kinds") or [],
                              "称呼形态比例": buf.get("addr_ratio"),
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
            old = conn.execute("SELECT summary, version FROM circle_note WHERE chat_id=?",
                               (str(chat_id),)).fetchone()
            prev_summary = str(old[0] or "") if old else ""
            version = (int(old[1] or 0) if old else 0) + 1
            conn.execute(
                "INSERT OR REPLACE INTO circle_note(chat_id, summary, topics, tone, "
                "source_count, source_kinds, prev_summary, version, msg_count, "
                "last_reflection, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (str(chat_id), summary, json.dumps(topics, ensure_ascii=False),
                 tone, int(buf.get("msg_count") or 0),
                 json.dumps([str(k) for k in (buf.get("source_kinds") or [])],
                            ensure_ascii=False),
                 prev_summary, version, 0, _now(), _now()))
            conn.execute("UPDATE context_buffer SET msg_count=0 WHERE chat_id=?",
                         (str(chat_id),))
            # buffer JSON 里的计数同步清零（下轮累积从 0 起；词面保留供下次合并）
            try:
                buf["msg_count"] = 0
                conn.execute("UPDATE context_buffer SET buffer=? WHERE chat_id=?",
                             (json.dumps(buf, ensure_ascii=False), str(chat_id)))
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


def run_context_reflections(brain_cfg: Optional[Dict[str, Any]], persona: str = "",
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
                "SELECT chat_id, COUNT(*) FROM l0_messages "
                "WHERE chat_id != '' AND source_kind='chat' "
                "GROUP BY chat_id HAVING COUNT(*) >= ? ORDER BY 2 DESC LIMIT 12",
                (min_c,)).fetchall()
            have: set = set()
            try:
                conn = open_brain()
                have = {str(r[0]) for r in
                        conn.execute("SELECT chat_id FROM circle_note").fetchall()}
                conn.close()
            except Exception:
                pass
            targets = [str(r[0]) for r in rows if str(r[0]) not in have]
        except Exception:
            targets = []
    elif chats:
        targets = [str(c) for c in chats if str(c).strip()]
    for chat_id in targets:
        if _circle_reflection_budget_left() <= 0:
            break
        try:
            if coldstart:
                # 冷启动直接跑（无 buffer 时先从近窗攒一份信号）
                if not update_context_buffer(chat_id, recent_l0_lines(chat_id=chat_id,
                                                                      limit=60)):
                    continue
                if reflect_circle_note(brain_cfg, chat_id, persona):
                    done += 1
                    _circle_reflection_budget_used()
            else:
                row = {}
                try:
                    conn = open_brain()
                    try:
                        r = conn.execute("SELECT buffer FROM context_buffer WHERE chat_id=?",
                                         (str(chat_id),)).fetchone()
                        if r:
                            row = json.loads(str(r[0] or "") or "{}")
                    finally:
                        conn.close()
                except Exception:
                    row = {}
                if not _circle_note_due(chat_id, row or {}):
                    continue
                if reflect_circle_note(brain_cfg, chat_id, persona):
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
            data = llm_judge.chat_json(brain_cfg, _CONSIST_SYSTEM,
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

    # 8) 日记步（plan persona-social-memory M2）：她的一天 → L2 diary 块（失败静默）。
    #    补齐模式（2026-11-03）：不在线的日子由下一次巩固补写，回看 7 天，超窗放弃。
    try:
        diary_n = maintain_diaries(brain_cfg, persona)
        if diary_n:
            out["diary_written"] = diary_n
        if maintain_month_diary(brain_cfg, persona):
            out["month_diary_written"] = 1
        # 9) 关系卡步：按**人**把近期往来压成印象卡（全局一份，跨群/跨项目）
        rel = maintain_relations(brain_cfg, persona)
        if rel:
            out["relations_written"] = rel
    except Exception:
        pass
    # 9.5) 语境链冷启动（M3）：还没有 circle_note 的老场合各跑一次首版 reflection
    # （受周期预算 ≤5 约束；原料 = L0 近窗聚合信号，无原话；失败静默）
    try:
        cs_n = run_context_reflections(brain_cfg, persona, coldstart=True)
        if cs_n:
            out["circle_notes_coldstart"] = cs_n
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
    # topics（plan §13.10，D-3）：正文按 INV-12 不进索引，检索词面靠 summary+topics——
    # 首句往往不含正文里聊的主题词，topics 补上（钳制归一化，不是词表，INV-4）。
    "topics：2-5 个可检索的主题词或短语，名词优先，取自本段素材；"
    "不要整句、不要评价、不要臆测；没有则空数组。"
    "\n只返回 JSON:{\"diary\": \"第一人称日记，≤900 字；没有则空串\", "
    "\"topics\": [\"...\", \"...\"]}"
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
    # chat_id / speaker（场合与对象标注，进 META 与叙事索引）。
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
                                        "chat_ids": set(), "peers": set()}
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
                    recs.append((ts, f"[{role}] {content}"))
                    if ts:
                        meta_acc["ts_min"] = min(meta_acc["ts_min"] or ts, ts)
                        meta_acc["ts_max"] = max(meta_acc["ts_max"] or ts, ts)
                    cid = str(rec.get("chat_id") or "").strip()
                    if cid:
                        meta_acc["chat_ids"].add(cid)
                    if role == "user":
                        spk = extract_peer(content)
                        if spk:
                            meta_acc["peers"].add(spk)
            if recs:
                pid = f.parent.name
                by_project.setdefault(pid, []).extend(recs)
                file_tails.setdefault(pid, {})[f.name] = recs[-_DIARY_FLOOR_LINES:]
                acc = span.setdefault(pid, {"ts_min": "", "ts_max": "",
                                            "chat_ids": set(), "peers": set()})
                acc["ts_min"] = min(acc["ts_min"] or meta_acc["ts_min"], meta_acc["ts_min"]) \
                    if meta_acc["ts_min"] else acc["ts_min"]
                acc["ts_max"] = max(acc["ts_max"] or meta_acc["ts_max"], meta_acc["ts_max"]) \
                    if meta_acc["ts_max"] else acc["ts_max"]
                acc["chat_ids"].update(meta_acc["chat_ids"])
                acc["peers"].update(meta_acc["peers"])
    except Exception:
        pass
    if not by_project:
        return 0
    # 该日完成任务（标题级一句话；任务明细走轨迹，日记只收经历）
    try:
        from omni_core.local.task_store import TaskStore

        done_day = [t for t in TaskStore.list()
                    if localtime.local_date(str(t.get("finished_at") or "")) == day]
    except Exception:
        done_day = []
    # H-4（§11）：跨项目聚合成「她的一天」——一次 LLM、全局一篇，不再按 pid 各写一篇
    try:
        if (scene_executor.diary_dir() / f"diary-{day}.md").exists():
            return 0                                    # 一日一篇，幂等
        all_items = [x for items in by_project.values() for x in items]
        # 素材 v3：全局尾窗 ∪ 每会话保底（保证被看见），按 ts 排序、硬顶封 token
        chosen = set(sorted(all_items, key=lambda x: x[0])[-_DIARY_TAIL_LINES:])
        for tails in file_tails.values():
            for tail in tails.values():
                chosen.update(tail)
        recs = [line for _, line in sorted(chosen, key=lambda x: x[0])]
        recs = recs[-_DIARY_MAX_LINES:]
        material = "\n".join(recs)
        task_lines = "\n".join(
            f"- 完成任务：{str(t.get('title') or t.get('objective') or '')[:60]}"
            for t in done_day)
        if task_lines:
            material += "\n\n[当日完成任务]\n" + task_lines
        if len(material.strip()) < 80:
            return 0                                    # 素材太薄，白卷
        system = _DIARY_SYSTEM + (
            "\n今天的事可能发生在几个不同场合（群聊、私聊、工作任务）。写**一篇**连贯的"
            "第一人称日记，按场合自然分段，别写成互不相干的流水账，也不要标项目名。")
        known = self_names()
        if known:
            system += (
                "\n身份锚：以下是「你」在本设备的已知名字（任一出现都指你自己，"
                f"不是聊天对象）：{'、'.join(known)}。")
        data = llm_judge.chat_json(brain_cfg, system, material, timeout=90.0)
        diary = str((data or {}).get("diary") or "").strip()
        if not diary:
            return 0
        topics = _normalize_topics((data or {}).get("topics"))
        # 场合/对象/时间区间跨项目合并——索引仍是一行（键 = 日期）
        chat_ids: set = set()
        peers: set = set()
        ts_min = ts_max = ""
        for acc0 in span.values():
            chat_ids.update(acc0.get("chat_ids") or [])
            peers.update(acc0.get("peers") or [])
            if acc0.get("ts_min"):
                ts_min = min(ts_min or acc0["ts_min"], acc0["ts_min"])
            if acc0.get("ts_max"):
                ts_max = max(ts_max or acc0["ts_max"], acc0["ts_max"])
        if scene_executor.write_diary_block(
                "", day, diary, topics=topics,
                chat_ids=sorted(chat_ids), peers=sorted(peers),
                time_from=ts_min, time_to=ts_max):
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
                chat_ids=sorted(chat_ids), peers=sorted(peers),
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


def maintain_diaries(brain_cfg: Optional[Dict[str, Any]], persona: str = "") -> int:
    """日记步总入口（巩固期第 8 步，补齐模式）：对窗口内每天独立尝试 ``maintain_diary``。

    已有日记幂等跳过、素材太薄白卷跳过（两者都不调 LLM）；只有「缺日记且有素材」的
    日子才产生一次 LLM 调用，单次巩固最多 ``_DIARY_CATCHUP_DAYS`` 次。
    """
    written = 0
    for day in _catchup_diary_days():
        try:
            written += maintain_diary(brain_cfg, persona, day=day)
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
    "topics：2-5 个可检索的主题词或短语（我们之间的事、共同的梗），名词优先，"
    "取自往来素材；不要整句、不要评价；没有则空数组。"
    '只返回 JSON:{"card": "...", "topics": ["...", "..."]}'
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
            topics = _normalize_topics((data or {}).get("topics"))
            meta_lines = (f"name: {info.get('name') or uid}\nuid: {uid}\n"
                          f"updated: {_now()}\n")
            if topics:
                meta_lines += f"topics: {json.dumps(topics, ensure_ascii=False)}\n"
            body = ("-----META-START-----\n" + meta_lines + "-----META-END-----\n"
                    + card[:600])
            (d / f"{uid}.md").write_text(body, encoding="utf-8")
            peer_label = f"{info.get('name') or uid}({uid})"
            upsert_narrative_index(uid, "relation", project_id="",
                                   summary="", topics=topics,
                                   chat_ids=[], peers=[peer_label],
                                   date_basis="local")
            written += 1
        except Exception:
            continue
    return written


def maintain_month_diary(brain_cfg: Optional[Dict[str, Any]], persona: str = "") -> int:
    """月记步（plan persona-social-memory M2.5）：把**已结束月份**的日记压缩为月记块。

    月记 ``diary-month-<月份>.md`` 是消费地图（META.summary = 关键人物/事件一行式）——
    她据此知道该翻哪几篇旧日记。规则：只压已结束月份；<3 篇不压；已有月记幂等跳过；
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
            data = llm_judge.chat_json(brain_cfg, _DIARY_MONTH_SYSTEM, material, timeout=90.0)
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
             persona: str = "", brain_cfg: Optional[Dict[str, Any]] = None,
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
                     persona or str(_cfg("persona") or ""), origin, source_task, now)
        conn.commit()
        return rid
    except Exception:
        return ""
    finally:
        conn.close()


def update_memory(record_id: str, new_text: str, persona: str = "") -> str:
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
    场合标签（``chat_id``）只作渲染元信息，不用于过滤批次。``project_id`` 参数保留
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
            "SELECT record_id, content, kind, importance, created, updated, chat_id, "
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
             "chat_id": r[6] or "", "source_kind": r[7] or "dialogue"} for r in rows]


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
