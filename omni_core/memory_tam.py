"""记忆层 · TAM(TencentDB Agent Memory)最小切片。

分层与机制见 doc/plans/memory-architecture.md。三件套:
- 写链:capture(会话消息入 buffer)→ flush(任务收尾触发:LLM 提炼 atoms(带 scope 标注)
  → 三态判重 → 落库)
- 存储:每项目 ``projects/<pid>/memory/index.db`` + 全局层 ``~/.omniagent/memory/index.db``
  (SQLite;atoms 表 + FTS5 trigram 索引,同 schema)。项目库存该项目事实;
  全局库存跨项目成立的记忆(用户偏好/通用工具规律),提炼时 LLM 标注 scope,
  未标注默认 project(保守不丢)
- 读链:inject(项目库 + 全局库两路检索,按分合并;池子低于阈值时零注入)

红线(TAM 验证):语义判断全 LLM(提炼/判重),脚本只做簿记;
LLM 失败一律 fallback store(宁重复不丢失);任何异常不得打断主流程。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from omni_core.local import llm_judge
from omni_core.local import runtime_paths as rp

_GATE = "runtime.knowledge.memory.enabled"
_INJECT_TRIGGER = 20      # 记忆池条数阈值:低于此零注入(设计稿 §2.1 触发条件)
_SEARCH_TOPK = 5
_SCORE_THRESHOLD = 0.3
_MAX_ATOMS_PER_FLUSH = 5
_FLUSH_MAX_CHARS = 8000   # 提炼原料(对话片段)截断
_FLUSH_MIN_ROLES = 2      # buffer 少于此条不触发提炼(无内容可提炼)

_lock = threading.RLock()
_buffers: Dict[str, List[Dict[str, str]]] = {}  # pid -> [{role, content}]


def enabled() -> bool:
    try:
        import config
        return bool(config.get_config(_GATE, False))
    except Exception:
        return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path(project_id: str):
    d = rp.project_dir(project_id) / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "index.db"


def _global_db_path():
    p = rp.global_memory_db()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _open_at(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.executescript("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS atoms(
      record_id TEXT PRIMARY KEY, content TEXT NOT NULL,
      kind TEXT DEFAULT 'fact', source_task TEXT DEFAULT '',
      version INTEGER NOT NULL DEFAULT 0,
      created TEXT NOT NULL, updated TEXT NOT NULL);
    """)
    conn.commit()
    return conn


def open_db(project_id: str) -> sqlite3.Connection:
    return _open_at(_db_path(project_id))


def open_global_db() -> sqlite3.Connection:
    """跨项目全局记忆库(与项目库同 schema;TAM「一个大库」语义的全局层)。"""
    return _open_at(_global_db_path())


_FTS_SCHEMA_VERSION = "1"  # FTS5 不支持 ALTER:换 tokenizer/schema 时改版本号 → drop 重建


def _fts_ok(conn: sqlite3.Connection) -> bool:
    """确保 trigram FTS 表存在且 schema 版本匹配;FTS5 不可用 → False(检索降级,不阻断)。"""
    try:
        ver = None
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='fts_schema_version'").fetchone()
            ver = row[0] if row else None
        except Exception:
            pass
        if ver != _FTS_SCHEMA_VERSION:
            conn.execute("DROP TABLE IF EXISTS atoms_fts")  # FTS5 无 ALTER,只能重建
            conn.execute(
                "CREATE VIRTUAL TABLE atoms_fts USING fts5("
                "content, record_id UNINDEXED, tokenize='trigram')")
            conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('fts_schema_version',?)",
                         (_FTS_SCHEMA_VERSION,))
            conn.commit()
        return True
    except Exception:
        return False


def _fts_sync(conn: sqlite3.Connection, record_id: str, content: str, delete_only: bool = False) -> None:
    """FTS 行与 atoms 行对齐(update/merge 先删旧行再写新)。"""
    if not _fts_ok(conn):
        return
    conn.execute("DELETE FROM atoms_fts WHERE record_id = ?", (record_id,))
    if not delete_only:
        conn.execute("INSERT INTO atoms_fts(content, record_id) VALUES(?, ?)", (content, record_id))


def count_atoms(project_id: str) -> int:
    try:
        conn = open_db(project_id)
        n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def count_global_atoms() -> int:
    try:
        conn = open_global_db()
        n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


# ---------------------------------------------------------------- 写链

def capture(project_id: str, role: str, content: str) -> None:
    """会话消息入 buffer(不提炼;提炼在任务收尾 flush 时批量做)。"""
    if not enabled() or not project_id:
        return
    with _lock:
        _buffers.setdefault(project_id, []).append({"role": role, "content": content})


