"""M8：长任务节奏控制 + HITL 双通道（doc/plans/multi-agent-redesign-2026-09-13.md §5/§7）。

1. T2 预算感知提示：用到 ratio 时注入一条**陈述性**提示，只提示一次
2. T1 压缩产物归黑板：带 source（兄弟分支也能看见被压掉的上下文）
3. 软注入保真：运行中插入人类指示，不打断当前 turn，历史/预算/世界模型不丢
4. 硬停止：cancel 正在跑的编排 Task，立即退出
"""
import threading
import time

from agents.tool import function_tool as sdk_function_tool

from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.brain.sdk_loop import build_budget_hint, run_subtask_sdk
from omni_core.local.tool_loop import ToolLoop, TaskSpec

BRAIN = "demo-model"
EXEC = "qwen3.5-4b-vl"


@sdk_function_tool(strict_mode=False, failure_error_function=None)
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


# === T1 压缩产物归黑板（带来源） ===========================================
def test_compressed_facts_carry_source(monkeypatch, tmp_path):
    from omni_core.local.world_model import WorldModel
    import omni_core.local.runtime_paths as _RP

    _RP._GLOBAL = tmp_path / ".omniagent"
    _RP.ensure_global_dirs()

    loop = _make_loop(monkeypatch)
    world = WorldModel(task_id="t_m8")

    class _SummaryBrain:
        def chat(self, messages, tools=None, tool_choice="auto"):
            return BrainReply(content="已完成步骤一。已采集条目 A。", finish_reason="stop")

        def close(self):
            pass

    loop._compress_history(_SummaryBrain(), [[{"role": "user", "content": "x"}]],
                           world, source="sub:目标A")

    facts = [f for f in world.facts if f.startswith("[压缩提取]")]
    assert facts, "压缩摘要没有写回共享黑板"
    assert all(world.fact_source(f) == "sub:目标A" for f in facts)
    # 兄弟分支看不到本分支的私有事实（视图隔离仍然成立）
    assert world.facts_for(scope="sub:目标B") == []


# === 软注入 / 硬停止 =======================================================
def _make_loop(monkeypatch, brain_mapping=None, executor_mapping=None, ocr=None,
               backend_class=None):
    from tests.test_m3b import _fake_brain_factory, _FakeBackend

    fb = _fake_brain_factory({
        BRAIN: brain_mapping if brain_mapping is not None else [],
        EXEC: executor_mapping if executor_mapping is not None else [],
    })
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", fb)
    monkeypatch.setattr("omni_core.local.loop.core.ExecutionModule",
                        backend_class or _FakeBackend)
    loop = ToolLoop(
        {"model": BRAIN, "base_url": "http://x", "capabilities": {}},
        verbose=False,
        executor_cfg={"enabled": True, "model": EXEC, "base_url": "http://y", "capabilities": {}},
    )
    if ocr is not None:
        loop.exec.ocr = ocr
    return loop


class _SlowBackend:
    """让子 agent 跑得慢一点，给主线程留出注入窗口。"""

    def __init__(self, *a, **k):
        self.observe_calls = 0
        self.ocr: list = []
        self.backend = _SlowInner()
        self.backend_kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

    def observe(self):
        self.observe_calls += 1
        time.sleep(2.0)          # 给主线程留出注入 / 停止的时间窗
        return {"active_window": "com.fake", "ocr_text": list(self.ocr)}

    def screenshot(self):
        return {"ok": True, "path": "none"}

    def read_screen_text(self):
        return {"ok": True, "ocr_text": list(self.ocr)}

    def execute_mouse_action(self, action, args):
        return True

    def execute_keyboard_action(self, action, args):
        return True


class _SlowInner:
    def verify_done(self, cond: str, percept: dict):
        ocr = (percept or {}).get("ocr_text") or []
        return any(cond and cond in str(o) for o in ocr), "ok"

    def text_of(self, percept: dict) -> str:
        return " ".join((percept or {}).get("ocr_text") or [])


def test_soft_injection_reaches_main_without_interrupting(monkeypatch, tmp_path):
    """运行中注入人类指示：不打断当前 turn，主 agent 下一轮可见，历史与预算保留。"""
    import omni_core.local.runtime_paths as _RP
    from tests.test_m3b import _dispatch, _task_done, _auto_observe_then_done, _fake_brain_factory

    _RP._GLOBAL = tmp_path / ".omniagent"
    _RP.ensure_global_dirs()

    seen_messages: list = []

    factory = _fake_brain_factory({
        BRAIN: [_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("all ok")],
        EXEC: [_auto_observe_then_done("sub ok")],
    })

    class _RecFactory(factory):
        def chat(self, messages, tools=None, tool_choice="auto"):
            if self.model == BRAIN:
                seen_messages.append(" ".join(
                    str(m.get("content", "")) for m in (messages or []) if m.get("role") == "user"
                ))
            return super().chat(messages, tools, tool_choice)

    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _RecFactory)
    monkeypatch.setattr("omni_core.local.loop.core.ExecutionModule", _SlowBackend)

    loop = ToolLoop(  # noqa: F821
        {"model": BRAIN, "base_url": "http://x", "capabilities": {}},
        verbose=False,
        executor_cfg={"enabled": True, "model": EXEC, "base_url": "http://y", "capabilities": {}},
    )
    loop.exec.ocr = ["S1", "ALL"]

    holder = {}

    def _runner():
        holder["res"] = loop.run_task(TaskSpec(objective="o", done_when="ALL"))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    time.sleep(0.8)                                   # 子 agent 正在跑 → 运行中注入
    injected = loop.inject_message("先别点设置，先做别的")
    t.join(30)

    assert injected is True, "软注入失败（图未运行或没有 checkpointer）"
    assert holder["res"]["success"] is True           # 注入没有破坏本轮执行
    assert any("先别点设置" in m for m in seen_messages), "主 agent 没有看到注入的人类指示"


def test_hard_stop_cancels_running_graph(monkeypatch, tmp_path):
    """硬停止：cancel 正在跑的编排 Task，立即退出且不算异常。"""
    import omni_core.local.runtime_paths as _RP
    from tests.test_m3b import _dispatch, _task_done, _auto_observe_then_done

    _RP._GLOBAL = tmp_path / ".omniagent"
    _RP.ensure_global_dirs()

    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("all ok")],
        executor_mapping=[_auto_observe_then_done("sub ok")],
        ocr=["S1", "ALL"],
        backend_class=_SlowBackend,
    )

    holder = {}

    def _runner():
        holder["res"] = loop.run_task(TaskSpec(objective="o", done_when="ALL"))

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    time.sleep(0.8)                                   # 子 agent 正在跑 → 硬停止
    loop.request_stop()
    t.join(30)

    res = holder.get("res") or {}
    assert res.get("success") is False
    assert "停止" in (res.get("reason") or ""), res
    assert "编排异常" not in (res.get("reason") or "")
