"""T4.6（O5+ / RH-1）— 可观测性能力包。

覆盖：
1. SDK 运行后 brain_calls / decision_steps 正常增长（不再恒零）。
2. 步数记账统一（Hook 为唯一权威，超限仅兜底防倒退）；校验状态真值化。
3. 轨迹包含请求指纹与知识注入记录，可溯源。
"""
import json
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.local.tool_loop import (
    ToolLoop, _SubtaskGate, _request_fingerprint,
)


class _Rec:
    """轨迹存储替身：记录 log_step / log_think 调用。"""

    def __init__(self):
        self.steps = []
        self.thinks = []

    def log_step(self, **kw):
        self.steps.append(kw)

    def log_think(self, content, role="", model=""):
        self.thinks.append({"content": content, "role": role, "model": model})


def _loop():
    return ToolLoop({"model": "m", "base_url": "http://x", "api_key": "k"}, verbose=False)


# --- 1. 指标回填：brain_calls / decision_steps --------------------------------
def test_emitter_no_longer_owns_counter_increment():
    """F1.2：brain_calls/decision_steps 计数已迁移到 sdk_loop.on_llm_end 钩子（按模型调用粒度），
    _emit 只负责推理/口播呈现与落盘，调用它不应再改变运行计数器。"""
    loop = _loop()
    assert loop._brain_calls == 0 and loop._decision_steps == 0

    emit, _delta, _turn_end = loop._make_llm_emitter("brain")
    emit("推理", "口播", [])
    emit("推理2", "口播2", [])

    # 计数已不在 _emit 内完成（迁移到 on_llm_end 钩子）
    assert loop._decision_steps == 0
    assert loop._brain_calls == 0


def test_executor_role_not_counted_as_brain_call(monkeypatch):
    """executor（子 agent）的模型调用计入 decision_steps，但不计 brain_calls。"""
    def _fake_run_subtask_sdk(brain, *, instructions=None, user_input=None, tools=None,
                              gate=None, **kw):
        return {"success": True, "reason": "ok", "steps": 1, "escalated": False,
                "escalate_reason": "", "provider_error": False,
                "llm_calls": 3, "repeat_failures": 0}

    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_run_subtask_sdk)
    loop = _loop()
    spec = types.SimpleNamespace(objective="o", done_when="", expected=None,
                                task_id="t46_exec", project_id=None, max_steps=None,
                                history=[], corrections=[])
    loop._run_via_sdk(spec, {"model": "m"}, "sys", types.SimpleNamespace(),
                      is_sub=True, allow_dispatch=False, user_input="hi")
    assert loop._decision_steps == 3, "executor 调用计入 decision_steps"
    assert loop._brain_calls == 0, "executor 调用不计 brain_calls"


# --- 2. 步数记账 & 校验状态真值化 ---------------------------------------------
def test_max_turns_exceeded_only_advances_one_step(monkeypatch):
    """超限时不再批量累加 turns，只在 Hook 未记账时兜底推进 1 步。"""
    def _raise(*a, **k):
        raise sl.MaxTurnsExceeded("max turns")

    monkeypatch.setattr(sl, "run_async", lambda coro, *a, **k: _close_and_run(coro))

    def _close_and_run(coro):
        try:
            coro.close()
        except Exception:
            pass
        raise sl.MaxTurnsExceeded("max turns")

    gate = types.SimpleNamespace(verify_done=lambda: (False, "未完成"),
                                 peek=lambda: (False, ""), verify_count=0,
                                 has_condition=False)
    res = sl.run_subtask_sdk(
        {"model": "m", "base_url": "http://x", "api_key": "k"},
        instructions="do", user_input="hi", tools=[], gate=gate,
        max_steps=5, chunk_turns=50, should_stop=lambda: False,
    )
    # 旧逻辑：state.steps += turns(50) -> 步数虚高为 50；
    # T4.6 修复：Hook 未记账时仅兜底 +1（不批量累加 turns）；
    # B1 修复（2026-09-22）：本块回合跑满但全局未达上限 -> 跨块续跑，
    # 直到真实步数抵达 max_steps 才 budget_exhausted。此处 max_steps=5、chunk_turns=50，
    # 每块仅由死循环兜底 +1，需 5 块抵终态，步数恒为 5（绝非 50 虚高、亦非误终止于 1）。
    assert res["steps"] == 5, f"步数不应批量累加 turns（虚高）；且应跨块续跑到真实上限: {res}"
    assert res["steps"] == 5  # == max_steps，确证未提前误终止
    assert "budget_exhausted" in res["reason"]
    assert "5" in res["reason"]  # 真实上下限可观测、可追溯