def flush(project_id: str, brain_cfg: Optional[Dict[str, Any]] = None,
          source_task: str = "") -> Dict[str, int]:
    """任务收尾:buffer 里的对话片段 → LLM 提炼 atoms(带 scope)→ 三态判重 → 落库。

    scope=global 的 atom 入全局库(~/.omniagent/memory/index.db,跨项目可见),
    其余入项目库;判重在各自库内做。任何失败静默(返回各步计数);
    提炼/判重 LLM 失败 fallback store(宁重复不丢);全局库打不开只写项目库不阻断。
    """
    stats = {"extracted": 0, "stored": 0, "merged": 0, "skipped": 0}
    with _lock:
        msgs = _buffers.pop(project_id, [])
    if not enabled() or not project_id or len(msgs) < _FLUSH_MIN_ROLES:
        return stats
    digest = "\n".join(f"[{m['role']}] {m['content']}" for m in msgs)[-_FLUSH_MAX_CHARS:]
    # 原料质量观测：会话消息大量为空时，digest 没有可提炼的实质内容
    _contentful = sum(1 for m in msgs if len((m.get("content") or "").strip()) >= 10)
    atoms, _note = _extract_detail(digest, brain_cfg)
    if not atoms and _contentful < 2:
        _note = f"原料薄(有效消息 {_contentful} 条)；" + _note
    atoms = atoms[:_MAX_ATOMS_PER_FLUSH]
    stats["extracted"] = len(atoms)
    if not atoms:
        stats["note"] = _note
        # 白卷不影响 L2：场景块维护吃的是会话摘要+世界状态，与 L1 提炼独立
        _maybe_maintain_scene(project_id, brain_cfg, digest, task_id=source_task, stats=stats)
        return stats
    try:
        conn = open_db(project_id)
        ok_p = _fts_ok(conn)
        gconn = None
        try:
            gconn = open_global_db()
            ok_g = _fts_ok(gconn)
        except Exception:
            gconn = None
        for atom in atoms:
            text = atom["text"]
            is_global = atom["scope"] == "global" and gconn is not None
            c = gconn if is_global else conn
            ok = ok_g if is_global else ok_p
            decision = _decide(c, text, brain_cfg, ok)
            rid = uuid.uuid4().hex[:16]
            now = _now()
            if decision["action"] == "duplicate":
                stats["skipped"] += 1
                continue
            if decision["action"] == "merge" and decision.get("target_id"):
                tid = decision["target_id"]
                row = c.execute("SELECT version FROM atoms WHERE record_id=?", (tid,)).fetchone()
                if row:
                    c.execute(
                        "UPDATE atoms SET content=?, version=?, updated=?, source_task=? WHERE record_id=?",
                        (decision["merged_content"], row[0] + 1, now, source_task, tid))
                    if ok:
                        _fts_sync(c, tid, decision["merged_content"])
                    stats["merged"] += 1
                    continue
            c.execute(
                "INSERT INTO atoms(record_id, content, kind, source_task, version, created, updated) "
                "VALUES(?,?,?,?,0,?,?)", (rid, text, "fact", source_task, now, now))
            if ok:
                _fts_sync(c, rid, text)
            stats["stored"] += 1
        conn.commit()
        conn.close()
        if gconn is not None:
            gconn.commit()
            gconn.close()
    except Exception:
        pass
    _maybe_maintain_profile(project_id, brain_cfg)
    _maybe_maintain_scene(project_id, brain_cfg, digest, task_id=source_task, stats=stats)
    return stats


_EXTRACT_SYSTEM = (
    "你是记忆提炼器。从对话片段提取关于环境规律、用户长期偏好、工具/系统行为的陈述句事实。"
    "每条事实标注 scope:关于用户本人偏好/跨项目通用的工具或环境规律 → \"global\";"
    "只在当前项目里成立的规律 → \"project\"。"
    "规则:1) 1-5 条,每条一句陈述 ≤60 字;2) 只写会跨会话仍然成立的内容,"
    "一次性的操作过程/闲聊/纯主观感受不写;3) 观察不到规律就交白卷(facts 为空数组)"
    "——交白卷是正确答案,凑数写出的假规律是错误答案。"
    '只返回 JSON:{"facts": [{"text": "...", "scope": "global|project"}]}'
)

