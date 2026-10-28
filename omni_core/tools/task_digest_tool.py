"""read_task_digest —— 任务细节的**按需拉取端**（plan §3.4 注意力预算）。

为什么需要它：任务轨迹（思考 + 工具调用整轮）原本会持久化并**回灌下一轮模型
上下文**。长期干活会让对话人格的上下文被细节塞满（"注意力被冲淡"）。三层预算之后：

- 对话层（人格可见）：对话 + **任务卡**（目标 / 状态 / 结论一行，≤120 字）；
- 任务层（worker 可见）：完整 steps / 工具结果 / 文件路径 —— 不进对话上下文；
- 存储层：记忆 + 事件流。

细节改为**按需拉取**：模型真需要"上次那个任务到底怎么做的"时调本工具。与 L2 的
「文件名常驻 + read_scene 读全文」完全同构（渐进披露，两个消费面一套范式）。

纯读工具：不声明 risk，S1 漏斗照常生效；不写任何文件。
"""
from __future__ import annotations

from typing import Any, Dict, List

from omni_core.tools.base import function_tool

#: 当前运行绑定的 task_id（空参调用时用当前任务）。由 sdk_bridge 每 run 注入。
_current_task_id: Dict[str, str] = {"value": ""}

_MAX_STEPS = 20
_STEP_RESULT_CHARS = 300
_DIGEST_MAX_CHARS = 4000


def set_task_digest_context(task_id: str) -> None:
    """绑定当前运行的 task_id（read_task_digest 空参时用它）。"""
    _current_task_id["value"] = task_id or ""


def current_digest_task_id() -> str:
    return _current_task_id.get("value", "") or ""


def _last_assistant_text(task_id: str, meta: Dict[str, Any]) -> str:
    """该任务所属会话里最后一条 assistant 文本（= 上一轮的结论）。"""
    try:
        from omni_core.local.task_store import ProjectStore

        pid = str(meta.get("project_id") or "")
        sid = str(meta.get("session_id") or "")
        if not (pid and sid):
            return ""
        recs = ProjectStore.read_session(pid, sid)
        for r in reversed(recs or []):
            if r.get("role") in ("assistant", "agent"):
                txt = str(r.get("content") or "").strip()
                if txt and str(r.get("task_id") or task_id) == task_id:
                    return txt[:600]
    except Exception:
        pass
    return ""


def _tail_steps(task_id: str) -> List[Dict[str, str]]:
    """轨迹尾部工具调用（工具名 + 参数/结果截断），不含思考正文。"""
    try:
        from omni_core.local.runtime_paths import task_trajectory

        d = task_trajectory(task_id)
        if not d.is_dir():
            return []
        files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            return []
        out: List[Dict[str, str]] = []
        for ln in files[0].read_text(encoding="utf-8").splitlines()[-200:]:
            ln = ln.strip()
            if not ln:
                continue
            try:
                import json

                step = json.loads(ln)
            except Exception:
                continue
            action = step.get("action") or {}
            name = str(action.get("tool") or "")
            if not name:
                continue
            args = action.get("args")
            out.append({
                "tool": name,
                "args": (str(args)[:_STEP_RESULT_CHARS] if args else ""),
                "result": str(step.get("result") or "")[:_STEP_RESULT_CHARS],
            })
        return out[-_MAX_STEPS:]
    except Exception:
        return []


def task_card(task_id: str) -> Dict[str, str]:
    """任务卡（对话层唯一可见形态）：目标 / 状态 / 结论一行，≤120 字。

    对话上下文只放这个，细节一律靠 ``read_task_digest`` 现查。
    """
    meta: Dict[str, Any] = {}
    try:
        from omni_core.local.task_store import TaskStore

        meta = TaskStore.get(task_id) or {}
    except Exception:
        meta = {}
    goal = str(meta.get("objective") or "").strip().replace("\n", " ")
    state = str(meta.get("state") or "").strip()
    concl = _last_assistant_text(task_id, meta).replace("\n", " ")
    line = f"目标：{goal[:50]}｜状态：{state or '未知'}"
    if concl:
        line += f"｜结论：{concl[:50]}"
    return {"task_id": task_id, "goal": goal, "state": state,
            "conclusion": concl, "card": line[:120]}


@function_tool(
    name="read_task_digest",
    description=("按需拉取某个任务（含历史轮次）的执行细节：目标 / 状态 / 结论 / "
                 "最近的工具调用与结果 / 产物。对话上下文里只有一行任务卡；"
                 "当用户追问「上次那个任务具体怎么做的 / 跑出什么」时再调用本工具。"),
    unit="core",
)
def read_task_digest(task_id: str = "") -> Dict[str, Any]:
    """读取任务摘要与最近执行细节（纯读，不改任何状态）。

    Args:
        task_id: 目标任务 id；留空 = 当前运行的任务。
    """
    try:
        tid = (task_id or "").strip() or current_digest_task_id()
        if not tid:
            return {"ok": False, "error": "未指定任务，且当前运行未绑定任务"}
        meta: Dict[str, Any] = {}
        try:
            from omni_core.local.task_store import TaskStore

            meta = TaskStore.get(tid) or {}
        except Exception:
            meta = {}
        if not meta:
            return {"ok": False, "error": f"未找到任务: {tid}"}
        card = task_card(tid)
        steps = _tail_steps(tid)
        artifacts: List[Dict[str, Any]] = []
        try:
            from omni_core.local.artifacts import load_artifacts

            artifacts = [{"name": a.get("name"), "path": a.get("path")}
                         for a in (load_artifacts(tid) or [])][-10:]
        except Exception:
            artifacts = []
        digest = {
            "ok": True,
            "task_id": tid,
            "card": card["card"],
            "objective": card["goal"][:_DIGEST_MAX_CHARS],
            "state": card["state"],
            "done_when": str(meta.get("done_when") or ""),
            "conclusion": card["conclusion"],
            "steps": steps,
            "artifacts": artifacts,
        }
        # 硬上限：宁可少给，也不让一次"按需拉取"变成新的上下文炸弹
        try:
            import json

            if len(json.dumps(digest, ensure_ascii=False)) > _DIGEST_MAX_CHARS * 2:
                digest["steps"] = steps[-8:]
        except Exception:
            pass
        return digest
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
