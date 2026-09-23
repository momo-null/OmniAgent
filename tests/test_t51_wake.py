"""T5.1（SA-1 MVP）— 通用事件唤醒端点 POST /api/runtime/wake。

覆盖：
1. 唤醒暂停任务正常续跑，会话消息自动标注 [事件唤醒]。
2. 并发唤醒(409)、空参数(400)、非法 JSON(400)、不存在任务(404) 校验生效。
"""
import pytest

from backend.api import router_runtime as rr
from omni_core.local.task_store import TaskStore


@pytest.fixture()
def wake_env(monkeypatch):
    """拦截后台派发（不真跑 agent），并记录调用参数。"""
    captured = []
    monkeypatch.setattr(rr, "_dispatch_chat",
                        lambda task_id, messages, max_steps, full_access=None,
                        agent_id="main": captured.append(
                            {"task_id": task_id, "messages": messages,
                             "max_steps": max_steps, "agent_id": agent_id}))
    started = []

    def _cleanup():
        for tid in started:
            try:
                rr._finish_task(tid)
            except Exception:
                pass

    return captured, started, _cleanup


def _mk_task(state="paused"):
    meta = TaskStore.create(objective="被唤醒的任务")
    tid = meta["task_id"]
    TaskStore.update(tid, state=state)
    return tid


# --- 1. 正常唤醒 -------------------------------------------------------------
def test_wake_resumes_task_and_marks_message(client, wake_env):
    captured, started, cleanup = wake_env
    tid = _mk_task("paused")
    started.append(tid)

    resp = client.post("/api/runtime/wake", json={"task_id": tid, "message": "外部事件到达"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True and body.get("task_id") == tid

    # 会话消息标注准确（区分人工 / 事件触发）
    assert len(captured) == 1
    msgs = captured[0]["messages"]
    assert msgs[-1]["role"] == "user"
    assert msgs[-1]["content"] == "[事件唤醒] 外部事件到达"
    assert captured[0]["agent_id"] == "main"
    # 任务状态置为运行
    assert (TaskStore.get(tid) or {}).get("state") == "running"
    cleanup()


# --- 2. 校验与错误码 ----------------------------------------------------------
def test_wake_rejects_empty_task_id(client, wake_env):
    _, _, _ = wake_env
    resp = client.post("/api/runtime/wake", json={"task_id": "", "message": "x"})
    assert resp.status_code == 400, resp.text


def test_wake_rejects_empty_message(client, wake_env):
    tid = _mk_task("paused")
    resp = client.post("/api/runtime/wake", json={"task_id": tid, "message": "   "})
    assert resp.status_code == 400, resp.text


def test_wake_rejects_unknown_task(client, wake_env):
    resp = client.post("/api/runtime/wake",
                       json={"task_id": "no-such-task-id", "message": "x"})
    assert resp.status_code == 404, resp.text


def test_wake_rejects_invalid_json(client, wake_env):
    resp = client.post("/api/runtime/wake", content=b"not-json",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400, resp.text


def test_wake_conflicts_when_task_running(client, wake_env, monkeypatch):
    captured, started, cleanup = wake_env
    tid = _mk_task("paused")
    started.append(tid)
    # 模拟已有任务在运行（并发唤醒 / 自身运行中）
    monkeypatch.setattr(rr, "_is_any_running", lambda: True)
    monkeypatch.setattr(rr, "_running_task_id", lambda: tid)

    resp = client.post("/api/runtime/wake", json={"task_id": tid, "message": "再来一次"})
    assert resp.status_code == 409, resp.text
    assert captured == [], "冲突时不得下发执行"
    cleanup()


def test_wake_conflicts_on_self_running(client, wake_env, monkeypatch):
    """仅本任务自身在运行（全局无其他任务）时同样拒绝。"""
    captured, started, cleanup = wake_env
    tid = _mk_task("paused")
    started.append(tid)
    monkeypatch.setattr(rr, "_is_any_running", lambda: False)
    monkeypatch.setattr(rr, "_is_task_running", lambda task_id, agent_id="main": task_id == tid)

    resp = client.post("/api/runtime/wake", json={"task_id": tid, "message": "再来一次"})
    assert resp.status_code == 409, resp.text
    assert captured == []
    cleanup()
