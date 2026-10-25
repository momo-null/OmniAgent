"""dispatch —— 公共派发库（通道无关，接入设计 v1.1 §6）

职责：objective → ToolLoop（复用 run_agent_autonomous 既有模式）→ 结果回发。
- **project 关联（姐姐 2026-10-25 要求）**：job/消息携带 project_id → 写入
  TaskSpec.project_id → 三层 AGENTS.md 注入自动取 projects/<pid>/AGENTS.md，
  trajectory/world-model 也按项目归属落盘。定时任务因此与 project 天然关联。
- 串行队列：飞书消息与 cron 任务共用一条 FIFO（ETF 分析非秒级敏感，排队不并发）。
- 结果回发：reply 非空（消息触发）→ 完成后经 ReplyChannel 回发；
  reply=None + notify_channel（cron）→ broadcast 到该通道默认会话。
- 失败直推：任务异常/超时由本库**不经 agent** 直接经通道 API 推错误文本
  （应急语义——agent 死了也要让姐姐知道任务没成）。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .channels import context
from .channels.base import ReplyChannel


@dataclass
class DispatchResult:
    task_id: str
    ok: bool
    summary: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_WORKER: Optional[threading.Thread] = None


def _extract_summary(result: Any) -> str:
    """从 ToolLoop.run_task 的返回里抠出可读摘要（结构防御式，不假设固定键）。"""
    if not isinstance(result, dict):
        return str(result)[:500]
    for key in ("summary", "final", "final_answer", "answer", "output", "reason", "message", "result"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:500]
    try:
        return json.dumps(result, ensure_ascii=False)[:500]
    except Exception:
        return str(result)[:500]


def _run_task_once(item: Dict[str, Any]) -> DispatchResult:
    task_id = item["task_id"]
    reply: Optional[ReplyChannel] = item.get("reply")
    notify_channel: Optional[str] = item.get("notify_channel")
    job_id: str = item.get("job_id", "")

    # 回发工具函数：优先消息来源句柄；cron 则用 notify 通道的默认会话
    def _send(text: str) -> None:
        try:
            if reply is not None:
                reply.send_text(text)
            elif notify_channel:
                adapter = context.get_adapter(notify_channel)
                if adapter:
                    adapter.broadcast(text)
        except Exception as e:  # 回发失败不影响任务状态
            print(f"[dispatch] 回发失败(任务{task_id}): {e}")

    try:
        import config
        from omni_core.local.loop import ToolLoop, TaskSpec

        brain_cfg = (config.load_config() or {}).get("brain", {})
        if not (brain_cfg.get("api_key") or brain_cfg.get("api_key_env")):
            raise RuntimeError("大脑未配置（config.brain 或环境变量），无法派发任务")

        loop = ToolLoop(brain_cfg, verbose=False)
        spec = TaskSpec(
            objective=item["objective"],
            done_when=item.get("done_when", ""),
            max_steps=item.get("max_steps") or 40,
            task_id=task_id,
            project_id=item.get("project_id") or None,   # ← project 关联核心
        )
        # 当前任务回发句柄（线程绑定）→ msg_send_* 工具缺省发回来源会话
        context.set_current_reply(reply)
        try:
            result = loop.run_task(spec)
        finally:
            context.set_current_reply(None)

        summary = _extract_summary(result)
        # run_task 真实返回：success / escalated / steps / trajectory_file …
        ok = bool(result.get("success", True)) and not result.get("escalated") if isinstance(result, dict) else True
        dr = DispatchResult(task_id=task_id, ok=ok, summary=summary, result=result if isinstance(result, dict) else {"raw": str(result)})
        if job_id:
            from .scheduler import record_run
            record_run(job_id, task_id, "ok" if ok else "error", summary=summary)
        _send(f"{'✅' if ok else '⚠️'} [{item.get('source', 'task')} {task_id[:8]}] {summary or '(无摘要)'}")
        return dr
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[dispatch] 任务失败 {task_id}: {err}\n{traceback.format_exc()[-800:]}")
        # 失败直推（不经 agent——它可能正是死因）
        _send(f"🚨 任务失败 [{item.get('source', 'task')} {task_id[:8]}]: {err}（由网关直发，agent 可能已死）")
        return DispatchResult(task_id=task_id, ok=False, error=err)


def _worker_loop() -> None:
    while True:
        item = _QUEUE.get()
        if item is None:
            break
        try:
            _run_task_once(item)
        except Exception:  # _run_task_once 已兜底；此处防御 worker 自身
            traceback.print_exc()
        finally:
            _QUEUE.task_done()


def ensure_worker() -> None:
    """惰性启动串行 worker（网关/调度器共用）。"""
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_worker_loop, name="dispatch-worker", daemon=True)
        _WORKER.start()


def submit(objective: str, done_when: str = "", max_steps: int = 40, *,
           source: str = "manual", project_id: Optional[str] = None,
           reply: Optional[ReplyChannel] = None,
           notify_channel: Optional[str] = None, job_id: Optional[str] = None) -> str:
    """异步提交（网关消息/cron 用）。返回 task_id。"""
    ensure_worker()
    task_id = uuid.uuid4().hex[:16]
    _QUEUE.put({"task_id": task_id, "objective": objective, "done_when": done_when,
                "max_steps": max_steps, "source": source, "project_id": project_id,
                "reply": reply, "notify_channel": notify_channel, "job_id": job_id or ""})
    return task_id


def dispatch_sync(objective: str, done_when: str = "", max_steps: int = 40, *,
                  source: str = "manual", project_id: Optional[str] = None,
                  reply: Optional[ReplyChannel] = None,
                  notify_channel: Optional[str] = None, job_id: Optional[str] = None) -> DispatchResult:
    """同步执行（CLI/测试/「立即运行」用）。"""
    return _run_task_once({"task_id": uuid.uuid4().hex[:16], "objective": objective,
                           "done_when": done_when, "max_steps": max_steps, "source": source,
                           "project_id": project_id, "reply": reply, "notify_channel": notify_channel,
                           "job_id": job_id or ""})


def queue_size() -> int:
    return _QUEUE.qsize()