_DEDUP_SYSTEM = (
    "你是记忆判重器。给你「新事实」列表和「已有记忆」候选列表(带编号)。"
    "对每条新事实判定:与某条已有记忆表达同一事实 → duplicate(返回 target_id);"
    "与某条互补可合并为一条更完整的 → merge(返回 target_id 和 merged_content,"
    "合并句不得丢失任一方关键信息,≤80 字);是新信息 → store。"
    '只返回 JSON:{"decisions": [{"idx": 新事实序号, "action": "store|duplicate|merge",'
    ' "target_id": "...", "merged_content": "..."}]}'
)


def _extract(digest: str, brain_cfg) -> List[Dict[str, str]]:
    """提炼 → [{text, scope}];LLM 返回裸字符串或未标 scope 一律按 project(保守不丢)。"""
    return _extract_detail(digest, brain_cfg)[0]


def _extract_detail(digest: str, brain_cfg) -> tuple:
    """提炼 → ({text, scope} 列表, 原因说明);空列表时原因写明白卷还是失败。"""
    try:
        # timeout=60s：P2-R §9.5 真机定参——reasoning 模型提炼思考常超 30s，
        # 默认 10s 会随机超时→假白卷/假失败（2026-10-05 真机复现并修复）
        data = llm_judge.chat_json(brain_cfg, _EXTRACT_SYSTEM, digest, timeout=60.0)
        if data is None:
            return [], "提炼失败(无模型/请求失败/解析失败)"
        facts = data.get("facts") or []
        out = []
        for f in facts:
            if isinstance(f, dict):
                text = str(f.get("text") or "").strip()
                if not text:
                    continue
                scope = "global" if str(f.get("scope") or "").strip().lower() == "global" else "project"
                out.append({"text": text[:200], "scope": scope})
            else:
                s = str(f).strip()
                if s:
                    out.append({"text": s[:200], "scope": "project"})
        if not out:
            return [], "白卷(模型判无可沉淀的跨会话事实)"
        return out, ""
    except Exception:
        return [], "提炼失败(异常)"


# ---------------------------------------------------------------- L2 场景块(TAM scene_blocks,执行器在 scene_executor.py)

_L2_MIN_INTERVAL_S = 900    # 最小维护间隔（对齐 TAM l2MinIntervalSeconds）
_PERSONA_SIGNAL_KEY = "persona_update_request"  # L2→L3 带外信号水位(场景 agent → 画像强制维护)


def _maybe_maintain_scene(project_id: str, brain_cfg, digest: str,
                          task_id: str = "", stats: Optional[Dict[str, Any]] = None) -> None:
    """L2 场景块维护薄壳：gate + 900s 水位 + 证据组装,机制全在 scene_executor。

    - 触发：任务收尾（flush 尾部）；最小间隔 900s（项目库 meta 水位 last_scene_ts）
    - 机制：多文件主题块 + 带工具 agent 循环 + 容量治理/软删/归一化/索引
      （TAM scene-extractor 完整移植,见 scene_executor.maintain_scenes）
    - 带外信号：agent 请求画像重大更新时,落 meta 水位,下次 flush 强制画像维护
    - 治理：LLM 失败/空证据 → 静默不动;异常绝不阻断主流程
    """
    if stats is None:
        stats = {}
    try:
        if not enabled() or not project_id or not brain_cfg:
            return
        conn = open_db(project_id)
        last = None
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='last_scene_ts'").fetchone()
            last = row[0] if row else None
        except Exception:
            pass
        now = datetime.now(timezone.utc)
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < _L2_MIN_INTERVAL_S:
                    conn.close()
                    return
            except Exception:
                pass
        conn.close()

        evidence = []
        d = (digest or "").strip()
        if d:
            evidence.append("[会话要点]\n" + d[:3000])
        if task_id:
            try:
                wm = rp.task_world_model(task_id)
                if wm.exists():
                    wtxt = wm.read_text(encoding="utf-8").strip()
                    if wtxt:
                        evidence.append("[世界状态]\n" + wtxt[:2500])
            except Exception:
                pass

        from omni_core.local import scene_executor
        scene_executor.maintain_scenes(project_id, brain_cfg, "\n\n".join(evidence),
                                       task_id=task_id, stats=stats)

        # 带外信号消费（L2→L3）：agent 请求画像更新 → 落水位,下次 flush 强制画像维护
        signal = str(stats.get("persona_update_request") or "").strip()
        if signal:
            try:
                c2 = open_db(project_id)
                c2.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                           (_PERSONA_SIGNAL_KEY, signal))
                c2.commit()
                c2.close()
            except Exception:
                pass
        # 水位（有实际维护动作才推进,空维护不烧间隔——对齐 TAM checkpoint 语义）
        if not stats.get("empty_maintenance"):
            try:
                c3 = open_db(project_id)
                c3.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_scene_ts',?)", (_now(),))
                c3.commit()
                c3.close()
            except Exception:
                pass
    except Exception:
        pass


