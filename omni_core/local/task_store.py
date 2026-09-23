"""Task / Project 仓储（方案 C）。

- Task 是独立平铺的一等实体，落 ``~/.omniagent/tasks/<task_id>/``。
- Project 只是工作目录的 slug 分组，仅承载会话历史，无独立 CRUD
  （slug 即 id，目录按需创建）。
- 资产（trajectory / world_model / collected / skills）跟 task 走，由
  runtime_paths 提供的路径函数落地；本模块只管 task 元信息、子任务板与会话历史
  （M6：``SubtaskStore`` 是多 agent 共享的领取/状态表，见
  doc/plans/multi-agent-redesign-2026-09-13.md §4）。

红线：不出现任何场景 / 业务词；所有路径经由 runtime_paths。
"""
from __future__ import annotations

import json
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.local import runtime_paths as P


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    return "t_" + uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------
class TaskStore:
    """Task 元信息管理 + 平铺列表。资产文件由调用方经 runtime_paths 写入。"""

    _lock = threading.RLock()

    @staticmethod
    def create(
        objective: str,
        done_when: str = "",
        project_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """创建服务端生成 ID 的任务元数据。

        Args:
            objective: 用户目标。
            done_when: 可选完成条件。
            project_id: 会话归属；缺省时自动创建项目归属。
            session_id: 会话标识；缺省时生成与任务绑定的会话标识。

        Returns:
            已落盘的任务元数据。
        """
        with TaskStore._lock:
            tid = _new_task_id()
            pid = P.validate_identifier(project_id, "project_id") if project_id else P.auto_project_id()
            sid = P.validate_identifier(session_id, "session_id") if session_id else "s_" + uuid.uuid4().hex[:12]
            P.ensure_task_dirs(tid)
            ProjectStore.ensure(pid)
            meta = {
                "task_id": tid,
                "project_id": pid,
                "session_id": sid,
                "objective": objective,
                "done_when": done_when,
                "state": "pending",
                "runs": [],
                "created_at": _now_iso(),
                "finished_at": None,
                "success": None,
                "full_access": False,
            }
            TaskStore._write(tid, meta)
            return meta

    @staticmethod
    def get(task_id: str) -> Optional[Dict[str, Any]]:
        """读取单个任务元数据。"""
        with TaskStore._lock:
            p = P.task_json(task_id)
            if not p.exists():
                return None
            return json.loads(p.read_text(encoding="utf-8"))

    @staticmethod
    def list(state: Optional[str] = None) -> List[Dict[str, Any]]:
        root = P.tasks_root()
        if not root.exists():
            return []
        out: List[Dict[str, Any]] = []
        for d in sorted(root.iterdir(), key=lambda x: x.name):
            pj = d / "task.json"
            if not pj.exists():
                continue
            try:
                meta = json.loads(pj.read_text(encoding="utf-8"))
            except Exception:
                continue
            if state and meta.get("state") != state:
                continue
            out.append(meta)
        # 按创建时间倒序（同毫秒用 task_id 兜底，保证稳定）
        out.sort(key=lambda m: (m.get("created_at", ""), m.get("task_id", "")), reverse=True)
        return out

    @staticmethod
    def update(task_id: str, **fields) -> Dict[str, Any]:
        """更新白名单内的任务元数据。"""
        allowed = {"objective", "done_when", "state", "success", "runs", "finished_at", "project_id", "session_id", "full_access"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新 task 字段: {', '.join(sorted(unknown))}")
        with TaskStore._lock:
            meta = TaskStore.get(task_id)
            if meta is None:
                raise KeyError(f"task not found: {task_id}")
            if "project_id" in fields and fields["project_id"]:
                fields["project_id"] = P.validate_identifier(fields["project_id"], "project_id")
            if "session_id" in fields and fields["session_id"]:
                fields["session_id"] = P.validate_identifier(fields["session_id"], "session_id")
            meta.update(fields)
            if "state" in fields and fields["state"] in ("done", "failed", "aborted"):
                if not meta.get("finished_at"):
                    meta["finished_at"] = _now_iso()
            TaskStore._write(task_id, meta)
            return meta

    @staticmethod
    def remove(task_id: str) -> None:
        """删除 task 索引文件（task.json）。workspace 目录由调用方负责清理。"""
        with TaskStore._lock:
            p = P.task_json(task_id)
            if p.exists():
                p.unlink()

    @staticmethod
    def add_run(task_id: str, run_id: str) -> Dict[str, Any]:
        with TaskStore._lock:
            meta = TaskStore.get(task_id)
            if meta is None:
                raise KeyError(f"task not found: {task_id}")
            if run_id not in meta["runs"]:
                meta["runs"].append(run_id)
            TaskStore._write(task_id, meta)
            return meta

    @staticmethod
    def _write(task_id: str, meta: Dict[str, Any]) -> None:
        """原子写入任务元数据，避免进程中断留下半截 JSON。"""
        path = P.task_json(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


# --------------------------------------------------------------------------
# M6：子任务板（多 agent 共享的领取 / 状态表）
# --------------------------------------------------------------------------
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"


class SubtaskStore:
    """某 task 下的子任务板，落 ``tasks/<task_id>/subtasks.json``。

    多 agent 共享语义：
    - 状态机 ``pending → running → done | failed | skipped``；
    - :meth:`claim` / :meth:`claim_next` 在锁内做 CAS（只有 pending 才能被领取），
      保证同一子任务不会被两个 agent 同时拿到；
    - 写入走原子写（tmp + replace），进程中断不留下半截 JSON。

    内核零领域假设：条目只含 desc / done_when 等通用字段。
    """

    _lock = threading.RLock()

    @staticmethod
    def _path(task_id: str) -> Path:
        return P.task_subtasks(task_id)

    @staticmethod
    def _read(task_id: str) -> List[Dict[str, Any]]:
        with SubtaskStore._lock:
            p = SubtaskStore._path(task_id)
            if not p.exists():
                return []
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return []
            return data if isinstance(data, list) else []

    @staticmethod
    def _write(task_id: str, items: List[Dict[str, Any]]) -> None:
        p = SubtaskStore._path(task_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    @staticmethod
    def add(task_id: str, desc: str, done_when: str = "") -> Dict[str, Any]:
        """新增一条 pending 子任务，返回该条目。"""
        with SubtaskStore._lock:
            items = SubtaskStore._read(task_id)
            entry = {
                "id": "st_" + uuid.uuid4().hex[:8],
                "desc": desc,
                "done_when": done_when,
                "state": PENDING,
                "owner_agent_id": "",
                "attempts": 0,
                "result_ref": "",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
            items.append(entry)
            SubtaskStore._write(task_id, items)
            return entry

    @staticmethod
    def add_many(task_id: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量新增（desc/done_when），返回条目列表。"""
        out = []
        for it in items or []:
            if isinstance(it, str):
                it = {"desc": it}
            out.append(SubtaskStore.add(
                task_id,
                str(it.get("desc", "")),
                str(it.get("done_when", "") or ""),
            ))
        return out

    @staticmethod
    def claim(task_id: str, subtask_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        """领取指定子任务（CAS：仅 pending 可领）。已被领取返回 None。"""
        with SubtaskStore._lock:
            items = SubtaskStore._read(task_id)
            for it in items:
                if it.get("id") == subtask_id:
                    if it.get("state") != PENDING:
                        return None
                    it["state"] = RUNNING
                    it["owner_agent_id"] = agent_id
                    it["attempts"] = int(it.get("attempts", 0)) + 1
                    it["updated_at"] = _now_iso()
                    SubtaskStore._write(task_id, items)
                    return it
            return None

    @staticmethod
    def claim_next(task_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
        """原子领取下一条 pending 子任务；没有可领的返回 None。"""
        with SubtaskStore._lock:
            items = SubtaskStore._read(task_id)
            for it in items:
                if it.get("state") == PENDING:
                    return SubtaskStore.claim(task_id, it.get("id", ""), agent_id)
            return None

    @staticmethod
    def finish(task_id: str, subtask_id: str, success: bool = True,
               result_ref: str = "") -> Optional[Dict[str, Any]]:
        """结束子任务：done / failed。"""
        with SubtaskStore._lock:
            items = SubtaskStore._read(task_id)
            for it in items:
                if it.get("id") == subtask_id:
                    it["state"] = DONE if success else FAILED
                    it["result_ref"] = result_ref
                    it["updated_at"] = _now_iso()
                    SubtaskStore._write(task_id, items)
                    return it
            return None

    @staticmethod
    def release(task_id: str, subtask_id: str) -> Optional[Dict[str, Any]]:
        """把 running 退回 pending（可重试）。"""
        with SubtaskStore._lock:
            items = SubtaskStore._read(task_id)
            for it in items:
                if it.get("id") == subtask_id:
                    if it.get("state") != RUNNING:
                        return None
                    it["state"] = PENDING
                    it["owner_agent_id"] = ""
                    it["updated_at"] = _now_iso()
                    SubtaskStore._write(task_id, items)
                    return it
            return None

    @staticmethod
    def list(task_id: str, state: Optional[str] = None) -> List[Dict[str, Any]]:
        items = SubtaskStore._read(task_id)
        if state:
            items = [i for i in items if i.get("state") == state]
        return items

    @staticmethod
    def clear(task_id: str) -> None:
        with SubtaskStore._lock:
            SubtaskStore._write(task_id, [])


# --------------------------------------------------------------------------
# Project（会话历史）
# --------------------------------------------------------------------------
class ProjectStore:
    """Project 仅承载会话历史 jsonl。slug 即 id，按需建目录。"""

    _lock = threading.RLock()

    @staticmethod
    def ensure(project_id: str) -> Path:
        with ProjectStore._lock:
            d = P.project_dir(project_id)
            d.mkdir(parents=True, exist_ok=True)
            return d

    @staticmethod
    def list() -> List[Dict[str, Any]]:
        root = P.projects_root()
        if not root.exists():
            return []
        out = []
        for d in sorted(root.iterdir(), key=lambda x: x.name):
            if not d.is_dir():
                continue
            sessions = sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True)
            out.append({
                "id": d.name,
                "session_count": len(sessions),
                "last_used_at": _iso_from_mtime(sessions[0]) if sessions else None,
            })
        out.sort(key=lambda m: m.get("last_used_at") or "", reverse=True)
        return out

    @staticmethod
    def list_sessions(project_id: str) -> List[str]:
        d = P.project_dir(project_id)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.jsonl"))

    @staticmethod
    def append_message(
        project_id: str,
        session_id: str,
        role: str,
        content: str,
        task_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        with ProjectStore._lock:
            d = ProjectStore.ensure(project_id)
            path = P.session_file(project_id, session_id)
            rec = {"role": role, "content": content, "ts": _now_iso()}
            if task_id:
                rec["task_id"] = P.validate_identifier(task_id, "task_id")
            # extra：结构化 agent turn（执行步骤 steps + 机器状态 meta），
            # 既供前端刷新后恢复完整过程流，也供下一轮模型上下文回填（根治跨轮失忆）。
            if extra:
                rec["extra"] = extra
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def read_session(project_id: str, session_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        path = P.session_file(project_id, session_id)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        msgs = [json.loads(ln) for ln in lines if ln.strip()]
        if limit:
            msgs = msgs[-limit:]
        return msgs


def _iso_from_mtime(p: Path) -> str:
    import time
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