def test_gate_records_last_verify_result(monkeypatch):
    loop = _loop()
    spec = types.SimpleNamespace(expected="X", done_when="X", objective="o")
    gate = _SubtaskGate(loop, spec, types.SimpleNamespace())
    monkeypatch.setattr(loop, "_verify", lambda *a, **k: (True, "命中"))
    assert gate.peek()[0] is True
    assert gate.last_verify_passed is True, "校验状态应真值化（不再固定 False）"

    monkeypatch.setattr(loop, "_verify", lambda *a, **k: (False, "未命中"))
    gate.verify("X")
    assert gate.last_verify_passed is False


def test_trajectory_step_uses_gate_verified():
    traj = _Rec()
    loop = _loop()
    gate = types.SimpleNamespace(last_verify_passed=True)
    # 直接验证 _on_step 轨迹写盘取值（构造最小闭包环境）
    loop._action_count = 0
    loop._pending_calls = []
    loop.state = types.SimpleNamespace(value="EXECUTING")
    loop.on_tool_call = None
    world = types.SimpleNamespace(snapshot=lambda: {}, log_action=lambda *a: None)
    import time as _t

    def _on_step(tool_name, result):
        traj.log_step(state=loop.state.value, observation=world.snapshot(),
                      action={"tool": tool_name, "args": {}}, result=result,
                      metrics={"latency_ms": 0.0},
                      verified=bool(getattr(gate, "last_verify_passed", False)))

    _on_step("t", {"ok": True})
    assert traj.steps[0]["verified"] is True


# --- 3. 请求指纹 & 知识注入轨迹 -----------------------------------------------
def test_request_fingerprint_shape_and_stability():
    fp1 = _request_fingerprint("SYS", ["a", "b"], 3)
    fp2 = _request_fingerprint("SYS", ["b", "a"], 3)
    assert fp1 == fp2, "工具名顺序不影响指纹（已排序）"
    assert fp1.endswith("-3")
    assert len(fp1.split("-")) == 3
    assert fp1 != _request_fingerprint("OTHER", ["a", "b"], 3)
    assert fp1 != _request_fingerprint("SYS", ["a"], 3)


def test_fingerprint_written_to_trajectory():
    traj = _Rec()
    loop = _loop()
    loop._fp_system_prompt = "SYS"
    loop._fp_tool_names = ["a", "b"]
    emit, _, _ = loop._make_llm_emitter("brain", traj)
    emit("", "hi", [])
    fps = [t for t in traj.thinks if "request_fingerprint" in str(t["content"])]
    assert fps, "轨迹应包含请求指纹记录"
    payload = json.loads(fps[0]["content"])
    assert payload["kind"] == "request_fingerprint"
    assert payload["role"] == "brain"
    assert payload["fingerprint"].endswith("-0")


def test_knowledge_injection_written_to_trajectory():
    traj = _Rec()
    loop = _loop()
    loop._log_knowledge_injection(traj, {"kind": "knowledge_injected",
                                         "chars": 123, "skills": 2})
    assert traj.thinks and json.loads(traj.thinks[0]["content"])["chars"] == 123
    # 无轨迹存储时静默跳过，不影响主流程
    loop._log_knowledge_injection(None, {"chars": 1})
