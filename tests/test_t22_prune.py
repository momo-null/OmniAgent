"""T2.2：Prune-First 压缩前文本修剪（Batch-2 任务 U1e）。

覆盖：
1. ``_prune_tool_results`` 纯函数：超长 output 首尾截断（dict / 对象两种形式）、
   严格规避 UTF-16 代理对、低于阈值零干预、threshold=0 关闭、非目标 item 不动。
2. ``run_subtask_sdk`` 压缩分支集成：修剪后若已低于压缩占比阈值则跳过
   ``_maybe_compress``（纯截断够用）；修剪后仍超阈值则照常压缩。
"""
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import _prune_tool_results, run_subtask_sdk


ELLIPSIS = "\n…[已截断过长工具输出]…\n"


# --- 纯函数测试 --------------------------------------------------------------
def _fco_dict(output):
    return {"type": "function_call_output", "call_id": "c1", "output": output}


def _fco_obj(output):
    return types.SimpleNamespace(type="function_call_output", call_id="c1", output=output)


def test_prune_long_output_dict_truncates():
    long_out = "x" * 40000
    it = _fco_dict(long_out)
    items, pruned = _prune_tool_results([it], threshold=10000, head=2000, tail=1000)
    assert pruned == 1
    new = items[0]
    assert new["output"] != long_out
    assert ELLIPSIS in new["output"]
    assert new["output"].startswith("x" * 2000)
    assert new["output"].endswith("x" * 1000)
    assert len(new["output"]) < len(long_out)
    # 入参未被修改（不改动原列表/原对象）
    assert it["output"] == long_out


def test_prune_long_output_object_form():
    long_out = "y" * 40000
    it = _fco_obj(long_out)
    items, pruned = _prune_tool_results([it], threshold=10000, head=2000, tail=1000)
    assert pruned == 1
    new = items[0]
    assert new.output.startswith("y" * 2000)
    assert new.output.endswith("y" * 1000)
    assert it.output == long_out  # 原对象未改


def test_prune_skips_surrogate_pairs():
    # emoji 是代理对（2 个 UTF-16 code unit），按码点切分不应拆开
    long_out = "🌸" * 10000  # 每个 emoji 占 1 码点
    it = _fco_dict(long_out)
    items, pruned = _prune_tool_results([it], threshold=2000, head=1000, tail=500)
    assert pruned == 1
    new = items[0]["output"]
    assert ELLIPSIS in new
    assert new.startswith("🌸" * 1000)
    assert new.endswith("🌸" * 500)
    # 整串仍合法（list 重建无异常），无半切代理对
    assert list(new) == list(new)


def test_prune_no_op_below_threshold():
    short = [_fco_dict("小输出"), _fco_dict("另一个短结果")]
    items, pruned = _prune_tool_results(short, threshold=10000, head=2000, tail=1000)
    assert pruned == 0
    assert [i["output"] for i in items] == ["小输出", "另一个短结果"]


def test_prune_disabled_when_threshold_zero():
    long_out = _fco_dict("z" * 40000)
    items, pruned = _prune_tool_results([long_out], threshold=0, head=2000, tail=1000)
    assert pruned == 0
    assert items[0]["output"] == "z" * 40000


def test_prune_ignores_non_output_items():
    # 非 function_call_output 的 item 不处理（如 user/assistant 消息）
    items_in = [
        {"role": "user", "content": "x" * 40000},
        _fco_dict("y" * 40000),
    ]
    items, pruned = _prune_tool_results(items_in, threshold=100, head=10, tail=10)
    assert pruned == 1
    assert items[0]["content"] == "x" * 40000  # 未动
    assert ELLIPSIS in items[1]["output"]


# --- run_subtask_sdk 集成：跳过摘要判定 --------------------------------------
class _FakeRes:
    def __init__(self, items):
        self._items = items

    def to_input_list(self):
        return self._items


def _make_gate():
    return types.SimpleNamespace(
        verify_done=lambda: (False, ""),
        verify_count=0,
        has_condition=False,
    )


def _run_prune_scenario(monkeypatch, compress_threshold, ctx_window=20000):
    calls = []
    orig = sl._maybe_compress

    def spy(items, keep, summarize):
        calls.append(1)
        return orig(items, keep, summarize)

    monkeypatch.setattr(sl, "_maybe_compress", spy)
    monkeypatch.setattr(sl, "run_async", lambda *a, **k: _FakeRes([
        {"type": "function_call_output", "call_id": "c1", "output": "x" * 40000},
    ]))

    brain = {"model": "m", "base_url": "http://x", "api_key": "k"}
    run_subtask_sdk(
        brain,
        instructions="do it",
        user_input="hi",
        tools=[],
        gate=_make_gate(),
        max_steps=1,
        chunk_turns=1,
        should_stop=lambda: False,
        summarize=lambda items: "summary",
        ctx_window=ctx_window,
        compress_threshold=compress_threshold,
        prune_threshold=10000,
        prune_head=2000,
        prune_tail=1000,
    )
    return calls


def test_prune_below_threshold_skips_summary(monkeypatch):
    # 初始 ratio = 10000/20000 = 0.5 >= 0.5 进入压缩；
    # 修剪后 output 变 ~3070 字符 → est ~767 → ratio 0.038 < 0.5 → 跳过摘要
    calls = _run_prune_scenario(monkeypatch, compress_threshold=0.5)
    assert calls == [], "修剪后已低于压缩阈值，应跳过 _maybe_compress"


def test_prune_still_over_threshold_triggers_summary(monkeypatch):
    # compress_threshold=0.01：修剪后 ratio 0.038 >= 0.01 → 仍压缩
    calls = _run_prune_scenario(monkeypatch, compress_threshold=0.01)
    assert len(calls) == 1, "修剪后仍超阈值，应调用 _maybe_compress"
