"""运行管线公共装配层回归（P1~P3）。"""
from datetime import datetime, timedelta

from harness import run_pipeline


def _ts(minutes_ago: float) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).isoformat()


def test_build_history_filters_silent_and_old_and_limits():
    recs = [
        {"role": "user", "content": "旧话", "ts": _ts(120)},
        {"role": "assistant", "content": "静默", "ts": _ts(100),
         "extra": {"silent": True}},
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "新话", "ts": _ts(10)},
        {"role": "assistant", "content": "回答", "ts": _ts(5)},
    ]
    out = run_pipeline.build_history(
        recs, max_items=40, time_minutes=90, drop_silent=True,
        now_ts=datetime.now().timestamp())
    assert out == [{"role": "user", "content": "新话"},
                   {"role": "assistant", "content": "回答"}]


def test_build_history_limit_and_converter_keeps_system():
    recs = [{"role": "user", "content": str(i), "ts": _ts(1)} for i in range(20)]
    out = run_pipeline.build_history(recs, max_items=12,
                                     now_ts=datetime.now().timestamp())
    assert len(out) == 12 and out[0]["content"] == "8"

    seen = []

    def conv(items):
        seen.extend(items)
        return [{"role": r["role"], "content": r["content"]} for r in items]

    recs2 = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    out2 = run_pipeline.build_history(recs2, roles=None, converter=conv)
    assert out2 == [{"role": "system", "content": "s"},
                    {"role": "user", "content": "u"}]
    assert len(seen) == 2


def test_append_outbound_note(monkeypatch):
    import harness.outbound as ob

    monkeypatch.setattr(ob, "resolve", lambda channel, chat_id: {"ch": channel})
    monkeypatch.setattr(ob, "note", lambda route: f"守则:{route['ch']}")
    assert run_pipeline.append_outbound_note("目标", "web", "chat:1") == "目标\n守则:web"
    monkeypatch.setattr(ob, "note", lambda route: "")
    assert run_pipeline.append_outbound_note("目标", "web", "chat:1") == "目标"


def test_turn_recorder_upsert_partial_and_final(monkeypatch):
    from omni_core.local.task_store import ProjectStore

    calls = []

    def fake_append(project_id, session_id, role, content, task_id=None,
                    extra=None, context_key="", l0_kind="chat", speaker=""):
        calls.append({"project_id": project_id, "session_id": session_id,
                      "role": role, "content": content, "task_id": task_id,
                      "extra": extra, "context_key": context_key,
                      "l0_kind": l0_kind, "speaker": speaker})

    monkeypatch.setattr(ProjectStore, "append_message", staticmethod(fake_append))
    steps = []
    rec = run_pipeline.TurnRecorder(
        steps, project_id="p", session_id="s", task_id="t", run_key="rk",
        flush_every=2, append_extra={"context_key": "ck", "l0_kind": "chat",
                                    "speaker": "sp"})

    rec.add_thinking("brain", "思", "m")
    rec.add_thinking("brain", "思2", "m2", block_id="b1")
    rec.add_thinking("brain", "思2-更新", "m3", block_id="b1")
    assert steps[0] == {"type": "thinking", "model": "m", "content": "思"}
    assert [s for s in steps if s.get("id") == "b1"] == [
        {"type": "thinking", "id": "b1", "model": "m3", "content": "思2-更新"}]
    rec.add_tool_call("brain", "verify", "", "", "m")  # 内核门控，不进对话
    rec.add_tool_call("brain", "shell", "ls", "ok", "m")
    # 2 步阈值触发过 partial
    partials = [c for c in calls if (c["extra"] or {}).get("partial")]
    assert partials and partials[-1]["extra"]["run_key"] == "rk"
    assert partials[-1]["context_key"] == "ck" and partials[-1]["speaker"] == "sp"

    rec.record_final("结论", extra={"steps": [], "final": True})
    final = calls[-1]
    assert final["role"] == "assistant" and final["content"] == "结论"
    assert final["extra"]["steps"] == [] and final["extra"]["final"] is True
    assert final["extra"]["run_key"] == "rk"
