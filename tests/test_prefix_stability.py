"""钉前缀回归锁：同一会话连续两个 run 的 system 必须逐字节一致。

2026-11-11：把唯一逐 run 变化的 <current-context> 移出 system（改由当轮
user 输入携带）。本测试用两个不同的处境信号跑两次 _run_via_sdk，锁死
「system 不随 run 变化」，并确认动态信号确实进了 user 输入。
"""
import json
import types

import config as config_mod
from omni_core.brain import sdk_loop as sl
from omni_core.local import knowledge_inject as ki
from omni_core.local import scene_executor
from omni_core.local.loop import ToolLoop
from omni_core.local.loop import context_signal as cs


class _Rec:
    def __init__(self):
        self.thinks = []

    def log_think(self, content, role="", model=""):
        self.thinks.append({"content": content, "role": role, "model": model})

    def records(self, kind):
        out = []
        for t in self.thinks:
            try:
                d = json.loads(t["content"])
            except Exception:
                continue
            if d.get("kind") == kind:
                out.append(d)
        return out


def _spec():
    return types.SimpleNamespace(
        objective="o", done_when="", expected=None, task_id="prefix",
        project_id=None, max_steps=None, history=[], corrections=[],
        task_mode="oneshot", attachments=[], run_key="rk1",
    )


def test_system_is_byte_identical_across_runs(monkeypatch):
    table = {"runtime.long_task.inject_instructions": True,
             "runtime.long_task.instructions_limit": 8192}
    monkeypatch.setattr(config_mod, "get_config", lambda p, d=None: table.get(p, d))
    monkeypatch.setattr(scene_executor, "load_scene_nav", lambda pid, **k: [])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "")
    # build_run 不清空手工构造的信号；render 每 run 给不同的处境
    monkeypatch.setattr(cs, "build_run", lambda **k: dict(cs._CURRENT))
    notes = ["<current-context>\ncontext=group:1\npeer=甲\n</current-context>",
             "<current-context>\ncontext=group:2\npeer=乙\n</current-context>"]
    monkeypatch.setattr(cs, "render", lambda *a, **k: notes.pop(0))

    captured = []

    def _sdk(*a, **k):
        captured.append(k)
        return {"success": True, "reason": "ok", "steps": 1, "escalated": False,
                "escalate_reason": "", "provider_error": False, "dispatch_plan": []}

    monkeypatch.setattr(sl, "run_subtask_sdk", _sdk)
    loop = ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
                    verbose=False)
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True

    for _ in range(2):
        loop._run_via_sdk(_spec(), {"model": "m"}, "SYS", types.SimpleNamespace(),
                          traj=_Rec(), user_input="hi")

    assert len(captured) == 2
    # 处境信号必须一路传到压缩层，超长任务后半程才能重新贴回
    assert captured[0].get("context_note") and captured[1].get("context_note")
    assert captured[0]["context_note"] != captured[1]["context_note"]
    assert captured[0]["instructions"] == captured[1]["instructions"], \
        "静态前缀跨 run 必须逐字节一致"
    assert "<current-context>" not in captured[0]["instructions"]
    assert str(captured[0]["user_input"]) != str(captured[1]["user_input"]), \
        "动态处境应随每轮输入变化，而不是消失"


def test_fingerprint_covers_context_note_and_schema():
    from omni_core.local.loop.parts import _request_fingerprint

    base = _request_fingerprint("SYS", ["a"], 0)
    assert base != _request_fingerprint("SYS", ["a"], 0, context_note="CTX")
    assert base != _request_fingerprint("SYS", ["a"], 0,
                                        tool_schemas=[{"name": "a", "params_json_schema": "{}"}])
    # 工具名顺序仍不影响（既有回归锁口径）
    assert _request_fingerprint("SYS", ["a", "b"], 0) == \
           _request_fingerprint("SYS", ["b", "a"], 0)