def _decide(conn: sqlite3.Connection, atom: str, brain_cfg, fts_ok: bool) -> Dict[str, Any]:
    """单条 atom 的三态判重;候选召回失败/LLM 失败 → store(宁重复不丢失)。"""
    if not fts_ok or not brain_cfg:
        return {"action": "store"}
    try:
        rows = conn.execute(
            "SELECT record_id, content FROM atoms_fts WHERE atoms_fts MATCH ? LIMIT 8",
            (atom,)).fetchall()
        if not rows:
            return {"action": "store"}
        cands = "\n".join(f"[{rid}] {txt}" for rid, txt in rows)
        data = llm_judge.chat_json(brain_cfg, _DEDUP_SYSTEM,
                                   f"新事实:{atom}\n\n已有记忆候选:\n{cands}", timeout=60.0)
        for d in (data or {}).get("decisions") or []:
            if str(d.get("action")) in ("duplicate", "merge") and d.get("target_id"):
                out = {"action": d["action"], "target_id": d["target_id"],
                       "merged_content": str(d.get("merged_content") or atom)[:200]}
                return out
        return {"action": "store"}
    except Exception:
        return {"action": "store"}


# ---------------------------------------------------------------- 读链

def search(project_id: str, query: str, k: int = _SEARCH_TOPK) -> List[Dict[str, Any]]:
    """项目库 FTS5 trigram 检索;S1(纯 FTS)下 MATCH 即过滤、不做绝对分门槛
    (trigram 单命中 bm25 量级 ~1e-6,0.3 门槛是 S2 混合检索的参数);任何异常返回 []。"""
    query = (query or "").strip()
    if not query:
        return []
    try:
        conn = open_db(project_id)
        try:
            return _search_conn(conn, query, k)
        finally:
            conn.close()
    except Exception:
        return []


def search_global(query: str, k: int = _SEARCH_TOPK) -> List[Dict[str, Any]]:
    """全局库检索(跨项目);任何异常返回 [](降级为仅项目库,不阻断注入)。"""
    query = (query or "").strip()
    if not query:
        return []
    try:
        conn = open_global_db()
        try:
            return _search_conn(conn, query, k)
        finally:
            conn.close()
    except Exception:
        return []


def _search_conn(conn: sqlite3.Connection, query: str, k: int) -> List[Dict[str, Any]]:
    if not _fts_ok(conn):
        return []
    rows = conn.execute(
        "SELECT record_id, content, bm25(atoms_fts) AS r FROM atoms_fts "
        "WHERE atoms_fts MATCH ? ORDER BY r LIMIT ?", (query, max(k * 3, 15))).fetchall()
    out = []
    for rid, content, rank in rows:
        rel = -float(rank)
        score = rel / (1.0 + rel) if rel > 0 else 0.0
        out.append({"record_id": rid, "content": content, "score": round(score, 6)})
    return out[:k]


def inject_text(project_id: str, query: str) -> str:
    """注入段:项目库 + 全局库两路检索按分合并;池子(两库合计)低于阈值零注入。"""
    if not enabled() or not project_id:
        return ""
    try:
        if count_atoms(project_id) + count_global_atoms() < _INJECT_TRIGGER:
            return ""
        hits = search(project_id, query) + search_global(query)
        hits.sort(key=lambda h: h["score"], reverse=True)
        hits = hits[:_SEARCH_TOPK]
        if not hits:
            return ""
        lines = "\n".join(f"- {h['content']}" for h in hits)
        return ("<relevant-memories>以下是按当前任务检索到的相关记忆,仅作参考,不代表当前任务进程;\n"
                "与实际观测不一致处以观测为准。\n" + lines + "</relevant-memories>")
    except Exception:
        return ""


# ---------------------------------------------------------------- 画像自动维护

_PROFILE_AUTO = "knowledge.profile.auto_maintain"     # 默认开(用户定案:画像应自动化)
_PROFILE_TRIGGER = "knowledge.profile.trigger_every_n"  # 每 N 条新 atoms 触发一次重写
_PROFILE_MAX_CHARS = 2000

