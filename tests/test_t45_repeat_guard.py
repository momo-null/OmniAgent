"""T4.5（RG-1）— 工具重复失败防护。

覆盖：
1. 同名工具连续失败达阈值 -> 下一次请求自动注入提醒，且仅注入一次（不刷屏）。
2. 工具执行成功后计数清零。
3. 关闭配置（threshold=0）时零干预；无法判定失败时不误判。
"""
import asyncio
import types

import pytest

from omni_core.brain.sdk_loop import (
    _RepeatGuard, _RepeatGuardModel, _is_tool_failure, run_subtask_sdk,
)


class _InnerModel:
    def __init__(self):
        self.calls = []

    async def get_response(self, *args, **kwargs):
        inp = kwargs.get("input", args[1] if len(args) > 1 else None)
        self.calls.append(list(inp or []))
        return types.SimpleNamespace(output=[])

    async def stream_response(self, *args, **kwargs):
        inp = kwargs.get("input", args[1] if len(args) > 1 else None)
        self.calls.append(list(inp or []))
        if False:
            yield None


def _invoke(model, items, si="SYS"):
    return asyncio.run(model.get_response(si, list(items), None, [], None, [], None))


# --- 失败判定 -----------------------------------------------------------------
def test_is_tool_failure_recognition():
    # F2.3：业务观察（工具协议层成功返回的一切，含 ok:false / 业务报错文本）
    # 不计失败；仅协议层错误标记（传输/解析故障）判失败。
    assert _is_tool_failure({"ok": False}) is False
    assert _is_tool_failure({"success": False}) is False
    assert _is_tool_failure({"error": "资源不足"}) is False
    assert _is_tool_failure({"ok": False, "error": "资源不足"}) is False
    # 协议层错误（传输/解析故障，非业务语义）仍判失败
    assert _is_tool_failure({"protocol_error": True}) is True
    assert _is_tool_failure({"transport_error": True}) is True
    # 无法判定时保守视为成功（避免误提醒）
    assert _is_tool_failure({"ok": True}) is False
    assert _is_tool_failure("some text") is False
    assert _is_tool_failure(None) is False


# --- 1. 连续失败达标 -> 注入提醒，仅一次 ---------------------------------------
def test_repeat_failure_injects_reminder_once():
    inner = _InnerModel()
    guard = _RepeatGuard(3)
    model = _RepeatGuardModel(inner, guard)

    base = [{"role": "user", "content": "开始"}]
    _invoke(model, base)            # 未达阈值：零干预
    assert inner.calls[0] == base

    for _ in range(3):
        guard.on_step("click", True)
    _invoke(model, base)
    sent = inner.calls[1]
    assert len(sent) == len(base) + 1, "达阈值后应注入提醒"
    assert "click" in sent[-1]["content"] and "连续失败" in sent[-1]["content"]

    # 注入一次后 pending 已消费；未再达标前不再注入
    _invoke(model, base)
    assert inner.calls[2] == base, "提醒只注入一次，避免刷屏"


def test_reminder_resets_after_injection():
    inner = _InnerModel()
    guard = _RepeatGuard(3)
    model = _RepeatGuardModel(inner, guard)
    base = [{"role": "user", "content": "x"}]

    for _ in range(3):
        guard.on_step("click", True)
    _invoke(model, base)            # 注入 + 计数清零
    for _ in range(2):
        guard.on_step("click", True)
    _invoke(model, base)
    assert inner.calls[1] == base, "清零后未再达阈值不应注入"


# --- 2. 成功后计数清零 ---------------------------------------------------------
def test_success_resets_counter():
    inner = _InnerModel()
    guard = _RepeatGuard(3)
    model = _RepeatGuardModel(inner, guard)
    base = [{"role": "user", "content": "x"}]

    guard.on_step("click", True)
    guard.on_step("click", True)
    guard.on_step("click", False)      # 成功 -> 清零
    guard.on_step("click", True)
    guard.on_step("click", True)
    _invoke(model, base)
    assert inner.calls[0] == base, "中间成功应清零计数，未达连续阈值"


def test_different_tool_name_restarts_count():
    inner = _InnerModel()
    guard = _RepeatGuard(3)
    model = _RepeatGuardModel(inner, guard)
    base = [{"role": "user", "content": "x"}]

    guard.on_step("click", True)
    guard.on_step("type", True)
    guard.on_step("wait", True)
    _invoke(model, base)
    assert inner.calls[0] == base, "只统计同名工具连续失败（换名即重计）"


# --- 3. 关闭配置 -> 零干预 -----------------------------------------------------
def test_disabled_guard_never_injects():
    inner = _InnerModel()
    guard = _RepeatGuard(0)
    model = _RepeatGuardModel(inner, guard)
    base = [{"role": "user", "content": "x"}]
    for _ in range(10):
        guard.on_step("click", True)
    _invoke(model, base)
    assert inner.calls[0] == base, "threshold=0 应完全零干预"


def test_run_subtask_disabled_by_config(monkeypatch):
    """配置 repeat_guard=0 时，run_subtask_sdk 不构造守卫（基线行为不变）。"""
    import config as cfgmod

    real_get = cfgmod.get_config

    def fake_get(key, default=None):
        if key == "runtime.long_task.repeat_guard":
            return 0
        return real_get(key, default)

    monkeypatch.setattr(cfgmod, "get_config", fake_get)
    from omni_core.brain import sdk_loop as sl

    created = []

    class _SpyGuard(_RepeatGuard):
        def __init__(self, *a, **kw):
            created.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(sl, "_RepeatGuard", _SpyGuard)
    monkeypatch.setattr(sl, "run_async", lambda coro, *a, **k: _drain(coro))

    def _drain(coro):
        try:
            coro.close()
        except Exception:
            pass
        return types.SimpleNamespace(to_input_list=lambda: [])

    run_subtask_sdk(
        {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
        instructions="do", user_input="hi", tools=[],
        gate=types.SimpleNamespace(verify_done=lambda: (True, ""), peek=lambda: (False, ""),
                                   verify_count=0, has_condition=False),
        max_steps=1, chunk_turns=1, should_stop=lambda: False,
    )
    assert created == [], "配置关闭时不应构造重复失败守卫"
