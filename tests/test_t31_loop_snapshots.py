"""T3.1（ST-1 最小版 + U4 回归锁定）— 快照回放与行为锁定。

基于 Mock Brain（替换 ``sdk_loop.Runner``）录制四类核心场景的行为快照，
固化：
  * ``run_subtask_sdk`` 返回字典的**全量字段集合**与关键字段取值；
  * 每轮 ``Runner.run`` 收到的 items 演化关键点（历史注入 / 追问消息追加）。

作为后续所有迭代的回归基线：任何对收尾分支的无意改动都会让本文件报错。
本期仅做行为快照固化，不新增回放脚本、不做轨迹级事件溯源（延后 EV-1）。

场景：
  a) 无 done_when 纯文本收尾            -> 任务成功
  b) 配置 done_when 但未达成            -> 触发追问、任务继续（不误判成功）
  c) done_when 条件达成、校验通过        -> 任务成功收尾
  d) Runner 异常触发                    -> 任务标记为 escalated
"""
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import run_subtask_sdk


# 返回字典的全量字段（U3 后新增 provider_error；F1.2 新增 llm_calls / repeat_failures；
# F2.2 新增 paused——daemon 模式纯文本收尾以 paused 结束），任何增减都会在此处被抓住
RESULT_KEYS = {
    "success", "reason", "steps", "escalated", "escalate_reason", "provider_error",
    "llm_calls", "repeat_failures", "paused",
}

USER_INPUT = "执行任务"
PUSH_BACK_MARK = "完成条件尚未满足"


# --- Mock Brain --------------------------------------------------------------
class _FakeRes:
    """模拟 Runner 结果：to_input_list 返回本轮结束后的会话历史。"""

    def __init__(self, items):
        self._items = items

    def to_input_list(self):
        return self._items


class _MockRunner:
    """记录每次 Runner.run 收到的 items，并按预设队列返回会话历史。

    ``run`` 返回协程，交由真实的 ``run_async`` 在常驻 loop 上执行，
    因此异常也能按真实路径冒泡进 sdk_loop 的全局捕获分支（场景 d）。
    """

    def __init__(self, responses=None, raise_exc=None):
        self.responses = list(responses or [])
        self.raise_exc = raise_exc
        self.calls = []          # 每轮收到的 items 快照

    def run(self, agent, items, max_turns=None, hooks=None, run_config=None):
        self.calls.append(list(items))

        async def _coro():
            if self.raise_exc is not None:
                raise self.raise_exc
            if self.responses:
                return _FakeRes(self.responses.pop(0))
            return _FakeRes(list(items))

        return _coro()


def _gate(has_condition=False, verify_done=(True, ""), peek=(False, "")):
    """L2 门控替身：verify_done / peek 返回固定判定结果。"""
    return types.SimpleNamespace(
        verify_done=lambda: verify_done,
        peek=lambda: peek,
        verify_count=0,
        has_condition=has_condition,
    )


def _run(monkeypatch, gate, *, max_steps=3, runner=None, **kw):
    runner = runner or _MockRunner()
    monkeypatch.setattr(sl, "Runner", runner)
    brain = {"model": "m", "base_url": "http://x", "api_key": "k"}
    res = run_subtask_sdk(
        brain,
        instructions="你是一个测试 agent",
        user_input=USER_INPUT,
        tools=[],
        gate=gate,
        max_steps=max_steps,
        chunk_turns=1,
        should_stop=lambda: False,
        **kw,
    )
    return res, runner


# --- 快照断言工具 ------------------------------------------------------------
def _assert_shape(res):
    """固化返回字典的形状（字段集合 + 类型），防止后续迭代悄悄增删字段。"""
    assert set(res.keys()) == RESULT_KEYS, f"返回字段集合发生变化: {sorted(res.keys())}"
    assert isinstance(res["success"], bool)
    assert isinstance(res["reason"], str)
    assert isinstance(res["steps"], int)
    assert isinstance(res["escalated"], bool)
    assert isinstance(res["escalate_reason"], str)
    assert isinstance(res["provider_error"], bool)
    assert isinstance(res["paused"], bool)


