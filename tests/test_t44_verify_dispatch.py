"""T4.4（U5c）— 关键子任务二次复核。

覆盖：
1. 开启复核（need_verify）时，校验通过 / 失败的展示文案差异化。
2. 默认关闭复核（无 need_verify 标记）时完全零干预，行为回归基线。
"""
import asyncio
import json
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import build_meta_tools
from omni_core.local.tool_loop import _format_prev_results, _recheck_spec


class _State:
    def __init__(self):
        self.dispatch_plan = []
        self.steps = 0
        self.done = False
        self.escalated = False
        self.dispatch_plan = None


def _gate():
    return types.SimpleNamespace(verify_done=lambda: (True, ""), peek=lambda: (False, ""),
                                 verify_count=0, has_condition=False)


def _invoke(tool, **kwargs):
    ctx = types.SimpleNamespace(tool_name=getattr(tool, "name", "tool"))
    return asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))


def _dispatch_tool():
    tools = build_meta_tools(_State(), _gate(), None, allow_dispatch=True)
    for t in tools:
        if getattr(t, "name", "") == "dispatch":
            return t
    raise AssertionError("dispatch 元工具未暴露")


def _plan_from(out):
    return out.get("items") or []


# --- dispatch 参数：need_verify ----------------------------------------------
def test_dispatch_need_verify_default_false():
    tool = _dispatch_tool()
    out = _invoke(tool, items=json.dumps([{"desc": "a", "done_when": "A"}]))
    plan = _plan_from(out)
    assert plan and all(not it.get("need_verify") for it in plan), "默认关闭复核"


def test_dispatch_need_verify_marks_batch():
    tool = _dispatch_tool()
    out = _invoke(tool, items=json.dumps([{"desc": "a", "done_when": "A"},
                                          {"desc": "b", "done_when": "B"}]),
                  need_verify=True)
    plan = _plan_from(out)
    assert len(plan) == 2
    assert all(it.get("need_verify") is True for it in plan), "整批次应标记需复核"


# --- 二次复核执行 -------------------------------------------------------------
class _LoopStub:
    """只验证 _recheck_batch 的替身：复用 ToolLoop 的方法实现。"""

    def __init__(self, verify_result):
        self._result = verify_result
        self.calls = []

    def _verify(self, spec, world, condition: str = "") -> tuple:
        # 完成条件由复核用轻量 spec 携带（condition 参数为空）
        self.calls.append(condition or getattr(spec, "done_when", ""))
        return self._result

    _recheck_batch = None  # 由下方动态绑定


def _bind_recheck(loop):
    from omni_core.local.tool_loop import ToolLoop
    loop._recheck_batch = lambda prev_results, world: ToolLoop._recheck_batch(
        loop, prev_results, world)
    return loop


def test_recheck_pass_marks_ok():
    loop = _bind_recheck(_LoopStub((True, "命中")))
    prev = [{"desc": "a", "done_when": "A", "success": True, "need_verify": True}]
    loop._recheck_batch(prev, None)
    assert prev[0]["recheck"] == "pass"
    assert loop.calls == ["A"], "应带上子任务完成条件做二次校验"


def test_recheck_fail_only_marks_not_blocking():
    loop = _bind_recheck(_LoopStub((False, "未见产物")))
    prev = [{"desc": "a", "done_when": "A", "success": True, "need_verify": True}]
    loop._recheck_batch(prev, None)
    assert prev[0]["recheck"] == "fail"
    assert prev[0]["recheck_reason"] == "未见产物"
    # 保留未完成语义：不翻转 success、不做强制拦截
    assert prev[0]["success"] is True


def test_recheck_skipped_when_not_marked():
    loop = _bind_recheck(_LoopStub((False, "x")))
    prev = [{"desc": "a", "done_when": "A", "success": True}]
    loop._recheck_batch(prev, None)
    assert "recheck" not in prev[0], "未标记复核的批次应零干预（默认行为不变）"
    assert loop.calls == []


def test_recheck_skipped_for_failed_subtask():
    loop = _bind_recheck(_LoopStub((False, "x")))
    prev = [{"desc": "a", "done_when": "A", "success": False, "need_verify": True}]
    loop._recheck_batch(prev, None)
    assert "recheck" not in prev[0], "失败子任务无需复核（本就未达成）"


def test_recheck_exception_is_captured():
    class _Boom(_LoopStub):
        def _verify(self, spec, world, condition: str = ""):
            raise RuntimeError("boom")

    loop = _bind_recheck(_Boom((False, "")))
    prev = [{"desc": "a", "done_when": "A", "success": True, "need_verify": True}]
    loop._recheck_batch(prev, None)
    assert prev[0]["recheck"] == "fail"
    assert "复核异常" in prev[0]["recheck_reason"]


# --- 展示文案差异化 -----------------------------------------------------------
def test_display_text_differs_for_pass_and_fail():
    passed = _format_prev_results(
        [{"desc": "a", "success": True, "reason": "", "recheck": "pass"}], "")
    failed = _format_prev_results(
        [{"desc": "a", "success": True, "reason": "",
          "recheck": "fail", "recheck_reason": "未见产物"}], "")
    assert "复核: 通过" in passed
    assert "复核未通过: 未见产物" in failed
    assert "复核未通过" not in passed
    assert passed != failed


def test_recheck_spec_carries_condition():
    spec = _recheck_spec("打开记事本")
    assert spec is not None
    assert (spec.expected or spec.done_when) == "打开记事本"