_PROFILE_SYSTEM = (
    "你是用户画像维护器。给你「当前用户画像」和「最近沉淀的新事实」。"
    "任务:输出更新后的完整用户画像——保留仍然成立的内容,把新事实中关于"
    "用户本人/用户长期偏好/用户环境的部分合并进去,剔除已过时或与用户无关的条目;"
    "矛盾的以新事实为准。不要虚构,没有可合并的新信息就原样返回。"
    "只返回 JSON:{\"profile\": \"<完整画像全文,≤2000字符>\"}"
)


def _maybe_maintain_profile(project_id: str, brain_cfg) -> None:
    """画像自动维护(TAM L3 思路):新 atoms 攒够 N 条 → LLM 增量重写 user_profile.md。

    - gate:``knowledge.profile.auto_maintain``(默认开);人工编辑(PUT /profile)
      与自动维护共存——重写以"保留仍成立内容"为约束,人工内容不会被无故抹掉;
    - 触发:上次生成之后新增的 atoms 数 ≥ trigger_every_n(缺省 20);
      **或**场景 agent 的带外信号(``persona_update_request`` 水位,见 _maybe_maintain_scene)
      强制触发一次——场景整理 agent 视野最完整,它请求的画像更新不应等 atom 攒批;
    - 旧画像先备份 user_profile.md.bak;LLM 失败/输出异常 → 不动现文件。
    """
    try:
        import config
        if not bool(config.get_config(_PROFILE_AUTO, True)):
            return
        trigger = int(config.get_config(_PROFILE_TRIGGER, 20) or 20)
        conn = open_db(project_id)
        last_ts = None
        force_reason = ""
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='last_profile_ts'").fetchone()
            last_ts = row[0] if row else None
            srow = conn.execute("SELECT v FROM meta WHERE k=?",
                                (_PERSONA_SIGNAL_KEY,)).fetchone()
            force_reason = str(srow[0] or "").strip() if srow else ""
        except Exception:
            pass
        if last_ts:
            n = conn.execute("SELECT COUNT(*) FROM atoms WHERE updated > ?", (last_ts,)).fetchone()[0]
        else:
            n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        if not force_reason and n < trigger:
            conn.close()
            return
        new_rows = conn.execute(
            "SELECT content FROM atoms WHERE updated > ? ORDER BY updated DESC LIMIT 40",
            (last_ts or "",)).fetchall()
        conn.close()
        pp = rp.user_profile()
        old_profile = pp.read_text(encoding="utf-8") if pp.exists() else ""
        new_facts = "\n".join(f"- {c}" for (c,) in new_rows)
        if force_reason:
            new_facts = f"[场景整理请求画像更新(优先处理)]\n{force_reason}\n\n{new_facts}"
        data = llm_judge.chat_json(
            brain_cfg, _PROFILE_SYSTEM,
            f"当前用户画像:\n{old_profile or '(空)'}\n\n最近新事实:\n{new_facts}")
        # 信号消费点:维护已尝试(成败均算),清水位防永久绕过触发节流
        if force_reason:
            try:
                c3 = open_db(project_id)
                c3.execute("DELETE FROM meta WHERE k=?", (_PERSONA_SIGNAL_KEY,))
                c3.commit()
                c3.close()
            except Exception:
                pass
        text = str((data or {}).get("profile") or "").strip()
        if not text:
            return
        pp.parent.mkdir(parents=True, exist_ok=True)
        if pp.exists():
            pp.replace(pp.with_suffix(".md.bak"))  # 上一版画像 → .bak
        pp.write_text(text[:_PROFILE_MAX_CHARS], encoding="utf-8")
        conn2 = open_db(project_id)
        conn2.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_profile_ts',?)", (_now(),))
        conn2.commit()
        conn2.close()
    except Exception:
        pass


def _dump_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT record_id, content, kind, source_task, version, updated FROM atoms "
        "ORDER BY updated DESC").fetchall()
    return [{"record_id": r, "content": c, "kind": k,
             "source_task": st, "version": v, "updated": u}
            for r, c, k, st, v, u in rows]


def dump_state(project_id: str) -> Dict[str, Any]:
    """管理面/调试:导出项目库 + 全局库全部 atoms(人类可读)。"""
    try:
        conn = open_db(project_id)
        try:
            atoms = _dump_rows(conn)
        finally:
            conn.close()
        gatoms: List[Dict[str, Any]] = []
        try:
            gconn = open_global_db()
            try:
                gatoms = _dump_rows(gconn)
            finally:
                gconn.close()
        except Exception:
            pass
        return {"atoms": atoms, "global_atoms": gatoms}
    except Exception:
        return {"atoms": [], "global_atoms": []}
