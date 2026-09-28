"""M8：长任务节奏控制（T2 预算感知提示）。

原 HITL 双通道（软注入/硬停止）与压缩归黑板用例建于手搓 LLMClient 链路，
SDK 迁移后失真，随 test_m3b 一并移除（2026-09-28）；
语义覆盖见 test_t51_wake / test_t32_compaction_model。
"""

from agents.tool import function_tool as sdk_function_tool

from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.brain.sdk_loop import build_budget_hint, run_subtask_sdk

BRAIN = "demo-model"


def ping() -> dict:
    """空操作（测试用：只为了让循环产生步数）。"""
    return {"ok": True}


class _StubGate:
    """最小 L2 门控替身（只提供 run_subtask_sdk 需要的接口）。"""

    has_condition = False
    verify_count = 0
    no_confidence = False

    def verify_done(self):
        return False, "no"

    def verify(self, condition: str = ""):
        return False, "no"

    def note(self, text: str = ""):
        return {"ok": True}

    def peek(self):
        return False, ""


class _AlwaysToolBrain:
    """每次都调同一个工具（制造步数），并记录每轮的用户消息。"""

    def __init__(self, cfg, timeout=180.0, on_debug=None):
        self.model = cfg.get("model", "")
        self.calls = 0
        self.user_texts: list = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        self.user_texts.append(" ".join(
            str(m.get("content", "")) for m in (messages or []) if m.get("role") == "user"
        ))
        return BrainReply(tool_calls=[ToolCall(name="ping", args={}, id=f"c{self.calls}")],
                          finish_reason="stop")

    def close(self):
        pass


# === T2 预算感知提示 =======================================================
def test_budget_hint_is_factual_not_prescriptive():
    """措辞红线：只陈述事实 + 给出可选动作，禁止「请尽快/请加速」这类价值判断。"""
    text = build_budget_hint(8, 10)
    assert "8/10" in text
    assert "escalate" in text            # 给出可选动作
    for banned in ("尽快", "加速", "立刻", "必须"):
        assert banned not in text, f"提示里出现了诱导性措辞: {banned}"


def test_budget_hint_injected_once_at_threshold():
    """用到 ratio（0.5）时注入提示，且只注入一次。"""
    brain = _AlwaysToolBrain({"model": BRAIN})
    res = run_subtask_sdk(
        brain,
        instructions="i",
        user_input="u",
        tools=[ping],
        gate=_StubGate(),
        max_steps=10,
        budget_hint_ratio=0.5,
    )
    assert res["success"] is False and "budget_exhausted" in res["reason"]
    hits = [t for t in brain.user_texts if "预算提示" in t]
    assert len(hits) == 1, f"提示应恰好注入一次，实际: {hits}"


def test_no_budget_hint_when_disabled():
    brain = _AlwaysToolBrain({"model": BRAIN})
    run_subtask_sdk(
        brain, instructions="i", user_input="u", tools=[ping],
        gate=_StubGate(), max_steps=6, budget_hint_ratio=0.0,
    )
    assert not any("预算提示" in t for t in brain.user_texts)