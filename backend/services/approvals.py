"""S2 审批后端：SSE 推卡 + REST 决议 + 任务级记忆（sandbox-permission-design.md §6）。

内核门（``omni_core/tools/policy.py``）经 ``ApprovalSink`` 协议调用本模块；
后端启动时经 ``install_approval_sink()`` 注册。未注册（单测 / 脚本直调工具）
内核回落 ``AutoDenySink``——fail-closed 且不挂测试。

生命周期与 outbox 同哲学：ApprovalStore / 任务级审批记忆均为**内存态**，
任务终态由 ``cancel_task_approvals``（``helpers._finish_task`` 挂点）清空——
待批卡置 cancelled 并唤醒阻塞的工具线程，不允许任务结束后还有卡悬着。

刷新恢复：推卡同时并入 ``GET /api/runtime/live`` 的 approvals 通道（helpers），
前端挂载即重建顶置卡。审计：文件落盘在内核 ``policy.audit``；本模块只镜像
一条 ``kind="audit"`` 的 debug SSE 事件供右栏运行日志实时可见（§6.5 裁决）。
"""
from __future__ import annotations

import threading
from typing import Dict, Optional, Set, Tuple

from omni_core.tools import policy
from omni_core.tools.policy import ApprovalCard, Decision

from backend.api.routers.helpers import (
    _running_task_id,
    push_approval,
    push_approval_resolved,
    push_chat,
)

_lock = threading.RLock()
# approval_id -> {"card": ApprovalCard, "event": threading.Event, "decision": Optional[Decision]}
_pending: Dict[str, dict] = {}
# task_id -> {("exec",) | ("actuate",) | ...}：任务级类记忆（不落盘、不跨任务）
_memory: Dict[str, Set[Tuple[str, ...]]] = {}


class SseApprovalSink(policy.ApprovalSink):
    """阻塞等待人工决议的审批 sink（工具线程在 ``Event.wait`` 上挂起）。"""

    def request(self, card: ApprovalCard) -> Decision:
        event = threading.Event()
        rec = {"card": card, "event": event, "decision": None}
        with _lock:
            _pending[card.approval_id] = rec
        push_approval(card, task_id=card.task_id)
        wait = max(0.0, float(card.wait_seconds or 0))
        signaled = event.wait(wait if wait > 0 else None)  # 0 = 无限等
        with _lock:
            _pending.pop(card.approval_id, None)
            decision = rec.get("decision")
        if decision is None:
            # 超时（fail-closed）；cancelled 路径已带 decision
            decision = Decision(approved=False, rule="timeout")
            push_approval_resolved(card.approval_id, "timeout", card.task_id)
            self._mirror_audit(card, "timeout", decision.rule)
        return decision

    def resolve(self, approval_id: str, action: str, remember: bool) -> bool:
        """REST 决议入口（approvals_api 调）。返回 False = 卡不存在或已决议。"""
        approved = action == "approve"
        with _lock:
            rec = _pending.get(approval_id)
            if rec is None:
                return False
            card = rec["card"]
            rec["decision"] = Decision(approved=approved,
                                       rule="" if approved else "user_deny")
            rec["event"].set()
            _pending.pop(approval_id, None)
        outcome = "approved" if approved else "user_deny"
        push_approval_resolved(approval_id, outcome, card.task_id)
        self._mirror_audit(card, outcome, "" if approved else "user_deny")
        return True

    # --- 任务级类记忆（一次同意覆盖该类全部工具；仅本任务） ---
    def remembered(self, task_id: str, key: Tuple[str, ...]) -> bool:
        with _lock:
            return key in _memory.get(task_id, set())

    def remember(self, task_id: str, key: Tuple[str, ...]) -> None:
        with _lock:
            _memory.setdefault(task_id, set()).add(key)

    @staticmethod
    def _mirror_audit(card: ApprovalCard, decision: str, rule: str) -> None:
        """门干预镜像到 debug 通道（kind=audit），右栏运行日志实时可见。"""
        try:
            push_chat("system", f"[审计] {card.tool or card.risk} → {decision}",
                      extra={"kind": "audit", "payload": {
                          "approval_id": card.approval_id, "tool": card.tool,
                          "unit": card.unit, "risk": card.risk,
                          "decision": decision, "rule": rule,
                      }},
                      debug=True, task_id=card.task_id)
        except Exception:
            pass


def cancel_task_approvals(task_id: str) -> None:
    """任务终态：该 task 全部 pending 置 cancelled（唤醒线程）+ 清任务级审批记忆。"""
    items = []
    with _lock:
        for aid in [a for a, rec in _pending.items() if rec["card"].task_id == task_id]:
            rec = _pending.pop(aid)
            rec["decision"] = Decision(approved=False, rule="cancelled")
            rec["event"].set()
            items.append((aid, rec["card"]))
        _memory.pop(task_id, None)
    for aid, card in items:
        push_approval_resolved(aid, "cancelled", card.task_id)


def get_sink() -> Optional[SseApprovalSink]:
    """当前注册的 sink（未安装返回 None；内核侧会走 AutoDeny）。"""
    sink = policy._get_sink()
    return sink if isinstance(sink, SseApprovalSink) else None


def install_approval_sink() -> None:
    """后端启动注册（server.py lifespan 调用）。"""
    policy.set_sink(SseApprovalSink())


def running_task_id_compat() -> str:
    """供测试/诊断：当前运行中的 task_id（helpers 同源）。"""
    return _running_task_id() or ""
