"""S2 后端链路端到端用例（验收 §9.4 后端半边）。

覆盖真实组装：内核门（真实 call_tool，与 SDK Runner 同一 invoke 路径）
→ SseApprovalSink（TestClient lifespan 注册）
→ SSE/outbox `approval` 事件 + `/live` approvals 通道（刷新恢复）
→ REST `POST /approvals/{id}/decision` 决议
→ 工具放行 / 结构化拒绝；任务终态 `_finish_task` cancel 唤醒阻塞线程。
中间不含任何 mock 门。

线程模型（与生产同构）：绑定（policy.bind / set_task）与 call_tool 必须在
**同一线程**——async bridge 经 `call_soon_threadsafe` 的 Handle 捕获调用线程的
context，绑定点和派发点跨线程会导致上下文丢失（生产中 graph_runner 绑定后
同线程派发，故成立）。
"""
import os
import threading
import time

import pytest

from omni_core.local import runtime_paths as RP
from omni_core.tools import policy
from omni_core.tools.base import call_tool
from omni_core.tools.workspace import set_task

_SHELL = "cmd" if os.name == "nt" else "bash"
_WAIT_SEC = 15  # 兜底超时：卡住时按 timeout 拒绝退出，测试不至于挂死


def _run_tool_in_thread(task_id: str, tool: str, args: dict) -> tuple:
    """在独立线程里绑定安全上下文并直调工具（模拟 run 内派发），返回 (thread, 结果盒)。"""
    results: dict = {}

    def run():
        policy.bind(task_id=task_id, full_access=False,
                    security_cfg={"approval": {"wait_seconds": _WAIT_SEC}})
        set_task(task_id)
        RP.ensure_task_dirs(task_id)
        try:
            results["res"] = call_tool(tool, args)
        except Exception as e:  # 意外异常也要落进结果盒，别让 join 悬空
            results["res"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            set_task(None)
            policy.unbind()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, results


def _wait_live_approval(client, task_id: str, timeout: float = 10.0) -> dict:
    """轮询 `/live` approvals 通道直到出现待批卡（同时验证刷新恢复面）。"""
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        r = client.get("/api/runtime/live", params={"task_id": task_id})
        items = (r.json() or {}).get("approvals") or []
        if items:
            return items[-1]
        last = items
        time.sleep(0.05)
    raise AssertionError(f"等待审批卡超时（/live approvals 为空: {last}）")


def test_approval_flow_end_to_end(client):
    """推卡 → /live 可见 → REST 允许(含 remember) → 工具真实执行；决议后卡收起。"""
    t, results = _run_tool_in_thread(
        "t_e2e1", "shell_exec", {"command": "echo e2e_ok", "shell": _SHELL})
    try:
        card = _wait_live_approval(client, "t_e2e1")
        assert card["tool"] == "shell_exec" and card["risk"] == "exec"
        assert card["arguments"].get("command") == "echo e2e_ok"
        assert card["task_id"] == "t_e2e1"
        assert card["wait_seconds"] == _WAIT_SEC
        # SSE outbox 里有推卡事件（前端顶置卡的数据源）
        box = __import__("backend.api.runtime_manager", fromlist=["manager"]).manager \
            .ensure_outbox("t_e2e1")
        assert any(m.get("approval_id") == card["approval_id"] for m in box["approval"])
        # 决议：允许 + 任务级记忆
        r = client.post(f"/api/runtime/approvals/{card['approval_id']}/decision",
                        json={"action": "approve", "remember": True})
        assert r.json().get("ok") is True, r.json()
        t.join(_WAIT_SEC + 5)
        assert not t.is_alive(), "决议后工具线程应被唤醒"
        res = results.get("res") or {}
        assert res.get("ok") is True, res
        # 决议后 /live 不再有该卡；outbox 有 approved 的 resolved 事件
        r2 = client.get("/api/runtime/live", params={"task_id": "t_e2e1"})
        live_now = (r2.json() or {}).get("approvals") or []
        assert all(a["approval_id"] != card["approval_id"] for a in live_now)
        box2 = __import__("backend.api.runtime_manager", fromlist=["manager"]).manager \
            .ensure_outbox("t_e2e1")
        assert any(m.get("approval_id") == card["approval_id"] and m.get("outcome") == "approved"
                   for m in box2["approval_resolved"])
        # remember（类级）：同任务第二次 exec 不再产生卡片，直接执行
        t2, results2 = _run_tool_in_thread(
            "t_e2e1", "shell_exec", {"command": "echo e2e_second", "shell": _SHELL})
        t2.join(_WAIT_SEC + 5)
        assert results2["res"].get("ok") is True, results2["res"]
        r3 = client.get("/api/runtime/live", params={"task_id": "t_e2e1"})
        assert ((r3.json() or {}).get("approvals") or []) == []
    finally:
        t.join(5)


def test_deny_flow_returns_structured_refusal(client):
    """REST 拒绝 → 工具收到结构化拒绝（denied/rule/hint），agent 循环可继续。"""
    t, results = _run_tool_in_thread(
        "t_e2e2", "shell_exec", {"command": "echo denied", "shell": _SHELL})
    try:
        card = _wait_live_approval(client, "t_e2e2")
        r = client.post(f"/api/runtime/approvals/{card['approval_id']}/decision",
                        json={"action": "deny"})
        assert r.json().get("ok") is True
        t.join(_WAIT_SEC + 5)
        assert not t.is_alive()
        res = results.get("res") or {}
        assert res.get("ok") is False and res.get("denied") is True
        assert res.get("rule") == "user_deny" and res.get("hint")
    finally:
        t.join(5)


def test_task_finish_cancels_pending_and_wakes_thread(client):
    """任务终态：`_finish_task` 把 pending 卡置 cancelled、唤醒阻塞线程、清任务级记忆。"""
    t, results = _run_tool_in_thread(
        "t_e2e3", "shell_exec", {"command": "echo cancelled", "shell": _SHELL})
    try:
        card = _wait_live_approval(client, "t_e2e3")
        from backend.api.routers.helpers import _finish_task
        _finish_task("t_e2e3")
        t.join(_WAIT_SEC + 5)
        assert not t.is_alive(), "终态 cancel 必须唤醒阻塞线程（否则任务卡死）"
        res = results.get("res") or {}
        assert res.get("denied") is True and res.get("rule") == "cancelled"
        # 该卡已不在 pending：REST 决议 404
        r = client.post(f"/api/runtime/approvals/{card['approval_id']}/decision",
                        json={"action": "approve"})
        assert r.status_code == 404
    finally:
        t.join(5)


def test_decision_endpoint_validation(client):
    """决议端点：未知卡 404；非法 action 400。"""
    r = client.post("/api/runtime/approvals/nope/decision", json={"action": "approve"})
    assert r.status_code == 404
    r2 = client.post("/api/runtime/approvals/nope/decision", json={"action": "hmm"})
    assert r2.status_code == 400


def test_audit_endpoint_returns_recent_entries(client):
    """审计只读端点：读隔离后的审计 jsonl，返回尾部条目（时间正序）。"""
    policy.bind(task_id="t_aud_api")
    try:
        policy.audit("approved", "user_approve", tool="shell_exec", risk="exec",
                     arguments={"command": "audit probe"})
        r = client.get("/api/runtime/audit", params={"limit": 50})
        assert r.status_code == 200
        items = (r.json() or {}).get("items") or []
        assert any(e.get("task_id") == "t_aud_api" for e in items)
        assert all({"ts", "decision", "tool", "arguments"} <= set(e) for e in items)
    finally:
        policy.unbind()
