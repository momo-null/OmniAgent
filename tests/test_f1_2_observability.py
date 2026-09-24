"""F1.2（P1）— 可观测性指标回填。

修复：
- 缺陷 A：成功 run 只记 1（实际 ≥steps）→ 计数迁移到 on_llm_end 钩子，每次模型调用 +1；
- 缺陷 B：escalate/异常终态归零 → 计数在 state 上跨块累计，终态路径统一带出；
- retry_count 接通真实重试计数（含 T4.5 repeat-guard 累计失败）。

验收：
- mock N 轮工具循环 → brain_calls 与 action_count 同数量级（1~2×N）；
- mock escalate 终态 → 三字段非 0；
- test_t46 / test_t31 回归全绿（已同步更新）。
"""
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import run_subtask_sdk


class _FakeRes:
    def __init__(self, items):
        self._items = items

    def to_input_list(self):
        return self._items


class _CountingRunner:
    """会真实触发 SDK 钩子的假 Runner：在协程内调用 on_llm_end / on_tool_end。"""

    def __init__(self, n_llm=3, n_tool=3, fail_tool=False, raise_exc=None):
        self.n_llm = n_llm
        self.n_tool = n_tool
        self.fail_tool = fail_tool
        self.raise_exc = raise_exc

    def run(self, agent, items, max_turns=None, hooks=None, run_config=None):
        async def _coro():
            # 模拟 N 次模型调用（每次响应触发 on_llm_end）
            for _ in range(self.n_llm):
                if hooks is not None:
                    await hooks.on_llm_end(None, agent, object())
            # 模拟 M 次工具调用（on_tool_end → 累加 steps / repeat-guard 失败）
            for i in range(self.n_tool):
                if hooks is not None:
                    tool = type("T", (), {"name": f"tool{i}"})()
                    # F2.3：仅**协议层错误**计失败；业务观察（ok:false 等）不计。
                    # 故此处用 protocol_error 标记来驱动 repeat-guard 计数。
                    res = {"protocol_error": True, "error": "传输故障"} if self.fail_tool else {"ok": True}
                    # 真实 SDK 钩子签名：on_tool_end(self, context, agent, tool, result)
                    await hooks.on_tool_end(None, agent, tool, res)
            if self.raise_exc is not None:
                raise self.raise_exc
            return _FakeRes(list(items))

        return _coro()


class _ProviderErr(Exception):
    status_code = 429


def _gate(has_condition=False, verify_done=(True, ""), peek=(False, "")):
    return types.SimpleNamespace(
        verify_done=lambda: verify_done,
        peek=lambda: peek,
        verify_count=0,
        has_condition=has_condition,
        no_confidence=False,
    )


def _run(monkeypatch, runner, gate=None, **kw):
    monkeypatch.setattr(sl, "Runner", runner)
    gate = gate or _gate(has_condition=False, verify_done=(True, ""))
    return run_subtask_sdk(
        {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
        instructions="inst",
        user_input="hi",
        tools=[],
        gate=gate,
        max_steps=5,
        chunk_turns=50,
        should_stop=lambda: False,
        **kw,
    )


def test_llm_call_counting_per_model_response(monkeypatch):
    """缺陷 A 修复：每次模型响应 +1，不再是「成功 run 恒记 1」。"""
    res = _run(monkeypatch, _CountingRunner(n_llm=4, n_tool=4))
    assert res["llm_calls"] == 4, f"应按模型调用次数计数: {res}"
    assert res["steps"] == 4, "工具调用次数应被记录"


def test_retry_count_picks_up_repeat_guard_failures(monkeypatch):
    """retry_count 接通真实重试计数（repeat-guard 累计失败）。"""
    res = _run(monkeypatch, _CountingRunner(n_llm=2, n_tool=3, fail_tool=True))
    assert res["repeat_failures"] == 3, f"失败工具调用应累计: {res}"
    assert res["llm_calls"] == 2


def test_brain_calls_tracks_llm_calls_at_toolloop(monkeypatch):
    """ToolLoop 层：brain 角色下 brain_calls / decision_steps 与 action 同数量级。"""
    from omni_core.local.loop import ToolLoop

    # 仅替换 Runner 为会触发 on_llm_end/on_tool_end 钩子的实现，其余走真实 run_subtask_sdk
    monkeypatch.setattr(sl, "Runner", _CountingRunner(n_llm=5, n_tool=5))

    loop = ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}, verbose=False)
    spec = types.SimpleNamespace(objective="o", done_when="", expected=None,
                                task_id="f12_brain", project_id=None, max_steps=None,
                                history=[], corrections=[])
    world = types.SimpleNamespace(log_action=lambda *a, **k: None)
    loop._run_via_sdk(spec, {"model": "m"}, "sys", world,
                      allow_dispatch=False, user_input="hi")
    assert loop._decision_steps == 5, f"decision_steps 应=模型调用数: {loop._decision_steps}"
    assert loop._brain_calls == 5, f"brain 角色下 brain_calls 应=模型调用数: {loop._brain_calls}"
    assert loop._action_count == 5, "action 数应=工具调用数"


def test_escalate_terminal_carries_nonzero_metrics(monkeypatch):
    """缺陷 B 修复：escalate/异常终态路径三字段（llm_calls/repeat_failures/终态标志）非 0。"""
    res = _run(
        monkeypatch,
        _CountingRunner(n_llm=3, n_tool=2, fail_tool=True, raise_exc=_ProviderErr("限流")),
        gate=_gate(has_condition=False, verify_done=(True, "")),
    )
    # provider 类异常 → escalated=True 且保留计数（不归零）
    assert res["escalated"] is True, "provider 异常应升级"
    assert res["provider_error"] is True
    assert res["llm_calls"] == 3, "escalate 终态仍应带出模型调用计数"
    assert res["repeat_failures"] == 2, "escalate 终态仍应带出重试计数"