# --- 场景 a：无 done_when 纯文本收尾 -> 成功（oneshot） ------------------------
def test_snapshot_a_plain_text_finish_without_done_when(monkeypatch):
    res, runner = _run(monkeypatch, _gate(has_condition=False, verify_done=(True, "")))

    _assert_shape(res)
    # 行为快照：无验证条件时信任大脑，纯文本收尾即判成功（oneshot 缺省，行为不变）
    assert res == {
        "success": True,
        "reason": "模型已收尾，判定完成",
        "steps": 1,
        "escalated": False,
        "escalate_reason": "",
        "provider_error": False,
        "llm_calls": 0,
        "repeat_failures": 0,
        "paused": False,
    }
    # items 演化：首轮仅本轮 user_input（无历史、无技能目录）
    assert len(runner.calls) == 1
    assert runner.calls[0] == [{"role": "user", "content": USER_INPUT}]


# --- 场景 a'：daemon 模式纯文本收尾 -> paused（非 success） --------------------
def test_snapshot_a_daemon_plain_text_finish_pauses(monkeypatch):
    res, runner = _run(monkeypatch, _gate(has_condition=False, verify_done=(True, "")),
                       task_mode="daemon")

    _assert_shape(res)
    # F2.2：daemon 汇报后暂停等待唤醒（不触发 success 终态）
    assert res["success"] is False
    assert res["paused"] is True
    assert res["escalated"] is False
    assert "daemon" in res["reason"]
    assert len(runner.calls) == 1


# --- 场景 b：done_when 配置但未达成 -> 追问、任务继续 --------------------------
def test_snapshot_b_done_when_unmet_triggers_pushback(monkeypatch):
    gate = _gate(has_condition=True, verify_done=(False, "目标产物缺失"),
                 peek=(False, "未完成"))
    res, runner = _run(monkeypatch, gate, max_steps=3)

    _assert_shape(res)
    # 行为快照：未达成 → 不判成功、不升级，而是追问后继续推进直至预算耗尽
    assert res["success"] is False
    assert res["escalated"] is False
    assert res["provider_error"] is False
    assert res["reason"].startswith("budget_exhausted")
    assert res["steps"] == 3
    # items 演化：追问消息被追加，且任务确实继续跑了多轮
    assert len(runner.calls) >= 2, "未达成时应追问并继续，而非直接收尾"
    assert any(PUSH_BACK_MARK in str(m.get("content", ""))
               for m in runner.calls[1] if isinstance(m, dict))
    # 后续轮次首条仍是本轮 user_input（历史未被裁剪）
    assert runner.calls[1][0] == {"role": "user", "content": USER_INPUT}


# --- 场景 c：done_when 达成、校验通过 -> 成功收尾 ------------------------------
def test_snapshot_c_done_when_met_finishes_success(monkeypatch):
    gate = _gate(has_condition=True, verify_done=(True, "完成条件已达成"),
                 peek=(True, "命中"))
    res, runner = _run(monkeypatch, gate, max_steps=3)

    _assert_shape(res)
    # 行为快照：校验通过 → 直接成功收尾，不追问、不多跑
    assert res == {
        "success": True,
        "reason": "完成条件已达成",
        "steps": 1,
        "escalated": False,
        "escalate_reason": "",
        "provider_error": False,
        "llm_calls": 0,
        "repeat_failures": 0,
        "paused": False,
    }
    assert len(runner.calls) == 1
    assert all(PUSH_BACK_MARK not in str(m.get("content", ""))
               for m in runner.calls[0] if isinstance(m, dict))


# --- 场景 d：Runner 异常 -> escalated ----------------------------------------
def test_snapshot_d_runner_exception_marks_escalated(monkeypatch):
    boom = RuntimeError("runner 内部故障")
    runner = _MockRunner(raise_exc=boom)
    res, _ = _run(monkeypatch, _gate(has_condition=False, verify_done=(True, "")),
                  runner=runner)

    _assert_shape(res)
    # 行为快照：非服务类异常 → 升级交回上级，保留原生报错文案，不降级为 paused
    assert res["success"] is False
    assert res["escalated"] is True
    assert res["provider_error"] is False
    assert res["steps"] == 0
    assert res["reason"] == res["escalate_reason"]
    assert res["reason"].startswith("runner 异常: RuntimeError")
    assert "runner 内部故障" in res["reason"]
