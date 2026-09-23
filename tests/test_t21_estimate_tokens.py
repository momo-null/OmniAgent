"""T2.1 (U1a): Token 估算漏算修复验证。

覆盖三类场景：
1. 超长工具参数 arguments / 返回 output 被统计（修复前仅≈2，修复后≥1000）；
2. 纯 content 旧格式 item 估算逻辑无回归、结果一致；
3. 对象形态 item 兼容（SDK 内部对象而非 dict）。
"""

import pytest

from omni_core.brain.sdk_loop import _estimate_tokens


def test_estimate_counts_arguments_and_output():
    """修复核心：function_call.arguments / function_call_output.output 必须被计入。"""
    items = [
        {"type": "function_call", "name": "t", "arguments": "x" * 400},
        {"type": "function_call_output", "output": "y" * 4000},
    ]
    est = _estimate_tokens(items)
    # 修复前：两个 item 都只命中 name/空 content -> 1 + 1 = 2（漏算）
    # 修复后：arguments 400//4=100 + output 4000//4=1000 = 1100
    assert est >= 1000, f"漏算修复后估算应≥1000，实际 {est}"


def test_estimate_legacy_content_unchanged():
    """纯 content 旧格式 item，结果应与仅统计 content 的原始口径一致（无回归）。"""
    items = [
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "a" * 40},
    ]
    est = _estimate_tokens(items)
    # 同口径手工核算：每个 item max(1, len(content)//4)
    expected = max(1, 11 // 4) + max(1, 40 // 4)
    assert est == expected, f"纯 content 旧格式应无回归，实际 {est} 期望 {expected}"


def test_estimate_object_items():
    """SDK 内部对象形态 item 也应兼容统计。"""
    class _Item:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    items = [_Item(type="function_call", name="t", arguments="z" * 400)]
    est = _estimate_tokens(items)
    assert est >= 100, f"对象形态 arguments 应被统计，实际 {est}"


def test_estimate_empty_items_safe():
    """空列表 / 无文本 item 不抛异常，且每个 item 至少记 1 token（兜底保留）。"""
    assert _estimate_tokens([]) == 0
    items = [{"type": "function_call", "name": "t"}, {}]
    # 两个 item 都无有效文本 -> 各兜底 1
    assert _estimate_tokens(items) == 2
