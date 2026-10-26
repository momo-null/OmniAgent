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
    real_task_id = ""      # TaskStore 登记后的正式 ID（异常路径未创建时保持空）
    temp_task = False      # 无关联项目的临时任务：完成即删

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
        from omni_core.local.task_store import TaskStore, ProjectStore

        brain_cfg = (config.load_config() or {}).get("brain", {})
        if not (brain_cfg.get("api_key") or brain_cfg.get("api_key_env")):
            raise RuntimeError("大脑未配置（config.brain 或环境变量），无法派发任务")

        # ── 任务落 TaskStore（UI 项目/会话列表实时可见过程）──
        # 有关联项目 → 任务持久留在项目下；无项目 → 临时任务，跑完即删（结果留 runs.jsonl）。
        project_id = item.get("project_id") or None
        temp_task = project_id is None
        if temp_task:
            project_id = None  # TaskStore.create 缺省归 default 项目
        meta = TaskStore.create(objective=item["objective"], done_when=item.get("done_when", ""),
                                project_id=project_id,
                                session_id=(item.get("session_id") or None) or None)
        real_task_id = meta["task_id"]
        TaskStore.update(real_task_id, state="running")
        if job_id:  # 派发即落 running 记录（详情页秒见反馈）
            from .scheduler import record_run
            record_run(job_id, real_task_id, "running")

        # ── 会话轨迹持久化（与 Web 端 chat_runtime 同格式）：无头任务（cron/消息）──
        # 也写入 projects/<pid>/sessions/<sid>.jsonl，前端打开任务即见完整过程流
        # （partial 增量 + final 收口，coalesce_records 可并回单轮）；否则只有
        # tasks/ 下的轨迹 jsonl，Web 历史接口读不到，界面空白。
        pid = meta.get("project_id") or ""
        sid = meta.get("session_id") or ""
        run_key = f"rk_{int(time.time() * 1000)}"
        turn_steps: list = []
        _flushed = 0
        _last_answer = ""

        def _flush_partial(force: bool = False) -> None:
            nonlocal _flushed
            if not (pid and sid):
                return
            if not (force or len(turn_steps) - _flushed >= 20):
                return
            if len(turn_steps) <= _flushed:
                return
            try:
                ProjectStore.append_message(
                    pid, sid, "assistant", "", task_id=real_task_id,
                    extra={"steps": turn_steps[_flushed:], "partial": True, "run_key": run_key})
                _flushed = len(turn_steps)
            except Exception:
                pass

        def _on_thinking(role: str, content: str, model: str) -> None:
            if role == "brain":
                turn_steps.append({"type": "thinking", "model": model, "content": content})
                _flush_partial()

        def _on_tool_call(role: str, name: str, arguments: str, result: str, model: str) -> None:
            if name in ("verify", "verify_done"):
                return
            turn_steps.append({"type": "tool_call", "model": model, "name": name,
                               "arguments": arguments or "", "result": result or ""})
            _flush_partial()

        def _on_llm_delta(role: str, kind: str, block_id: str, text: str, model: str) -> None:
            nonlocal _last_answer
            if kind == "message" and role == "brain":
                _last_answer = text

        def _record_final(content: str) -> None:
            if not (pid and sid):
                return
            try:
                ProjectStore.append_message(
                    pid, sid, "assistant", content, task_id=real_task_id,
                    extra={"steps": turn_steps, "run_key": run_key})
            except Exception:
                pass

        if pid and sid:
            try:
                ProjectStore.append_message(pid, sid, "user", item["objective"],
                                            task_id=real_task_id)
            except Exception:
                pass

        # ── 固定会话连续对话：读会话历史注入 TaskSpec.history（对齐 Web /chat 链路）──
        # B3 同款：以服务端会话记录（含结构化 steps）重建模型上下文，跨消息记忆唯一来源
        history_items = []
        if pid and sid:
            try:
                _recs = ProjectStore.coalesce_records(ProjectStore.read_session(pid, sid))
                for r in _recs:
                    role = r.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    ex = r.get("extra") or {}
                    content = str(r.get("content") or "")
                    steps = ex.get("steps") or []
                    # agent 轮：把已执行的步骤摘要并入口播文本，模型记得做过什么
                    if role == "assistant" and steps:
                        tool_names = [s.get("name") for s in steps if s.get("type") == "tool_call"]
                        thought = next((s.get("content", "") for s in reversed(steps)
                                        if s.get("type") == "thinking"), "")
                        seg = f"[上轮执行] 工具: {', '.join(filter(None, tool_names)) or '无'}"
                        if thought:
                            seg += f"；要点: {thought[:200]}"
                        content = f"{content}\n{seg}".strip()
                    if content:
                        history_items.append({"role": role, "content": content})
                history_items = history_items[-40:]  # 防超长（消息级滑窗，压缩归大脑）
            except Exception:
                history_items = []

        loop = ToolLoop(brain_cfg, verbose=False,
                        on_thinking=_on_thinking, on_tool_call=_on_tool_call,
                        on_llm_delta=_on_llm_delta)
        spec = TaskSpec(
            objective=item["objective"],
            done_when=item.get("done_when", ""),
            max_steps=item.get("max_steps") or 40,
            task_id=real_task_id,
            project_id=meta.get("project_id"),
            history=history_items,
        )
        # 当前任务回发句柄（线程绑定）→ msg_send_* 工具缺省发回来源会话
        context.set_current_reply(reply)
        try:
            result = loop.run_task(spec)
        finally:
            context.set_current_reply(None)
        _record_final(_last_answer or str((result or {}).get("reason") or ""))

        summary = _extract_summary(result)
        # 回复/推送优先用 agent 的最终口播（真实回答），内部过程文案（如
        # 「无校验条件，信任大脑」这类 finish 验证器 reason）只做兜底
        answer = (_last_answer or "").strip() or summary
        # run_task 真实返回：success / escalated / steps / trajectory_file …
        ok = bool(result.get("success", True)) and not result.get("escalated") if isinstance(result, dict) else True
        try:
            TaskStore.update(real_task_id, state="done" if ok else "failed", success=ok)
        except Exception:
            pass
        if temp_task:  # 临时任务：结果已记 runs.jsonl，索引即删（列表不再显示）
            try:
                TaskStore.remove(real_task_id)
            except Exception:
                pass
        dr = DispatchResult(task_id=real_task_id, ok=ok, summary=summary, result=result if isinstance(result, dict) else {"raw": str(result)})
        if job_id:
            from .scheduler import record_run
            record_run(job_id, real_task_id, "ok" if ok else "error", summary=answer)
        # 飞书单条文本上限 4000 字符（send_text 内截断到 4000，这里先留前缀余量）
        _send(f"{'✅' if ok else '⚠️'} [{item.get('source', 'task')} {real_task_id[:8]}]\n{answer[:3500] or '(无摘要)'}")
        return dr
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[dispatch] 任务失败 {task_id}: {err}\n{traceback.format_exc()[-800:]}")
        # 失败也写会话终态：已收集的 steps 原样收口（前端能看到失败前做了什么）。
        # 防御：异常可能发生在记录器定义之前（import/TaskStore 失败），按局部名守卫。
        try:
            if "turn_steps" in locals():
                _record_final(str(((locals().get("result") or {}).get("reason")) or err))
        except Exception:
            pass
        # 失败也必须有结果：task 状态落库（若已创建）+ runs 记录
        try:
            from omni_core.local.task_store import TaskStore
            if real_task_id:
                TaskStore.update(real_task_id, state="failed", success=False)
                if temp_task:
                    TaskStore.remove(real_task_id)
        except Exception:
            pass
        if job_id:
            from .scheduler import record_run
            record_run(job_id, real_task_id or task_id, "error", error=err)
        # 失败直推（不经 agent——它可能正是死因）
        _send(f"🚨 任务失败 [{item.get('source', 'task')} {task_id[:8]}]: {err}（由网关直发，agent 可能已死）")
        return DispatchResult(task_id=real_task_id or task_id, ok=False, error=err)


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
           notify_channel: Optional[str] = None, job_id: Optional[str] = None,
           session_id: Optional[str] = None) -> str:
    """异步提交（网关消息/cron 用）。返回 task_id。

    session_id：显式指定会话（消息通道用 ``msg:<channel>:<chat_id>`` 固定会话——
    同一聊天窗口连续对话共享上下文，对齐 hermes 的 thread 绑定语义；不传则每任务新会话）。
    """
    ensure_worker()
    task_id = uuid.uuid4().hex[:16]
    _QUEUE.put({"task_id": task_id, "objective": objective, "done_when": done_when,
                "max_steps": max_steps, "source": source, "project_id": project_id,
                "reply": reply, "notify_channel": notify_channel, "job_id": job_id or "",
                "session_id": session_id or ""})
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
