"""F2.4 — 步数预算与墙钟对齐。

覆盖：
1. 主链墙钟缺省 0（不检查），不随子任务墙钟（escalation.wallclock_sec=120）污染主链。
2. 大步数预算（>200）+ 未配置墙钟 → 主链不检查（返回 0）；小任务同样返回 0。
3. 显式配置 runtime.long_task.wallclock_sec → 主链按配置值检查（返回该值）。
4. wallclock_sec 参数正确透传给 run_subtask_sdk（主链/子任务解耦的落地点）。
"""
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.local.loop import ToolLoop
import config as config_mod


def _loop():
    return ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}, verbose=False)


def _cfg_patch(monkeypatch, value):
    """把 runtime.long_task.wallclock_sec 固定为 value，其余路径透传默认。"""
    monkeypatch.setattr(
        config_mod, "get_config",
        lambda path, default=None: value if path == "runtime.long_task.wallclock_sec" else default,
    )


def _spec(max_steps):
    return types.SimpleNamespace(
        objective="o", done_when="", expected=None, task_id="t_f24",
        project_id=None, max_steps=max_steps, history=[], corrections=[],
    )


# --- 1. 缺省 0：不与子任务 120s 墙钟耦合 --------------------------------------
def test_main_chain_wallclock_default_zero(monkeypatch):
    _cfg_patch(monkeypatch, 0)
    loop = _loop()
    # 配 1000 步但不配墙钟 → 主链返回 0（不检查），绝不会被 120s 腰斩
    assert loop._resolve_main_wallclock(1000, _spec(1000)) == 0.0
    # 子任务墙钟缺省仍是 120（验证两者来源解耦：取的是 escalation，不在此函数）
    assert float(loop.escalation.get("wallclock_sec", 0) or 0) == 120.0


# --- 2. 大/小预算均未配置墙钟 → 返回 0 ----------------------------------------
def test_big_budget_no_wallclock_returns_zero(monkeypatch):
    _cfg_patch(monkeypatch, 0)
    loop = _loop()
    assert loop._resolve_main_wallclock(1000, _spec(1000)) == 0.0
    # 不限步（budget=None 但 spec.max_steps 大）同样不检查
    assert loop._resolve_main_wallclock(None, _spec(500)) == 0.0


def test_small_budget_no_wallclock_returns_zero(monkeypatch):
    _cfg_patch(monkeypatch, 0)
    loop = _loop()
    assert loop._resolve_main_wallclock(50, _spec(50)) == 0.0


# --- 3. 显式配置墙钟 → 直接生效 ----------------------------------------------
def test_explicit_wallclock_respected(monkeypatch):
    _cfg_patch(monkeypatch, 300)
    loop = _loop()
    assert loop._resolve_main_wallclock(1000, _spec(1000)) == 300.0
    assert loop._resolve_main_wallclock(50, _spec(50)) == 300.0


# --- 4. wallclock_sec 透传 run_subtask_sdk ------------------------------------
def test_wallclock_passed_to_run_subtask_sdk(monkeypatch):
    captured = {}

    def _fake_run(brain, *, instructions=None, user_input=None, tools=None, gate=None, **kw):
        captured.update(kw)
        return {"success": True, "reason": "ok", "steps": 1, "escalated": False,
                "escalate_reason": "", "provider_error": False,
                "llm_calls": 1, "repeat_failures": 0}

    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_run)
    loop = _loop()
    spec = _spec(None)
    loop._run_via_sdk(spec, {"model": "m"}, "sys", types.SimpleNamespace(),
                      is_sub=True, allow_dispatch=False, user_input="hi", wallclock_sec=300.0)
    assert captured.get("wallclock_sec") == 300.0

    captured.clear()
    loop._run_via_sdk(spec, {"model": "m"}, "sys", types.SimpleNamespace(),
                      is_sub=True, allow_dispatch=False, user_input="hi", wallclock_sec=0.0)
    assert captured.get("wallclock_sec") == 0.0
