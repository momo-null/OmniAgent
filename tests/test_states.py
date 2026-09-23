"""M4a.4 Agent 状态机测试：状态序列合法（不重复行为，仅追踪）。

复用 test_m3b 的假大脑 / 假后端 pattern（monkeypatch）。
"""
import json

from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.local.tool_loop import ToolLoop, TaskSpec
from omni_core.local.states import AgentState, LEGAL_TRANSITIONS


class _FakeBackend:
    tool_schemas = []

    def __init__(self, *a, **k):
        self.observe_calls = 0
        self.ocr = []
        self.backend = _FakeBackendInner()  # M4a.3: 可配置 OCR 文本
        self.backend_kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

    def observe(self):
        self.observe_calls += 1
        return {"active_window": "com.fake.game", "ocr_text": list(self.ocr)}

    def screenshot(self):
        return {"ok": True, "path": "none"}

    def read_screen_text(self):
        return {"ok": True, "ocr_text": list(self.ocr)}

    def execute_mouse_action(self, action, args):
        return True

    def execute_keyboard_action(self, action, args):
        return True


def _fake_brain_factory(mapping):
    class _FB:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.model = cfg.get("model", "")
            self._script = list(mapping.get(self.model, []))
            self.calls = 0

        def chat(self, messages, tools=None, tool_choice="auto"):
            self.calls += 1
            if self._script:
                return self._script.pop(0)
            return BrainReply(tool_calls=[ToolCall(name="task_done", args={"reason": "ok"}, id="t")], finish_reason="stop")

        def close(self):
            pass

    return _FB


BRAIN = "demo-model"
EXEC = "qwen3.5-4b-vl"


def _dispatch(items):
    """M7：主 agent 派发子任务。"""
    return BrainReply(tool_calls=[ToolCall(name="dispatch", args={"items": json.dumps(items)}, id="d")], finish_reason="stop")


def _task_done():
    return BrainReply(tool_calls=[ToolCall(name="task_done", args={"reason": "ok"}, id="t")], finish_reason="stop")


def _observe():
    """新架构：大脑自主调 observe 获取屏幕状态。"""
    return BrainReply(tool_calls=[ToolCall(name="observe", args={}, id="o1")], finish_reason="stop")


def _think():
    """无工具调用、仅自然语言——内层 loop 视作思考备注继续，步数耗尽则 FAILED。"""
    return BrainReply(content="思考中", tool_calls=[], finish_reason="stop")


def _make_loop(monkeypatch, tmp_path, brain_mapping, executor_mapping=None):
    fb = _fake_brain_factory({BRAIN: brain_mapping, EXEC: executor_mapping or []})
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", fb)
    monkeypatch.setattr("omni_core.local.loop.core.ExecutionModule", _FakeBackend)
    loop = ToolLoop(
        {"model": BRAIN, "base_url": "http://x", "capabilities": {}},
        verbose=False,
        executor_cfg={"enabled": True, "model": EXEC, "base_url": "http://x", "capabilities": {"vision": True}},
    )
    loop.traj_dir = str(tmp_path)
    return loop


def _assert_legal(seq):
    states = [AgentState(s) for s in seq]
    assert states[0] == AgentState.INIT
    for a, b in zip(states, states[1:]):
        assert b in LEGAL_TRANSITIONS[a], f"非法转移 {a} -> {b}"


def test_single_brain_success_sequence(monkeypatch, tmp_path):
    loop = _make_loop(monkeypatch, tmp_path, brain_mapping=[_task_done()])
    res = loop.run_task(TaskSpec(objective="o", max_steps=3))
    assert res["success"] is True
    assert loop._state_seq[0] == "INIT"
    assert loop._state_seq[-1] == "DONE"
    _assert_legal(loop._state_seq)


def test_single_brain_failure_sequence(monkeypatch, tmp_path):
    # 大脑一直只思考不 task_done -> max_steps 用尽 -> FAILED（单大脑无 escalate）
    loop = _make_loop(monkeypatch, tmp_path, brain_mapping=[_think(), _think()])
    res = loop.run_task(TaskSpec(objective="o", max_steps=2))
    assert res["success"] is False
    assert loop._state_seq[-1] == "FAILED"
    _assert_legal(loop._state_seq)


def test_two_layer_no_planning_state(monkeypatch, tmp_path):
    """M7 去分层：没有独立的「规划」阶段，主 agent 直接执行（EXECUTING）。"""
    loop = _make_loop(
        monkeypatch, tmp_path,
        brain_mapping=[_dispatch([{"desc": "a", "done_when": "A"}]), _task_done()],
        executor_mapping=[_observe(), _task_done()],
    )
    loop.exec.ocr = ["A"]  # M4a.3: observe 后屏幕含 done_when → verify 通过
    res = loop.run_task(TaskSpec(objective="o"))
    assert res["success"] is True
    assert "PLANNING" not in loop._state_seq   # 不再有固定规划阶段
    assert "EXECUTING" in loop._state_seq
    assert loop._state_seq[0] == "INIT"
    assert loop._state_seq[-1] == "DONE"
    _assert_legal(loop._state_seq)


class _FakeBackendInner:
    """模拟真实执行后端（self.exec.backend 调用点）。"""
    def verify_done(self, cond: str, percept: dict):
        ocr = (percept or {}).get("ocr_text") or []
        return (any(cond and cond in str(o) for o in ocr), "ok" if cond else "no")
    def text_of(self, percept: dict) -> str:
        return " ".join((percept or {}).get("ocr_text") or [])

