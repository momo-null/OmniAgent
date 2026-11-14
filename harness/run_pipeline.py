"""运行管线公共装配层（P1~P3）。

只收 Web 与渠道共享的“无交互态”中段：历史塑形、场合自述注入、过程记录
（thinking/tool/partial/final）、记忆入队、运行边界标注。

明确不收：SSE 推流、force 打断与补投、daemon paused、ReplyChannel 回发、
失败直推、三形态任务落库、TaskStore 状态机。这些是入口语义，留在各自入口。

边界：本模块属 harness（宿主侧），可依赖 omni_core；omni_core 不得反向 import。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from utils import get_logger

logger = get_logger("harness.run_pipeline")

#: 部分落盘阈值（与两个入口原有实现保持一致）
PARTIAL_FLUSH_EVERY = 20


# ── P1：历史塑形 / 场合自述 ──────────────────────────────────────────
def build_history(
    records: List[Dict[str, Any]],
    *,
    max_items: Optional[int] = None,
    time_minutes: Optional[int] = None,
    drop_silent: bool = False,
    now_ts: Optional[float] = None,
    roles: Optional[tuple] = ("user", "assistant"),
    converter: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """会话记录 → 模型历史（或只做过滤后的记录）。

    两端语义差异参数化：
    - 渠道（dispatch）：``max_items=12/40``、``time_minutes=90``、``drop_silent=True``，
      默认 converter（role/content，空内容丢弃）；
    - Web（chat_runtime）：三个过滤全关，传 ``converter=_session_records_to_model_messages``
      保留任务卡/窗口/折叠提示。

    ``roles=None`` 表示不按角色过滤（Web 侧要保留 system 记录）。
    """
    cut = None
    if time_minutes and time_minutes > 0:
        cut = (now_ts if now_ts is not None else time.time()) - int(time_minutes) * 60

    out: List[Dict[str, Any]] = []
    for r in records or []:
        role = r.get("role")
        if roles is not None and role not in roles:
            continue
        if drop_silent and role == "assistant" and (r.get("extra") or {}).get("silent"):
            continue
        ts = str(r.get("ts") or "")
        if cut is not None and ts:
            try:
                if datetime.fromisoformat(ts).timestamp() < cut:
                    continue
            except ValueError:
                pass  # 异常时间戳按在窗内处理（fail-open）
        out.append(r)

    if max_items and max_items > 0:
        out = out[-int(max_items):]
    if converter is not None:
        return converter(out)
    return [
        {"role": r.get("role"), "content": str(r.get("content") or "")}
        for r in out
        if str(r.get("content") or "")
    ]


def append_outbound_note(objective: str, channel: str, chat_id: str) -> str:
    """把 (通道, 会话) 的出站守则附到 objective；配置缺失/解析失败原样返回。"""
    try:
        from harness.outbound import note, resolve

        text = note(resolve(str(channel or ""), str(chat_id or "")))
        if text:
            return f"{objective}\n{text}"
    except Exception:
        pass
    return objective


# ── P2：过程记录 ─────────────────────────────────────────────────────
def merge_thinking_step(steps: List[Dict[str, Any]], block_id: str,
                        model: str, content: str) -> None:
    """流式推理链按 block_id upsert（快照语义：content 覆盖，不追加）。"""
    for s in steps:
        if s.get("type") == "thinking" and s.get("id") == block_id:
            s["content"] = content
            s["model"] = model
            return
    steps.append({"type": "thinking", "id": block_id, "model": model, "content": content})


def merge_message_step(steps: List[Dict[str, Any]], block_id: str,
                       model: str, content: str) -> None:
    """流式口播按 block_id upsert（与 thinking 对称）。"""
    for s in steps:
        if s.get("type") == "message" and s.get("id") == block_id:
            s["content"] = content
            s["model"] = model
            return
    steps.append({"type": "message", "id": block_id, "model": model, "content": content})


class TurnRecorder:
    """一轮 run 的过程记录器：只负责写，不负责推流。

    - ``steps`` 由调用方持有（同一列表引用），记录后两端仍可做 silent/send 判定；
    - ``append_extra`` 是 ProjectStore.append_message 的进料道参数
      （context_key/l0_kind/speaker），Web 侧不传、渠道侧传。
    """

    def __init__(
        self,
        steps: List[Dict[str, Any]],
        *,
        project_id: str = "",
        session_id: str = "",
        task_id: str = "",
        run_key: str = "",
        flush_every: int = PARTIAL_FLUSH_EVERY,
        append_extra: Optional[Dict[str, Any]] = None,
        allow_empty_thinking: bool = False,
    ) -> None:
        self.steps = steps
        self.project_id = str(project_id or "")
        self.session_id = str(session_id or "")
        self.task_id = str(task_id or "")
        self.run_key = str(run_key or "")
        self.flush_every = max(1, int(flush_every))
        self.append_extra = dict(append_extra or {})
        self.allow_empty_thinking = bool(allow_empty_thinking)
        self._flushed = 0

    # ── 三种过程事件 ──
    def add_thinking(self, role: str, content: str, model: str,
                     *, block_id: Optional[str] = None) -> None:
        """非流式追加 / 流式按 block_id upsert；role 过滤由调用方做。"""
        if block_id:
            merge_thinking_step(self.steps, block_id, model, content)
        elif content or self.allow_empty_thinking:
            self.steps.append({"type": "thinking", "model": model, "content": content})
        self.flush_partial()

    def add_message_delta(self, block_id: str, model: str, content: str) -> None:
        merge_message_step(self.steps, block_id, model, content)
        self.flush_partial()

    def add_tool_call(self, role: str, name: str, arguments: str,
                      result: str, model: str) -> None:
        if name in ("verify", "verify_done"):
            return
        self.steps.append({"type": "tool_call", "model": model, "name": name,
                           "arguments": arguments or "", "result": result or ""})
        self.flush_partial()

    # ── 落盘 ──
    def flush_partial(self, force: bool = False) -> None:
        """把新增过程步增量落一条 partial（崩溃不丢、终态防巨型单行）。"""
        if not (self.project_id and self.session_id):
            return
        if not (force or len(self.steps) - self._flushed >= self.flush_every):
            return
        if len(self.steps) <= self._flushed:
            return
        try:
            from omni_core.local.task_store import ProjectStore

            ProjectStore.append_message(
                self.project_id, self.session_id, "assistant", "",
                task_id=self.task_id,
                extra={"steps": self.steps[self._flushed:], "partial": True,
                       "run_key": self.run_key},
                **self.append_extra)
            self._flushed = len(self.steps)
        except Exception:
            pass

    def record_final(self, content: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """终态收口：一条带 steps 的 assistant 记录。

        ``extra`` 优先于默认字段（Web 侧可传 ``steps=[]`` 表示超限省略，
        渠道侧传 silent/send 标记）。
        """
        if not (self.project_id and self.session_id):
            return
        payload: Dict[str, Any] = {"steps": self.steps, "run_key": self.run_key}
        payload.update(dict(extra or {}))
        try:
            from omni_core.local.task_store import ProjectStore

            ProjectStore.append_message(
                self.project_id, self.session_id, "assistant", content,
                task_id=self.task_id, extra=payload, **self.append_extra)
        except Exception:
            pass


# ── P3：记忆入队 / 运行边界 ─────────────────────────────────────────
def enqueue_memory(
    project_id: str,
    brain_cfg: Optional[Dict[str, Any]],
    *,
    source_task: str = "",
    did_work: bool = True,
    throttled: bool = False,
    on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> bool:
    """按入口语义入队：Web 不节流、渠道 chat 高频节流。"""
    if not project_id:
        return False
    try:
        from omni_core import memory_writer

        fn = memory_writer.enqueue_throttled if throttled else memory_writer.enqueue
        return bool(fn(project_id, brain_cfg, source_task=source_task,
                       did_work=bool(did_work), on_done=on_done))
    except Exception:
        return False


def set_run_boundary(
    *,
    context_key: str = "",
    source_kind: str = "",
    source_external: bool = False,
    peer: str = "",
    session_id: str = "",
    sent_count: Any = None,
) -> bool:
    """写入 context_signal + 进料道标注；失败静默（增强项，不阻断运行）。"""
    try:
        from harness.events import registry
        from omni_core.local.loop import context_signal

        registry.set_source_kind(source_kind)
        context_signal.set_boundary(
            context_key=context_key,
            source_kind=source_kind,
            source_external=bool(source_external),
            peer=peer,
            session_id=session_id,
            # 历史实现传的是 registry.sent_count 函数对象；保持原样，避免行为漂移
            sent_count=registry.sent_count if sent_count is None else sent_count,
        )
        return True
    except Exception:
        return False


def clear_run_boundary() -> None:
    """清掉本轮进料道标注（进程级单值，防残留串进下一轮）。"""
    try:
        from harness.events import registry
        from omni_core.local.loop import context_signal

        registry.set_source_kind("")
        context_signal.set_boundary()
    except Exception:
        pass
