"""Debug 模块 · 记忆全景 / 监控指标的**只读**聚合层（与正式路由零耦合）。

删除方式：与 ``debug_api.py`` 一起删除（同属可整体删除的 Debug 模块）。

只读来源：
- ``brain.db``：atoms / atoms_fts / l0_messages / l0_fts / narrative_index /
  narrative_fts / circle_note / context_buffer / meta；
- ``~/.omniagent`` 下的正文文件（日记 / 月记 / 关系卡 / 场景块 / 画像 / 角色卡 /
  AGENTS.md / 会话 jsonl / 观测 jsonl）；
- ``.codebuddy/tmp/memory-effect-report.json``（离线效果报告，存在才读）。

纪律：不写任何记忆、不触发巩固、不调 LLM、不进注入面。观测记录是量尺不是记忆，
这里只向本机呈现聚合值，不给 agent 提供任何查询工具。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.local import runtime_paths as rp

#: 单次读取正文的上限（防止会话 jsonl / 长日记把调试面板撑爆）
MAX_TEXT = 60_000
_ROOT = Path(__file__).resolve().parents[3]
_EFFECT_REPORT = _ROOT / ".codebuddy" / "tmp" / "memory-effect-report.json"

_DOC_KINDS = ("diary", "month", "relation", "scene", "profile", "character",
              "agents", "session", "observation")


# ------------------------------------------------------------------ 通用小工具

def _count(conn, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
    except Exception:
        return 0


def _rows(conn, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """动态列名取行：atoms / l0 有随版本追加的列，不写死列清单才不会漏字段。"""
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description or []]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def _file_meta(p: Path, layer: str, kind: str, name: str, key: str) -> Dict[str, Any]:
    try:
        st = p.stat()
        chars = st.st_size
        updated = _iso(st.st_mtime)
    except Exception:
        chars, updated = 0, ""
    return {"layer": layer, "kind": kind, "name": name, "key": key,
            "chars": chars, "bytes": chars, "updated": updated}


def _text_of(p: Path, limit: int = MAX_TEXT) -> Dict[str, Any]:
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "text": raw[:limit], "chars": len(raw),
            "truncated": len(raw) > limit}


def _child(root: Path, name: str) -> Optional[Path]:
    """把用户给的名字钉死在 root 直接子级（拒绝 .. / 绝对路径 / 子目录逃逸）。"""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    try:
        parent = root.resolve()
        cand = (parent / name).resolve()
        return cand if cand.parent == parent else None
    except Exception:
        return None


def _mem_dir(mb) -> Path:
    return Path(str(mb._BRAIN_PATH)).parent


# ------------------------------------------------------------------ 文档清单 / 正文

def list_documents(mb) -> List[Dict[str, Any]]:
    """所有可读记忆正文（文件是唯一真相），每项带 ``key`` 供按需读全文。"""
    out: List[Dict[str, Any]] = []

    def add_dir(layer: str, kind: str, root: Path, pattern: str,
                skip_prefix: str = "") -> None:
        try:
            for p in sorted(root.glob(pattern), key=lambda x: x.name, reverse=True):
                if skip_prefix and p.name.startswith(skip_prefix):
                    continue
                if not p.is_file():
                    continue
                out.append(_file_meta(p, layer, kind, p.name, f"{kind}/{p.name}"))
        except Exception:
            pass

    def add_one(layer: str, kind: str, name: str, p: Path, key: str) -> None:
        if p.is_file():
            out.append(_file_meta(p, layer, kind, name, key))

    diary = rp.global_diary()
    add_dir("L2", "diary", diary, "diary-*.md", skip_prefix="diary-month-")
    add_dir("L2", "month", diary, "diary-month-*.md")
    add_dir("L2", "relation", rp.global_relations(), "*.md")

    try:
        for p in sorted(rp.projects_root().glob("*/memory/scene_blocks/*.md")):
            pid = p.parent.parent.parent.name
            out.append(_file_meta(p, "L2", "scene", f"{pid}/{p.name}",
                                  f"scene/{pid}/{p.name}"))
    except Exception:
        pass

    add_one("L3", "profile", "self_profile.md", rp.self_profile(), "profile/self_profile.md")
    add_one("L3", "character", "character.md", rp.character_card(), "character/character.md")
    add_one("L3", "agents", "AGENTS.md（全局）", rp.global_agents_file(), "agents/global")
    try:
        for p in sorted(rp.projects_root().glob("*/AGENTS.md")):
            pid = p.parent.name
            out.append(_file_meta(p, "L3", "agents", f"AGENTS.md（项目 {pid}）",
                                  f"agents/project/{pid}"))
        for p in sorted(rp.tasks_root().glob("*/AGENTS.md")):
            tid = p.parent.name
            out.append(_file_meta(p, "L3", "agents", f"AGENTS.md（任务 {tid}）",
                                  f"agents/task/{tid}"))
    except Exception:
        pass

    try:
        for p in sorted(rp.projects_root().glob("*/*.jsonl")):
            pid = p.parent.name
            out.append(_file_meta(p, "L0", "session", f"{pid}/{p.name}",
                                  f"session/{pid}/{p.name}"))
    except Exception:
        pass
    add_dir("监控", "observation", _mem_dir(mb), "memory_recall-*.jsonl")
    return out


def read_document(mb, key: str) -> Dict[str, Any]:
    """按白名单 key 读正文；没有任何「任意路径读文件」的口子。"""
    kind, _, rest = str(key or "").partition("/")
    if kind not in _DOC_KINDS:
        return {"ok": False, "error": "未知文档类型"}
    try:
        if kind in ("diary", "month", "relation", "observation"):
            root = (rp.global_diary() if kind in ("diary", "month")
                    else rp.global_relations() if kind == "relation" else _mem_dir(mb))
            p = _child(root, rest)
        elif kind == "scene":
            pid, _, fname = rest.partition("/")
            p = _child(rp.project_dir(pid) / "memory" / "scene_blocks", fname) if pid else None
        elif kind == "session":
            pid, _, fname = rest.partition("/")
            p = _child(rp.project_dir(pid), fname) if pid else None
        elif kind == "profile":
            # 固定文件也要校验 key 尾部，避免 ``profile/任意串`` 被当成合法别名
            p = rp.self_profile() if rest == rp.self_profile().name else None
        elif kind == "character":
            p = rp.character_card() if rest == rp.character_card().name else None
        elif kind == "agents":
            scope, _, ident = rest.partition("/")
            p = (rp.global_agents_file() if scope == "global" and not ident else
                 rp.project_dir(ident) / "AGENTS.md" if scope == "project" and ident else
                 rp.task_dir(ident) / "AGENTS.md" if scope == "task" and ident else None)
        else:
            p = None
    except Exception:
        p = None
    if p is None:
        return {"ok": False, "error": "非法文档路径"}
    if not p.is_file():
        return {"ok": False, "error": "文档不存在"}
    r = _text_of(p)
    r.update({"key": key, "name": p.name})
    return r


# ------------------------------------------------------------------ 分层列表

def atoms_page(mb, *, limit: int = 100, offset: int = 0, state: str = "",
               kind: str = "", q: str = "") -> Dict[str, Any]:
    """L1 全量分页（不再有 200 条硬上限）+ 状态/类别/词面过滤。"""
    limit = max(1, min(500, int(limit or 100)))
    offset = max(0, int(offset or 0))
    where, params = ["1=1"], []
    if state:
        where.append("state = ?")
        params.append(state)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if q:
        where.append("(content LIKE ? OR record_id LIKE ? OR source_task LIKE ?)")
        params += [f"%{q}%"] * 3
    clause = " AND ".join(where)
    conn = mb.open_brain()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) FROM atoms WHERE {clause}",
                                 tuple(params)).fetchone()[0] or 0)
        items = _rows(conn, f"SELECT * FROM atoms WHERE {clause} "
                            "ORDER BY updated DESC LIMIT ? OFFSET ?",
                      tuple(params + [limit, offset]))
    finally:
        conn.close()
    return {"ok": True, "total": total, "count": len(items), "limit": limit,
            "offset": offset, "items": items}


def l0_page(mb, *, limit: int = 100, offset: int = 0, q: str = "",
            context_key: str = "", source_kind: str = "") -> Dict[str, Any]:
    """L0 逐字检索副本（表是索引副本，真相在会话 jsonl 文件）。"""
    limit = max(1, min(500, int(limit or 100)))
    offset = max(0, int(offset or 0))
    where, params = ["1=1"], []
    if q:
        where.append("(text LIKE ? OR speaker LIKE ?)")
        params += [f"%{q}%"] * 2
    if context_key:
        where.append("(context_key = ? OR session_id = ?)")
        params += [context_key, context_key]
    if source_kind:
        where.append("source_kind = ?")
        params.append(source_kind)
    clause = " AND ".join(where)
    conn = mb.open_brain()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) FROM l0_messages WHERE {clause}",
                                 tuple(params)).fetchone()[0] or 0)
        items = _rows(conn, f"SELECT * FROM l0_messages WHERE {clause} "
                            "ORDER BY recorded_at DESC LIMIT ? OFFSET ?",
                      tuple(params + [limit, offset]))
    finally:
        conn.close()
    return {"ok": True, "total": total, "count": len(items), "limit": limit,
            "offset": offset, "items": items}


def narrative_page(mb, *, limit: int = 200, kind: str = "", q: str = "") -> Dict[str, Any]:
    """叙事索引全量条目（只给指针/摘要，不返回正文——正文按需 read_document）。"""
    limit = max(1, min(1000, int(limit or 200)))
    where, params = ["1=1"], []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if q:
        where.append("(doc_id LIKE ? OR summary LIKE ? OR topics LIKE ? OR peers LIKE ?)")
        params += [f"%{q}%"] * 4
    clause = " AND ".join(where)
    conn = mb.open_brain()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) FROM narrative_index WHERE {clause}",
                                 tuple(params)).fetchone()[0] or 0)
        items = _rows(conn, f"SELECT * FROM narrative_index WHERE {clause} "
                            "ORDER BY date_from DESC, doc_id DESC LIMIT ?",
                      tuple(params + [limit]))
    finally:
        conn.close()
    return {"ok": True, "total": total, "count": len(items), "items": items}


# ------------------------------------------------------------------ 全层总览

def landscape(mb) -> Dict[str, Any]:
    """一次拿到「所有记忆管理」的全景：各层存量、索引健康、水位、队列、正文清单。"""
    conn = mb.open_brain()
    try:
        by_kind = {str(r[0]): int(r[1]) for r in
                   conn.execute("SELECT kind, COUNT(*) FROM atoms GROUP BY kind")}
        by_state = {str(r[0]): int(r[1]) for r in
                    conn.execute("SELECT state, COUNT(*) FROM atoms GROUP BY state")}
        by_narr_kind = {str(r[0]): int(r[1]) for r in
                        conn.execute("SELECT kind, COUNT(*) FROM narrative_index GROUP BY kind")}
        meta = {str(r["k"]): str(r.get("v") or "") for r in
                _rows(conn, "SELECT k, v FROM meta") if r.get("k")}
        tables = sorted(str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"))
        l0_recent = _rows(conn, "SELECT record_id, context_key, session_id, kind, role, "
                                "speaker, text, recorded_at FROM l0_messages "
                                "ORDER BY recorded_at DESC LIMIT 20")
        circle = _rows(conn, "SELECT * FROM circle_note ORDER BY updated DESC LIMIT 50")
        buffers = _rows(conn, "SELECT * FROM context_buffer ORDER BY updated DESC LIMIT 50")
        atom_total = _count(conn, "atoms")
        l0_total = _count(conn, "l0_messages")
        narr_total = _count(conn, "narrative_index")
        db = {
            "total": atom_total,
            "active": by_state.get("active", 0),
            "by_kind": by_kind, "by_state": by_state,
            "atoms_fts": _count(conn, "atoms_fts"),
            "l0": l0_total, "l0_fts": _count(conn, "l0_fts"),
            "narrative": narr_total, "narrative_fts": _count(conn, "narrative_fts"),
            "by_narrative_kind": by_narr_kind,
            "circle_notes": _count(conn, "circle_note"),
            "context_buffers": _count(conn, "context_buffer"),
            "tables": tables, "meta": meta,
        }
    finally:
        conn.close()

    docs = list_documents(mb)
    doc_by_kind: Dict[str, int] = {}
    for d in docs:
        doc_by_kind[d["kind"]] = doc_by_kind.get(d["kind"], 0) + 1

    scenes: List[Dict[str, Any]] = []
    try:
        for idx in sorted(rp.projects_root().glob("*/memory/scene_index.json")):
            try:
                data = json.loads(idx.read_text(encoding="utf-8"))
            except Exception:
                continue
            pid = idx.parent.parent.name
            for e in (data.get("scenes") or [])[:40]:
                if isinstance(e, dict):
                    scenes.append({"project": pid, **e})
            scenes.append({"project": pid, "watermark_last_maintain": str(data.get("last_maintain_ts") or ""),
                           "watermark_last_atom": str(data.get("last_l2_atom_ts") or "")})
    except Exception:
        pass

    def _asset(p: Path) -> Dict[str, Any]:
        if not p.is_file():
            return {"exists": False, "chars": 0, "updated": "", "preview": ""}
        st = p.stat()
        try:
            preview = p.read_text(encoding="utf-8", errors="replace")[:240]
        except Exception:
            preview = ""
        return {"exists": True, "chars": st.st_size, "updated": _iso(st.st_mtime),
                "preview": preview}

    try:
        enabled = bool(mb.enabled())
    except Exception:
        enabled = False
    try:
        from omni_core import memory_writer
        writer = memory_writer.stats()
    except Exception:
        writer = {}
    try:
        db_bytes = Path(str(mb._BRAIN_PATH)).stat().st_size
    except Exception:
        db_bytes = 0

    return {
        "ok": True, "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "enabled": enabled,
        "brain": {"path": str(mb._BRAIN_PATH), "bytes": db_bytes, **db},
        "health": {
            "enabled": enabled,
            "atoms_fts_gap": atom_total - db["atoms_fts"],
            "l0_fts_gap": l0_total - db["l0_fts"],
            "narrative_fts_gap": narr_total - db["narrative_fts"],
            "last_consolidation": meta.get("last_consolidation", ""),
            "flush_count": meta.get("flush_count", ""),
        },
        "l0": {"count": l0_total, "recent": l0_recent,
               "session_files": doc_by_kind.get("session", 0)},
        "l2": {"narrative_index": narr_total, "by_kind": by_narr_kind,
               "scenes": scenes[:120], "documents": doc_by_kind},
        "l3": {"profile": _asset(rp.self_profile()),
               "character": _asset(rp.character_card()),
               "agents": _asset(rp.global_agents_file())},
        "context": {"count": db["circle_notes"], "notes": circle,
                    "buffers": buffers},
        "documents": docs[:400],
        "writer": writer,
    }


# ------------------------------------------------------------------ 监控指标

def _local_day(ts: str) -> str:
    """观测 ts 是 UTC ISO；按**本地日历日**归档（与叙事日期口径一致）。"""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().date().isoformat()
    except Exception:
        return str(ts)[:10]


def _observation_metrics(mb, days: int = 30) -> Dict[str, Any]:
    """聚合 ``memory_recall-YYYY-MM.jsonl``，只读不改。"""
    rows: List[Dict[str, Any]] = []
    try:
        for p in sorted(_mem_dir(mb).glob("memory_recall-*.jsonl")):
            with p.open(encoding="utf-8", errors="replace") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    if isinstance(r, dict):
                        rows.append(r)
    except Exception:
        pass
    today = datetime.now().astimezone().date()
    day_keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    daily = {d: {"runs": 0, "calls": 0, "hits": 0, "zero": 0,
                 "context_chars": 0, "missed": 0} for d in day_keys}
    by_tool: Dict[str, int] = {}
    calls = hits = zero = ctx_chars = ctx_injected = cross = external = llm_delta = 0
    evidence_runs = bundle_taken = quota_blocked = 0
    no_candidate = missed_candidate = fetched = 0
    last_ts = ""
    for r in rows:
        c = int(r.get("calls") or 0)
        ev = int(r.get("evidence") or 0)
        bt = int(r.get("bundle_taken") or 0)
        calls += c
        hits += int(r.get("hits") or 0)
        zero += 1 if r.get("zero_query") or c == 0 else 0
        ctx_chars += int(r.get("context_chars") or 0)
        ctx_injected += 1 if r.get("injected_context") else 0
        cross += int(r.get("narrative_cross") or 0)
        external += 1 if r.get("source_external") else 0
        llm_delta += int(r.get("llm_calls_delta") or 0)
        evidence_runs += 1 if ev > 0 else 0
        bundle_taken += bt
        quota_blocked += int(r.get("quota_blocked") or 0)
        missed = False
        if c > 0 or bt > 0:
            fetched += 1
        elif ev > 0:
            missed_candidate += 1
            missed = True
        else:
            no_candidate += 1
        for k, v in (r.get("by_tool") or {}).items():
            if isinstance(v, (int, float)):
                by_tool[str(k)] = by_tool.get(str(k), 0) + int(v)
        ts = str(r.get("ts") or "")
        last_ts = max(last_ts, ts)
        d = _local_day(ts)
        if d in daily:
            daily[d]["runs"] += 1
            daily[d]["calls"] += c
            daily[d]["hits"] += int(r.get("hits") or 0)
            daily[d]["zero"] += 1 if r.get("zero_query") or c == 0 else 0
            daily[d]["context_chars"] += int(r.get("context_chars") or 0)
            daily[d]["missed"] += 1 if missed else 0
    n = len(rows)
    return {
        "runs": n, "calls": calls, "hits": hits,
        "hit_rate": round(hits / calls, 3) if calls else None,
        "zero_runs": zero, "zero_rate": round(zero / n, 3) if n else None,
        "avg_context_chars": round(ctx_chars / n, 1) if n else None,
        "context_injected_runs": ctx_injected,
        "narrative_cross": cross, "external_runs": external,
        "llm_calls_delta": llm_delta,
        "evidence_runs": evidence_runs,
        "bundle_taken": bundle_taken, "quota_blocked": quota_blocked,
        "no_candidate": no_candidate, "missed_candidate": missed_candidate,
        "fetched": fetched,
        "missed_rate": round(missed_candidate / n, 3) if n else None,
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])),
        "daily": [{"date": d, **daily[d]} for d in day_keys],
        "last_ts": last_ts,
        "files": len(list(_mem_dir(mb).glob("memory_recall-*.jsonl"))) if n else 0,
        "note": "每 run 一行观测；zero_rate 是零自取率（非 0 召回率）；"
                "missed_candidate = 有高置信候选却一次都没取",
    }


def _telemetry_metrics() -> Dict[str, Any]:
    """聚合 tasks/*/reports/*.json 的四指标（M4a.2 telemetry）。"""
    files: List[Path] = []
    try:
        files = sorted(rp.tasks_root().glob("*/reports/*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:200]
    except Exception:
        pass
    keys = ("success_rate", "brain_intervention_rate", "tool_efficiency", "recovery_rate")
    sums = {k: 0.0 for k in keys}
    recent: List[Dict[str, Any]] = []
    ok = 0
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        m = d.get("metrics") or {}
        if not isinstance(m, dict) or not any(k in m for k in keys):
            continue
        ok += 1
        for k in keys:
            try:
                sums[k] += float(m.get(k) or 0)
            except Exception:
                pass
        if len(recent) < 12:
            recent.append({"run_id": d.get("run_id") or p.stem,
                           "success": d.get("success"), "steps": d.get("steps"),
                           "metrics": {k: m.get(k) for k in keys}})
    return {"runs": ok,
            "averages": {k: round(sums[k] / ok, 3) for k in keys} if ok else {},
            "recent": recent}


def _effect_report() -> Dict[str, Any]:
    if not _EFFECT_REPORT.is_file():
        return {"available": False, "note": "尚未运行 scripts/verify_memory_effect.py"}
    try:
        d = json.loads(_EFFECT_REPORT.read_text(encoding="utf-8"))
    except Exception as e:
        return {"available": False, "error": str(e)}
    return {"available": True, "generated": d.get("generated"),
            "l1": d.get("l1") or {}, "background": d.get("background") or {},
            "narrative": d.get("narrative") or {},
            "narrative_self_fetch": d.get("narrative_self_fetch") or {},
            "p0_regression": d.get("p0_regression") or {},
            "silent_cost": d.get("silent_cost") or {},
            "pending_human": d.get("pending_human") or {}}


def metrics(mb) -> Dict[str, Any]:
    """监控指标总览：观测（在线真值）+ 离线效果报告 + 运行轨迹 + 写入队列。"""
    try:
        from omni_core import memory_writer
        writer = memory_writer.stats()
    except Exception:
        writer = {}
    conn = mb.open_brain()
    try:
        active = int(conn.execute("SELECT COUNT(*) FROM atoms WHERE state='active'")
                     .fetchone()[0] or 0)
        tomb = int(conn.execute("SELECT COUNT(*) FROM atoms WHERE state != 'active'")
                   .fetchone()[0] or 0)
        salience = conn.execute("SELECT AVG(salience), MAX(salience) FROM atoms "
                                "WHERE state='active'").fetchone()
        access = conn.execute("SELECT SUM(access_count), MAX(access_count) FROM atoms").fetchone()
        meta = {str(r[0]): str(r[1] or "") for r in conn.execute("SELECT k, v FROM meta")}
    finally:
        conn.close()
    return {
        "ok": True, "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "observation": _observation_metrics(mb),
        "effect_report": _effect_report(),
        "telemetry": _telemetry_metrics(),
        "writer": writer,
        "store": {"active_atoms": active, "inactive_atoms": tomb,
                  "avg_salience": round(float(salience[0] or 0), 3) if salience else 0,
                  "max_salience": round(float(salience[1] or 0), 3) if salience else 0,
                  "total_access": int(access[0] or 0) if access else 0,
                  "max_access": int(access[1] or 0) if access else 0,
                  "last_consolidation": meta.get("last_consolidation", ""),
                  "flush_count": meta.get("flush_count", "")},
    }
