"""B1（2026-09-22）：chunk 边界误判导致预算虚假耗尽 —— 修复验证。

根因：``run_subtask_sdk`` 的 ``MaxTurnsExceeded`` 分支在有硬上限时，只要本块回合跑满
就直接返回 ``budget_exhausted``，未校验全局真实步数 -> 一切带步数上限的任务在第一个
``chunk_turns`` 块边界被误终止（实机：上限 1000/2000，任务仅跑 94/97 步即谎报「已达步数上限」）。

修复后（sdk_loop.py 1164 行分支）：
- ``state.steps >= max_steps`` -> 真实预算耗尽，正常终止；
- ``state.steps < max_steps`` -> 仅单块回合跑满，跨块续跑下一块；
- reason 统一标注真实上下限（可观测、可追溯、可自动化断言）。

两个层面：
1) 长任务分块续跑：模型持续输出工具调用，跨多块续跑后抵达真实上限才终止。
2) 空转兜底：模型纯文本无记账，死循环兜底每块 +1，有限块内正常终止，无死循环。
"""
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import run_subtask_sdk


def _gate():
    return types.SimpleNamespace(
        verify_done=lambda: (False, "未完成"),
        peek=lambda: (False, ""),
        verify_count=0,
        has_condition=False,
    )


def _brain():
    return {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}


# --- 1. 长任务分块续跑（模型持续输出工具调用） -------------------------------
def test_chunk_continuation_until_real_budget(monkeypatch):
    """max_steps=100、chunk_turns=10：跨块续跑 >=9 次，步数达标才终止，reason 含真实上下限。"""
    calls = {"run": 0}

    class _FakeRunner:
        @staticmethod
        async def run(agent, items, max_turns=0, hooks=None, run_config=None):
            calls["run"] += 1
            # 模拟模型每轮都在用工具推进：触发 max_turns 次真实 hook 记账
            for _ in range(max_turns):
                await hooks.on_tool_end(None, agent, types.SimpleNamespace(name="do_work"), "ok")
            raise sl.MaxTurnsExceeded(f"max turns ({max_turns})")

    monkeypatch.setattr(sl, "Runner", _FakeRunner)

    res = run_subtask_sdk(
        _brain(), instructions="do", user_input="hi", tools=[],
        gate=_gate(), max_steps=100, chunk_turns=10, should_stop=lambda: False,
    )
    # 每块推进 10 步（chunk_turns），需 10 块抵满 100 步 -> 9 次续跑 + 1 次终态
    assert res["success"] is False
    assert "budget_exhausted" in res["reason"]
    assert res["steps"] == 100, f"应跨块续跑至真实上限 100，而非误终止: {res}"
    assert calls["run"] >= 10, f"应触发多块续跑（>=10 次 Runner 调用），实际 {calls['run']}"
    # reason 含真实上下限（修复谎报，可观测、可追溯）
    assert "已达步数上限 100" in res["reason"], res["reason"]


def test_chunk_continuation_small_chunk(monkeypatch):
    """chunk_turns 远小于 max_steps（复现实机 50/1000 形态）：必须跨多块而非首块误终止。"""
    calls = {"run": 0}

    class _FakeRunner:
        @staticmethod
        async def run(agent, items, max_turns=0, hooks=None, run_config=None):
            calls["run"] += 1
            for _ in range(max_turns):
                await hooks.on_tool_end(None, agent, types.SimpleNamespace(name="do_work"), "ok")
            raise sl.MaxTurnsExceeded(f"max turns ({max_turns})")

    monkeypatch.setattr(sl, "Runner", _FakeRunner)

    res = run_subtask_sdk(
        _brain(), instructions="do", user_input="hi", tools=[],
        gate=_gate(), max_steps=1000, chunk_turns=50, should_stop=lambda: False,
    )
    assert res["steps"] == 1000, f"应跨块续跑至真实上限 1000: {res}"
    assert calls["run"] >= 20, f"应触发大量块续跑（>=20 次），实际 {calls['run']}"
    assert "已达步数上限 1000" in res["reason"], res["reason"]


# --- 2. 空转兜底（纯文本无记账，死循环兜底每块 +1） --------------------------
def test_empty_spin_guard_no_deadlock(monkeypatch):
    """模型纯文本无记账：死循环兜底每块 +1，有限块内正常终止，无死循环（复现 T4.6 兜底）。"""
    calls = {"run": 0}

    def _close_and_run(coro):
        calls["run"] += 1
        try:
            coro.close()
        except Exception:
            pass
        raise sl.MaxTurnsExceeded("max turns")

    monkeypatch.setattr(sl, "run_async", lambda coro, *a, **k: _close_and_run(coro))

    res = run_subtask_sdk(
        _brain(), instructions="do", user_input="hi", tools=[],
        gate=_gate(), max_steps=100, chunk_turns=10, should_stop=lambda: False,
    )
    assert res["success"] is False
    assert "budget_exhausted" in res["reason"]
    # 无 hook 记账 -> 每块死循环兜底 +1，100 块抵终态；绝不能提前停在首块 10 步
    assert res["steps"] == 100, f"应跨块续跑到真实上限 100，而非误终止于首块: {res}"
    assert res["steps"] != 10, "不应在第一个块边界误终止"
    assert "100" in res["reason"]


def test_no_early_termination_before_real_limit(monkeypatch):
    """核心回归：修复前会在首块（chunk_turns=50）误报 budget_exhausted；修复后步数必抵 max_steps。"""
    calls = {"run": 0}

    def _close_and_run(coro):
        calls["run"] += 1
        try:
            coro.close()
        except Exception:
            pass
        raise sl.MaxTurnsExceeded("max turns")

    monkeypatch.setattr(sl, "run_async", lambda coro, *a, **k: _close_and_run(coro))

    res = run_subtask_sdk(
        _brain(), instructions="do", user_input="hi", tools=[],
        gate=_gate(), max_steps=200, chunk_turns=50, should_stop=lambda: False,
    )
    # 修复前：Runner 仅调用 1 次、steps≈1、立即 budget_exhausted（虚假耗尽）。
    # 修复后：跨块续跑，steps 必等于真实上限 200，Runner 调用 >=4 次。
    assert res["steps"] == 200, f"步数必须抵达真实上限 200，而非首块误终止: {res}"
    assert calls["run"] >= 4, f"应跨多块续跑（>=4 次），实际 {calls['run']}"
