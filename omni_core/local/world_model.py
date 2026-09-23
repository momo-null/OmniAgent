"""世界模型（M4b.1：内存 + 磁盘持久化 + checkpoint）。

去场景化（§9）：世界状态不绑定任何感知形状（屏幕/结构化/终端…）。
内部只持有 backend 产出的原始 percept + 其文本化结果 state_text，
内核（tool_loop / summary / snapshot）只消费 state_text，永不读取感知字段名。

职责：
- 保存 backend 产出的当前 percept（current_percept）+ 文本化快照（state_text）
- 维护最近 K 步动作结果的 ring buffer
- 积累已知事实（facts，供大脑规划/反思注入）
- 磁盘持久化：``tasks/<task_id>/world_model.md``
- 子目标 checkpoint：``tasks/<task_id>/checkpoints/<run_id>/<subgoal>.json``（崩溃 resume）
- merge_progress：压缩时关键进展写回避 JPEG 效应

无状态大脑依赖本模块：_plan / _reflect 只组装 world 快照 + 当前子目标 / 升级原因，
不累积长历史。

M6 共享黑板（设计 doc/plans/multi-agent-redesign-2026-09-13.md §3）：
- ``WorldModel.shared(task_id)`` 让同一 task 下的所有 agent 拿同一个实例；
- 写入受 RLock 保护，可并发；
- ``add_fact(text, source=...)`` 记录来源，``view(scope)`` 按来源合成视图，
  使每个 agent 只看「全局事实 + 自己分支的事实」；
- 落盘改为原子写（tmp + replace），并发下不会互相覆盖半截文件。

未登记实例（task_id 为空 或直接 ``WorldModel(...)`` 构造）行为与 M6 前一致。
"""
import json
import os
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from omni_core.local.runtime_paths import task_dir


