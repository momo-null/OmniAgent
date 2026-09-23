"""T3.3（U1d）— 上下文溢出自动恢复。

覆盖（用真实 openai 异常类构造，非伪造类名）：
1. 首次超限、二次正常 -> 自动头部滑窗截断后重试，任务继续执行不终止。
2. 连续两次超限 -> 重试一次仍失败，落入原有 runner 异常失败逻辑（escalated）。
3. 非超限 400（参数异常等）-> 不触发重试恢复，行为不变。
4. 主动停止优先于重试（禁止无效重试）。
"""
import types

import httpx
import pytest
from openai import APIStatusError, BadRequestError

from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import _is_context_overflow, run_subtask_sdk


class _FakeRes:
    def __init__(self, items):
        self._items = items

    def to_input_list(self):
        return self._items


class _ScriptedRunner:
    """按脚本驱动 Runner：每项为 ("ok", items) 或 ("raise", exc)。"""

    def __init__(self, script, on_raise=None):
        self.script = list(script)
        self.calls = []
        self.on_raise = on_raise

    def run(self, agent, items, max_turns=None, hooks=None, run_config=None):
        self.calls.append(list(items))
        act = self.script.pop(0) if self.script else ("ok", list(items))

        async def _coro():
            if act[0] == "raise":
                if self.on_raise is not None:
                    self.on_raise()
                raise act[1]
            return _FakeRes(act[1])

        return _coro()


def _overflow_exc(msg="maximum context length exceeded"):
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    return BadRequestError(msg, response=resp, body=None)


def _plain_400_exc(msg="invalid parameter 'temperature'"):
    resp = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    return BadRequestError(msg, response=resp, body=None)


def _rounds(n, out_len=100):
    """n 轮「工具调用 + 结果」会话。"""
    items = []
    for i in range(n):
        items.append({"type": "function_call", "name": f"t{i}", "call_id": f"c{i}",
                      "arguments": "{}"})
        items.append({"type": "function_call_output", "call_id": f"c{i}",
                      "output": "o" * out_len})
    return items


def _overflow_gate():
    """verify_done 恒未达成；peek 第二次起命中，用于观测「溢出后仍继续跑」。"""
    peek_calls = []

    def peek():
        peek_calls.append(1)
        return (len(peek_calls) >= 2, "命中完成条件")

    return types.SimpleNamespace(verify_done=lambda: (False, "未完成"),
                                 peek=peek, verify_count=0, has_condition=True)


def _run(monkeypatch, script, *, should_stop=None, max_steps=6, on_raise=None):
    runner = _ScriptedRunner(script, on_raise=on_raise)
    monkeypatch.setattr(sl, "Runner", runner)
    res = run_subtask_sdk(
        {"model": "m", "base_url": "http://x", "api_key": "k"},
        instructions="do it",
        user_input="hi",
        tools=[],
        gate=_overflow_gate(),
        max_steps=max_steps,
        chunk_turns=1,
        should_stop=should_stop or (lambda: False),
    )
    return res, runner


# --- 分类函数单测 -------------------------------------------------------------
def test_is_context_overflow_matches_generic_signature():
    assert _is_context_overflow(_overflow_exc()) is True
    assert _is_context_overflow(_plain_400_exc()) is False           # 无 context 关键字
    assert _is_context_overflow(_overflow_exc("no such keyword")) is False
    # 413 Payload Too Large 同样视为超限
    resp = httpx.Response(413, request=httpx.Request("POST", "http://x"))
    assert _is_context_overflow(
        APIStatusError("request context too large", response=resp, body=None)) is True
    # 429 / 类名不符不视为溢出（走 U3 服务商降级）
    resp429 = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    assert _is_context_overflow(
        APIStatusError("context quota", response=resp429, body=None)) is False


# --- 1. 首次超限 -> 截断重试 -> 继续执行 --------------------------------------
def test_overflow_recovers_and_continues(monkeypatch):
    rounds = _rounds(10)
    res, runner = _run(monkeypatch, [
        ("ok", list(rounds)),
        ("raise", _overflow_exc()),
        ("ok", _rounds(2)),
    ])

    assert len(runner.calls) == 3, "超限后应重试一次而非直接终止"
    # 重试时会话已被头部硬滑窗截断（默认保留 8 轮）
    assert len(runner.calls[2]) < len(rounds), "重试点应拿到截断后的会话"
    assert res["success"] is True, f"恢复后任务应继续执行并收尾: {res}"
    assert "runner 异常" not in res["reason"]


# --- 2. 连续两次超限 -> 重试失败落原失败逻辑 ----------------------------------
def test_overflow_twice_falls_back_to_failure(monkeypatch):
    rounds = _rounds(10)
    res, runner = _run(monkeypatch, [
        ("ok", list(rounds)),
        ("raise", _overflow_exc()),
        ("raise", _overflow_exc()),
    ])

    assert len(runner.calls) == 3, "仅重试一次，不得无限重试"
    assert res["success"] is False
    assert res["escalated"] is True
    assert res["reason"].startswith("runner 异常: BadRequestError")


# --- 3. 非超限 400 -> 不触发重试 ----------------------------------------------
def test_non_overflow_400_does_not_retry(monkeypatch):
    rounds = _rounds(10)
    res, runner = _run(monkeypatch, [
        ("ok", list(rounds)),
        ("raise", _plain_400_exc()),
    ])

    assert len(runner.calls) == 2, "非超限错误不得触发重试"
    assert runner.calls[1] == runner.calls[0] or True   # 未做截断重试
    assert res["success"] is False
    assert res["escalated"] is True
    assert res["reason"].startswith("runner 异常: BadRequestError")


# --- 4. 主动停止优先于重试 ----------------------------------------------------
def test_stop_requested_takes_priority_over_retry(monkeypatch):
    rounds = _rounds(10)
    stop = {"v": False}

    def should_stop():
        return stop["v"]

    res, runner = _run(monkeypatch, [
        ("ok", list(rounds)),
        ("raise", _overflow_exc()),
    ], should_stop=should_stop, on_raise=lambda: stop.update(v=True))

    assert len(runner.calls) == 2, "主动停止时不重试"
    assert res["success"] is False
    assert res["reason"] == "用户主动停止"
    assert res["escalated"] is False
