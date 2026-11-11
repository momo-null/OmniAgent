"""定时任务走 Web chat 链路的回归锁（2026-11-11）。

定时任务不是外部调用：与人工聊天共用 _dispatch_chat 轮次（因此同样落
extra.meta / 完成信息条），但进料道标 task、不伪装成聊天原话。
"""
import threading

from backend.api.routers import chat_runtime as cr
from gateway import scheduler as sched


class _FakeTaskStore:
    meta: dict = {}
    updated = None

    @classmethod
    def create(cls, objective, done_when="", project_id=None, session_id=None, title=""):
        cls.meta = {"task_id": "t_sched", "project_id": project_id or "default",
                    "session_id": "s_sched", "objective": objective,
                    "done_when": done_when, "title": title}
        return cls.meta

    @classmethod
    def update(cls, *a, **kw):
        cls.updated = (a, kw)


class _FakeProjectStore:
    appended: list = []

    @classmethod
    def append_message(cls, *a, **kw):
        cls.appended.append((a, kw))


class _FakeThread:
    def __init__(self, *a, **kw):
        pass

    def start(self):
        pass


def test_launch_scheduled_turn_uses_chat_pipeline(monkeypatch):
    _FakeProjectStore.appended = []
    monkeypatch.setattr(cr, "_task_store", lambda: _FakeTaskStore)
    monkeypatch.setattr(cr, "_project_store", lambda: _FakeProjectStore)
    monkeypatch.setattr(cr, "_try_start_task", lambda *a, **kw: True)
    monkeypatch.setattr(cr, "mark_turn_start", lambda *a: None)
    monkeypatch.setattr(cr, "push_chat", lambda *a, **kw: None)
    captured, done = {}, threading.Event()

    def _fake_dispatch(*args):
        captured["args"] = args
        done.set()

    monkeypatch.setattr(cr._rt, "_dispatch_chat", _fake_dispatch)

    task_id = cr.launch_scheduled_turn("生成今天的 today.json", project_id="life",
                                       max_steps=12)
    assert task_id == "t_sched"
    assert done.wait(2), "Web chat 派发线程未启动"
    args = captured["args"]
    assert args[0] == "t_sched"
    assert args[1] == [{"role": "user", "content": "生成今天的 today.json"}]
    assert args[2] == 12
    assert args[6] == "task"       # mode
    assert args[10] == "task"      # source_kind：非聊天原话
    assert _FakeTaskStore.meta["project_id"] == "life"
    assert _FakeProjectStore.appended[0][1].get("l0_kind") == "task"


def test_submit_via_web_posts_non_external_payload(monkeypatch):
    calls = {}

    class _Resp:
        ok = True

        def json(self):
            return {"ok": True, "task_id": "t_web"}

    monkeypatch.setattr(sched, "_in_backend_process", lambda: False)
    monkeypatch.setattr(sched, "record_run",
                        lambda *a, **kw: calls.setdefault("rec", (a, kw)))
    monkeypatch.setattr(sched.threading, "Thread", _FakeThread)

    import requests

    def _post(url, json=None, timeout=None):
        calls.update({"url": url, "payload": json})
        return _Resp()

    monkeypatch.setattr(requests, "post", _post)

    tid = sched.submit_via_web("目标", 0, 0, source="cron:j", project_id="life",
                               notify_channel="", job_id="j")
    assert tid == "t_web"
    assert calls["url"].endswith("/api/runtime/internal/scheduled_turn")
    assert calls["payload"]["source_kind"] == "task"
    assert calls["payload"]["project_id"] == "life"
    assert calls["payload"]["mode"] == "task"
    assert "source_external" not in calls["payload"]
    assert calls["rec"][0][0] == "j" and calls["rec"][0][2] == "running"


def test_submit_via_web_falls_back_when_backend_down(monkeypatch):
    import requests
    from gateway import dispatch

    monkeypatch.setattr(sched, "_in_backend_process", lambda: False)
    monkeypatch.setattr(sched, "record_run", lambda *a, **kw: None)
    monkeypatch.setattr(sched.threading, "Thread", _FakeThread)

    def _boom(*a, **kw):
        raise OSError("backend down")

    monkeypatch.setattr(requests, "post", _boom)
    monkeypatch.setattr(dispatch, "submit", lambda *a, **kw: "t_fallback")
    assert sched.submit_via_web("目标", job_id="j") == "t_fallback"


def test_run_now_uses_web_pipeline(monkeypatch):
    monkeypatch.setattr(sched, "load_jobs", lambda: [
        {"id": "j", "objective": "目标", "project_id": "life", "max_steps": 8}])
    monkeypatch.setattr(sched, "_channel_gate", lambda job: None)
    captured = {}

    def _submit(objective, done_when="", max_steps=40, **kw):
        captured.update({"objective": objective, "max_steps": max_steps, **kw})
        return "t_now"

    monkeypatch.setattr(sched, "submit_via_web", _submit)
    assert sched.run_now("j") == "t_now"
    assert captured["objective"] == "目标"
    assert captured["project_id"] == "life"
    assert captured["source"] == "manual:j"