# --- M6：按 task_id 的共享注册表（同 task 内所有 agent 共享一块黑板） -------
_REGISTRY: Dict[str, "WorldModel"] = {}
_REGISTRY_LOCK = threading.RLock()


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：先写 .tmp 再 replace，避免并发/中断留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class WorldModel:
    """内存 + 磁盘世界模型。资产跟 task_id 走。

    M6 起可作为共享黑板：用 :meth:`shared` 取实例，写入线程安全且带来源。
    """

    def __init__(self, task_id: str = "", history_k: int = 8):
        self.task_id = task_id
        self.history_k = history_k
        self.current_percept: Dict[str, Any] = {}        # backend 原始 percept，内核不解读
        self.state_text: str = ""                        # backend.text_of(percept) 的文本化
        self.actions: "deque[Dict[str, Any]]" = deque(maxlen=history_k)
        self.facts: List[str] = []
        # M6 溯源：facts 仍是纯文本列表（对外契约不变），来源另存 text -> {source, ts}
        self.fact_meta: Dict[str, Dict[str, str]] = {}
        # M5 采集能力：agent 在长任务中持久化采集到的结构化数据（跨翻页/子任务不丢）
        self.notes: List[str] = []                      # 自由文本备注（record 工具写入）
        self.collected: List[Dict[str, Any]] = []       # 结构化条目：{"text": ...}
        self.objective: str = ""
        self.run_id: str = ""
        self._lock = threading.RLock()

    # --- M6 共享注册表 ------------------------------------------------------
    @classmethod
    def shared(cls, task_id: str, history_k: int = 8) -> "WorldModel":
        """取该 task 的共享黑板（不存在则创建并登记）。

        task_id 为空时不登记（避免无 id 的临时 world 互相串台），返回独立实例。
        """
        if not task_id:
            return cls(task_id="", history_k=history_k)
        with _REGISTRY_LOCK:
            wm = _REGISTRY.get(task_id)
            if wm is None:
                wm = cls(task_id=task_id, history_k=history_k)
                _REGISTRY[task_id] = wm
            return wm

    @classmethod
    def release(cls, task_id: str) -> None:
        """任务结束后释放登记（内存回收；已落盘数据不受影响）。"""
        if not task_id:
            return
        with _REGISTRY_LOCK:
            _REGISTRY.pop(task_id, None)

    @classmethod
    def registry_keys(cls) -> List[str]:
        with _REGISTRY_LOCK:
            return list(_REGISTRY.keys())

    @classmethod
    def clear_registry(cls) -> None:
        """清空注册表（仅测试使用）。"""
        with _REGISTRY_LOCK:
            _REGISTRY.clear()

    # --- 目录 ---------------------------------------------------------------
    def _dir(self) -> Path:
        return task_dir(self.task_id)

    def _checkpoint_dir(self) -> Path:
        return self._dir() / "checkpoints"

    # --- 状态更新（委托 backend 文本化） -------------------------------------
    def update(self, percept: Dict[str, Any], backend=None) -> None:
        """写入 backend 产出的原始 percept，并由 backend.text_of 文本化。

        内核不读取 percept 内部字段——文本化完全由 backend 负责。
        """
        with self._lock:
            self.current_percept = percept or {}
            self.state_text = backend.text_of(percept) if backend else ""

    def log_action(self, call: Dict[str, Any], result: Any) -> None:
        with self._lock:
            self.actions.append(
                {
                    "tool": call.get("name"),
                    "args": call.get("args", {}),
                    "result": result,
                }
            )

    def recent_actions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.actions)

    def current_state_text(self) -> str:
        """当前环境状态的文本化表达（替代原 current_ocr_text 的屏幕假设）。"""
        return self.state_text

    def summary(self) -> str:
        with self._lock:
            return self._summary_locked()

    def _summary_locked(self) -> str:
        lines: List[str] = []
        lines.append(f"当前环境状态:\n{self.state_text or '(无)'}")
        if self.collected:
            lines.append(f"已采集条目: {len(self.collected)} 条（如：{', '.join((c.get('text') or c.get('name','')) for c in self.collected[:8])}{'...' if len(self.collected)>8 else ''}）")
        if self.notes:
            lines.append(f"备注: {len(self.notes)} 条")
        if self.facts:
            lines.append("已知事实:")
            for f in self.facts[-10:]:  # 最近 10 条
                lines.append(f"  - {f}")
        recent = self.recent_actions()
        if recent:
            lines.append("最近动作:")
            for a in recent:
                lines.append(f"  - {a['tool']}({a['args']}) -> {a['result']}")
        return "\n".join(lines)

    # --- M4b.1 知识累积 -----------------------------------------------------
    def add_fact(self, fact: str, source: str = "") -> None:
        """累积一条已知事实（按文本去重）。

        M6：``source`` 记录写入者（agent id / 分支标识）。同一文本重复写入时
        来源以最后一次为准（last-write-wins），文本本身不重复入列。
        """
        f = (fact or "").strip()
        if not f:
            return
        with self._lock:
            if f not in self.facts:
                self.facts.append(f)
            self.fact_meta[f] = {
                "source": source or "",
                "ts": datetime.now(timezone.utc).isoformat(),
            }

    def fact_source(self, fact: str) -> str:
        """取某条事实的来源（未知返回空串）。"""
        with self._lock:
            return (self.fact_meta.get(fact) or {}).get("source", "")

    def facts_for(self, scope: Optional[str] = None, k: Optional[int] = None) -> List[str]:
        """按来源筛选事实。

        scope 为 None → 全部；否则取「全局事实（source 为空）+ 本分支事实」。
        k 限制返回条数（取最近的）。
        """
        with self._lock:
            if not scope:
                picked = list(self.facts)
            else:
                picked = [
                    f for f in self.facts
                    if (self.fact_meta.get(f) or {}).get("source", "") in ("", scope)
                ]
            return picked[-k:] if k else picked

    # --- M6：按分支合成视图（每个 agent 只看与自己相关的部分） --------------
    def view(self, scope: Optional[str] = None, k: int = 10, actions: int = 5) -> str:
        """返回给某个 agent 的世界视图文本。

        与 ``summary()`` 的差别：传入 scope 时，事实只保留「全局 + 本分支」，
        且全局/本分支各占一半配额，避免多 agent 下上下文爆炸与无关噪声。
        """
        with self._lock:
            if scope:
                half = max(1, k // 2)
                glob = [f for f in self.facts
                        if (self.fact_meta.get(f) or {}).get("source", "") == ""][-half:]
                own = [f for f in self.facts
                       if (self.fact_meta.get(f) or {}).get("source", "") == scope][-half:]
                keep = set(glob) | set(own)
                picked = [f for f in self.facts if f in keep][-k:]
            else:
                picked = list(self.facts[-k:])

            lines: List[str] = []
            lines.append(f"当前环境状态:\n{self.state_text or '(无)'}")
            if self.collected:
                sample = ", ".join((c.get("text") or c.get("name", "")) for c in self.collected[:8])
                lines.append(f"已采集条目: {len(self.collected)} 条（如：{sample}"
                             f"{'...' if len(self.collected) > 8 else ''}）")
            if self.notes:
                lines.append(f"备注: {len(self.notes)} 条")
            if picked:
                lines.append("已知事实:")
                for f in picked:
                    src = (self.fact_meta.get(f) or {}).get("source", "")
                    prefix = f"[{src}] " if src else ""
                    lines.append(f"  - {prefix}{f}")
            recent = self.recent_actions()
            if actions:
                recent = recent[-actions:]
            if recent:
                lines.append("最近动作:")
                for a in recent:
                    lines.append(f"  - {a['tool']}({a['args']}) -> {a['result']}")
            return "\n".join(lines)

    # --- M5 采集持久化（跨翻页/子任务不丢） --------------------------------
    def add_note(self, text: str) -> None:
        """追加一条自由文本备注（record 工具写入，去重）。"""
        t = (text or "").strip()
        if not t:
            return
        with self._lock:
            if t not in self.notes:
                self.notes.append(t)

    def add_collected(self, name: str) -> bool:
        """记录一条采集条目（纯文本，按内容去重）；返回是否新增。"""
        n = (name or "").strip()
        if not n:
            return False
        with self._lock:
            for c in self.collected:
                if c.get("text") == n:
                    return False
            self.collected.append({"text": n})
            return True

    def add_collected_bulk(self, entries: List[Any]) -> int:
        """批量记录采集条目（list of {text|name} 或纯字符串）。返回新增条数。"""
        added = 0
        for e in entries or []:
            nm = e.get("text") or e.get("name") or "" if isinstance(e, dict) else str(e)
            if self.add_collected(nm):
                added += 1
        return added

    def merge_collection(self, other: "WorldModel", source: str = "") -> None:
        """把另一个 world 的 notes/collected / facts 合并进自身（子任务→共享黑板）。

        M6：``source`` 标记这批数据的来源分支；notes/collected 仍是全局共享
        （采集结果天然是共享产出），只有 facts 带来源。
        """
        for n in (other.notes or []):
            self.add_note(n)
        for c in (other.collected or []):
            self.add_collected(c.get("text", "") or c.get("name", ""))
        for f in (other.facts or []):
            self.add_fact(f, source=source)

    # --- M4b.1 持久化（world_model.md） -------------------------------------
    def save(self) -> str:
        """把当前状态写盘为 `world_model.md`。返回文件路径（空 task_id 则跳过写盘返回空串）。

        M6：整体在锁内取快照，且用原子写（并发 save 不会留下半截文件）。
        """
        if not self.task_id:
            return ""
        with self._lock:
            return self._save_locked()

    def _save_locked(self) -> str:
        self._dir().mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        path = self._dir() / "world_model.md"

        lines = [
            "---",
            f"task_id: {self.task_id}",
            f"last_saved: {now}",
            f"run_id: {self.run_id}",
            # F2.1：objective 超长（污染形态）时拒绝持久化到 frontmatter，避免污染磁盘
            f"objective: {self.objective}" if not getattr(self, "_objective_overlong", False) else "objective: \"\"",
            f"facts_count: {len(self.facts)}",
            "---",
            "",
            "# 当前环境状态",
            f"- {self.state_text or '(无)'}",
            "",
            "# 已知事实",
        ]
        for f in self.facts:
            lines.append(f"- {f}")
        if not self.facts:
            lines.append("_(暂无)_")
        lines.append("")
        lines.append("# 采集记录")
        for c in self.collected:
            lines.append(f"- {c.get('text', '')}")
        if not self.collected:
            lines.append("_(暂无)_")
        lines.append("")
        lines.append("# 备注")
        for n in self.notes:
            lines.append(f"- {n}")
        if not self.notes:
            lines.append("_(暂无)_")
        lines.append("")
        lines.append("# 最近动作")
        for a in self.recent_actions():
            lines.append(f"- {a['tool']}({a['args']}) -> {a['result']}")
        if not self.actions:
            lines.append("_(暂无)_")

        _atomic_write_text(path, "\n".join(lines))
        self._save_collected_json()
        return str(path)

    def _save_collected_json(self) -> None:
        """把采集结果另存为机器可读的 JSON（.omniagent/<project>/world_model/<task>/collected.json）。

        M6：同时落 ``facts_meta``（事实来源），使 view(scope) 在重载后仍可溯源。
        """
        try:
            self._dir().mkdir(parents=True, exist_ok=True)
            with self._lock:
                payload = {
                    "task_id": self.task_id,
                    "run_id": self.run_id,
                    "objective": self.objective,
                    "state_text": self.state_text,
                    "collected": list(self.collected),
                    "notes": list(self.notes),
                    "facts_meta": {k: dict(v) for k, v in self.fact_meta.items()},
                }
            _atomic_write_text(
                self._dir() / "collected.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception:
            pass

    def load(self) -> bool:
        """从 `world_model.md` 恢复状态。成功返回 True，文件不存在返回 False。"""
        path = self._dir() / "world_model.md"
        if not path.exists():
            return False
        content = path.read_text(encoding="utf-8")
        # 解析 YAML frontmatter（精简实现：读到第二个 ---）
        in_front = False
        front_end = 0
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip()
            if stripped == "---":
                if not in_front:
                    in_front = True
                else:
                    front_end = i
                    break

        # 解析 facts 列表
        body = content.split("\n")[front_end + 1:]
        parsed_facts = []
        in_facts_section = False
        in_actions_section = False
        for line in body:
            s = line.strip()
            if s.startswith("# 已知事实"):
                in_facts_section = True
                in_actions_section = False
                continue
            if s.startswith("# 最近动作"):
                in_facts_section = False
                in_actions_section = True
                continue
            if in_facts_section and s.startswith("- "):
                fact = s[2:].strip()
                if fact and fact != "_(暂无)_":
                    parsed_facts.append(fact)
        self.facts = parsed_facts

        # 优先从 collected.json 恢复采集数据（机器可读、稳健）
        cj = self._dir() / "collected.json"
        if cj.exists():
            try:
                data = json.loads(cj.read_text(encoding="utf-8"))
                self.collected = [{"text": c.get("text") or c.get("name") or ""}
                                  for c in (data.get("collected") or [])]
                self.notes = list(data.get("notes") or [])
                # 恢复原始任务目标与环境状态文本（续跑/崩溃恢复用，不被空值覆盖）
                if data.get("objective"):
                    self.objective = data["objective"]
                if data.get("state_text"):
                    self.state_text = data["state_text"]
                # M6：恢复事实来源（旧文件无此字段时退化为无来源）
                meta = data.get("facts_meta") or {}
                self.fact_meta = {
                    f: dict(meta[f]) for f in self.facts if f in meta
                }
                return True
            except Exception:
                pass
        # 兜底：从 world_model.md 正文解析「采集记录 / 备注」段落
        in_coll = in_notes = False
        parsed_coll, parsed_notes = [], []
        for line in body:
            s = line.strip()
            if s.startswith("# 采集记录"):
                in_coll, in_notes = True, False
                continue
            if s.startswith("# 备注"):
                in_coll, in_notes = False, True
                continue
            if s.startswith("# 最近动作"):
                in_coll = in_notes = False
                continue
            if in_coll and s.startswith("- "):
                t = s[2:].strip()
                if t and t != "_(暂无)_":
                    parsed_coll.append({"text": t})
            if in_notes and s.startswith("- "):
                t = s[2:].strip()
                if t and t != "_(暂无)_":
                    parsed_notes.append(t)
        self.collected = parsed_coll
        self.notes = parsed_notes
        return True

    # --- M4b.1 子目标 checkpoint --------------------------------------------
    def checkpoint(self, subgoal: Dict[str, str], snapshot: Optional[Dict] = None) -> str:
        """在子目标边界写盘 checkpoint（JSON）。返回文件路径（空 task_id 则跳过返回空串）。"""
        if not self.task_id:
            return ""
        rid = self.run_id or uuid.uuid4().hex[:12]
        cdir = self._checkpoint_dir() / rid
        cdir.mkdir(parents=True, exist_ok=True)
        desc = subgoal.get("desc", "unknown")
        safe_name = "".join(c if c.isalnum() or c in "_ -" else "_" for c in desc)[:40].strip().replace(" ", "_")
        path = cdir / f"{safe_name}.json"

        with self._lock:
            data = {
                "run_id": rid,
                "task_id": self.task_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "subgoal": subgoal,
                "world_snapshot": snapshot or {
                    "state_text": self.state_text,
                    "percept_keys": list((self.current_percept or {}).keys()),
                    "facts": list(self.facts),
                    "objective": self.objective,
                    "facts_meta": {k: dict(v) for k, v in self.fact_meta.items()},
                    "recent_actions": [
                        {"tool": a["tool"], "args": a["args"], "result": a["result"]}
                        for a in self.recent_actions()
                    ],
                },
            }
        _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        return str(path)

    @classmethod
    def load_checkpoint(cls, task_id: str, run_id: str) -> Optional[Dict]:
        """加载指定 task_id/run_id 下最新 checkpoint。"""
        cdir = task_dir(task_id) / "checkpoints" / run_id
        if not cdir.exists():
            return None
        files = sorted(cdir.glob("*.json"), key=os.path.getmtime, reverse=True)
        if not files:
            return None
        return json.loads(files[0].read_text(encoding="utf-8"))

    @classmethod
    def list_checkpoints(cls, task_id: str) -> List[str]:
        """列出某 task 下所有 run_id。"""
        cdir = task_dir(task_id) / "checkpoints"
        if not cdir.exists():
            return []
        dirs = [d for d in cdir.iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: os.path.getmtime(str(d)), reverse=True)
        return [d.name for d in dirs]

    # --- M4b.1 merge_progress（防 JPEG 效应） --------------------------------
    def merge_progress(self, key_progress: List[str], source: str = "") -> None:
        """将压缩后的关键进展写回 world-model facts，避免反复压缩导致信息磨灭。

        大脑长任务压缩时调用：摘要中的关键事实经此方法进入持久 facts，
        后续 plan/reflect 均可通过 summary() 注入。
        M6：``source`` 标记压缩发生的分支（压缩产物归共享黑板，兄弟分支可见）。
        """
        for p in key_progress:
            p = p.strip()
            if p:
                self.add_fact(p, source=source)

    # --- snapshot 供 checkpoint 与 stateless brain 注入 ---------------
    def snapshot(self) -> Dict[str, Any]:
        """返回当前世界状态的轻量快照，供 checkpoint / 大脑注入。

        去场景化（§9）：只暴露 state_text 文本与元数据，不暴露任何感知字段名。
        """
        with self._lock:
            return {
                "state_text": self.state_text,
                "facts": list(self.facts[-10:]),
                "collected_count": len(self.collected),
                "notes_count": len(self.notes),
                "objective": self.objective,
                "recent_actions": [
                    {"tool": a["tool"], "args": a["args"], "result": a["result"]}
                    for a in self.recent_actions()[-5:]
                ],
            }
