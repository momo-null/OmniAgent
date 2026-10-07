"""T3.2（U1b+）— Model 适配层粘性压缩。

覆盖：
1. 低于阈值零干预：模型收到原始请求入参（对象内容完全一致）。
2. 超阈值触发摘要：请求入参为「摘要 + 尾部会话」，SDK 原始 items 无改动。
3. 粘性/缓存友好：连续无新增会话时复用历史摘要，请求前缀逐字节一致。
4. 降级：摘要报错 / 摘要无效（≥ 原文 60%）自动退化为硬滑窗，任务不崩溃。
5. 配置与互斥：maxInputTokens=0 不启用；启用后禁用 chunk 边界压缩。
"""
import asyncio
import json
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import (
    SUMMARY_INSTRUCTION, _CompactionModel, _estimate_tokens, run_subtask_sdk,
)


# --- 测试替身 ----------------------------------------------------------------
class _InnerModel:
    """内层模型替身：只记录每次请求收到的 (system_instructions, input)。"""

    def __init__(self):
        self.calls = []

    async def get_response(self, *args, **kwargs):
        si = kwargs.get("system_instructions", args[0] if args else None)
        inp = kwargs.get("input", args[1] if len(args) > 1 else None)
        self.calls.append({"si": si, "input": list(inp or [])})
        return types.SimpleNamespace(output=[])

    async def stream_response(self, *args, **kwargs):
        si = kwargs.get("system_instructions", args[0] if args else None)
        inp = kwargs.get("input", args[1] if len(args) > 1 else None)
        self.calls.append({"si": si, "input": list(inp or [])})
        if False:
            yield None


def _invoke(model, items, si="SYS"):
    """按 SDK 真实调用形态（位置参数）触发一次请求。"""
    return asyncio.run(model.get_response(si, list(items), None, [], None, [], None))


def _msg(text):
    return {"role": "user", "content": text}


def _msgs(n, size=4000):
    """n 条会话消息，每条约 1000 token（size/4）。"""
    return [_msg("b" * size) for _ in range(n)]


def _mk_model(summarizer, threshold=1000, retain=0.5, hard=0, keep=8):
    inner = _InnerModel()
    model = _CompactionModel(
        inner,
        threshold_tokens=threshold,
        retain_ratio=retain,
        summarize=summarizer,
        hard_ceiling_tokens=hard,
        keep_rounds=keep,
    )
    return model, inner


# --- 0. Prune-First：超长工具输出先截断，再判阈值 ------------------------------
def test_prune_truncates_long_tool_output():
    """prune_threshold > 0 时，超长 output 被首尾截断后进请求（工具返回体积生效）。"""
    inner = _InnerModel()
    model = _CompactionModel(
        inner,
        threshold_tokens=100000,   # 远高于内容量：确保只走 prune、不触发压缩
        retain_ratio=0.5,
        summarize=lambda items, si=None: "摘要",
        prune_threshold=100,
        prune_head=20,
        prune_tail=10,
    )
    long_out = {"type": "function_call_output", "call_id": "c1", "output": "z" * 5000}
    _invoke(model, [long_out])
    sent = inner.calls[-1]["input"]
    text = sent[0].get("output", "")
    assert len(text) < 5000, "超长工具输出应被截断"
    assert "已截断过长工具输出" in text


# --- 1. 低于阈值零干预 --------------------------------------------------------
def test_below_threshold_passthrough_untouched():
    calls = []

    def summ(items, si=None):
        calls.append(1)
        return "不该被调用"

    model, inner = _mk_model(summ)
    items = [_msg("a" * 400) for _ in range(3)]   # 3 × 100 token = 300 < 1000
    snapshot = json.dumps(items, sort_keys=True)

    _invoke(model, items)

    assert inner.calls[0]["input"] == items, "低于阈值应原样透传"
    assert calls == [], "低于阈值不得触发摘要（零开销）"
    assert json.dumps(items, sort_keys=True) == snapshot, "原始 items 不得被改动"
    assert model.summary_msg is None and model.kept_from == 0


# --- 2. 超阈值触发摘要 --------------------------------------------------------
def test_over_threshold_compacts_with_summary():
    seen = {}

    def summ(items, si=None):
        seen["ctx"] = list(items)
        seen["si"] = si
        return "摘要：已完成 A、B 两步"

    model, inner = _mk_model(summ)
    items = _msgs(10)                    # 10 × 1000 = 10000 token > 1000
    snapshot = json.dumps(items, sort_keys=True)

    _invoke(model, items)

    sent = inner.calls[0]["input"]
    assert len(sent) < len(items), "应压缩为「摘要 + 尾部会话」"
    assert sent[0]["content"].startswith("[历史压缩摘要]"), sent[0]
    assert "摘要：已完成 A、B 两步" in sent[0]["content"]
    # 尾部会话来自原 items 末尾（未被改写）
    assert sent[1:] == items[len(items) - len(sent) + 1:]
    # SDK 原始状态无改动
    assert json.dumps(items, sort_keys=True) == snapshot
    # 热前缀：系统提示透传给摘要回调；八段结构化指令仅追加在末尾
    assert seen["si"] == "SYS"
    assert seen["ctx"][-1]["content"] == SUMMARY_INSTRUCTION
    assert SUMMARY_INSTRUCTION.count("\n") >= 8 and "下一步计划" in SUMMARY_INSTRUCTION


# --- 3. 粘性复用：前缀逐字节一致（缓存友好） ----------------------------------
def test_sticky_reuse_keeps_prefix_identical():
    calls = []

    def summ(items, si=None):
        calls.append(len(items))
        return "摘要"

    model, inner = _mk_model(summ)
    items = _msgs(10)

    _invoke(model, items)                      # 首次：生成摘要
    first = json.dumps(inner.calls[0]["input"], sort_keys=True, ensure_ascii=False)

    _invoke(model, items)                      # 无新增会话：复用历史摘要
    second = json.dumps(inner.calls[1]["input"], sort_keys=True, ensure_ascii=False)

    assert first == second, "无新增内容时请求前缀应逐字节一致（缓存友好）"
    assert len(calls) == 1, "无新增内容不得重复生成摘要"

    # 新增会话 -> 增量重压并推进位点
    items2 = items + _msgs(4)
    _invoke(model, items2)
    assert len(calls) == 2, "出现新增内容应增量重压"
    assert len(inner.calls[2]["input"]) < len(items2)


# --- 4. 降级：摘要报错 / 摘要无效 -> 硬滑窗 -----------------------------------
def _items_with_calls(n_rounds=5):
    """构造含 function_call 的会话（供 _hard_keep 按轮截断）。"""
    items = []
    for i in range(n_rounds):
        items.append({"type": "function_call", "name": f"t{i}", "call_id": f"c{i}",
                      "arguments": "{}"})
        items.append({"type": "function_call_output", "call_id": f"c{i}",
                      "output": "o" * 4000})
    return items


def test_degrades_to_hard_window_when_summary_raises():
    def summ(items, si=None):
        raise RuntimeError("摘要服务不可用")

    model, inner = _mk_model(summ, keep=2)
    items = _items_with_calls(5)
    _invoke(model, items)

    sent = inner.calls[0]["input"]
    assert sent, "降级后仍应有请求入参（任务不崩溃）"
    assert len(sent) < len(items), "应降级为纯滑窗截断"
    assert model.summary_msg is None


def test_degrades_to_hard_window_when_summary_invalid():
    def summ(items, si=None):
        # 复述原文 -> 新摘要长度 ≥ 原文 60%，判定压缩失败
        return "原文复述：" + "".join(
            str(it.get("content") or it.get("output") or "") for it in items
        )[:200000]

    model, inner = _mk_model(summ, keep=2)
    items = _items_with_calls(5)
    _invoke(model, items)

    sent = inner.calls[0]["input"]
    assert len(sent) < len(items), "摘要无效应降级为硬滑窗"
    assert model.summary_msg is None


# --- 5. 配置开关：maxInputTokens 决定是否启用粘性压缩 --------------------------
def _run_with_spies(monkeypatch, max_input_tokens):
    compaction_calls = []

    class _SpyCompaction(_CompactionModel):
        def __init__(self, *a, **kw):
            compaction_calls.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(sl, "_CompactionModel", _SpyCompaction)

    class _Res:
        """返回足够长的会话，确保上下文管理段被走到。"""

        def to_input_list(self):
            return [_msg("x" * 40000)]

    def fake_run_async(coro, *a, **k):
        try:
            coro.close()          # 丢弃未执行的协程，避免 RuntimeWarning
        except Exception:
            pass
        return _Res()

    monkeypatch.setattr(sl, "run_async", fake_run_async)

    run_subtask_sdk(
        {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
        instructions="do it",
        user_input="hi",
        tools=[],
        # verify 未达成：让循环走完「追问 -> 上下文管理」全段，才能观测压缩分支
        gate=types.SimpleNamespace(verify_done=lambda: (False, "未完成"),
                                   peek=lambda: (False, ""),
                                   verify_count=0, has_condition=False),
        max_steps=1,
        chunk_turns=1,
        should_stop=lambda: False,
        summarize=lambda items: "摘要",
        compress_threshold=0.01,
        max_input_tokens=max_input_tokens,
        retain_ratio=0.5,
    )
    return compaction_calls


def test_compaction_disabled_when_max_input_tokens_zero(monkeypatch):
    assert _run_with_spies(monkeypatch, max_input_tokens=0) == [], \
        "maxInputTokens=0 应关闭粘性压缩"


def test_compaction_enabled_when_max_input_tokens_positive(monkeypatch):
    assert _run_with_spies(monkeypatch, max_input_tokens=8000) == [1], \
        "maxInputTokens>0 应启用粘性压缩"


# --- 6. 压缩检查点回调（中途蒸馏挂点，2026-10-04） ------------------------------
def test_on_compact_fires_on_new_summary_only():
    """新摘要产出（含增量重压）触发 on_compact；粘性复用与零干预不触发。"""
    fired = []

    def summ(items, si=None):
        return "摘要内容"

    model, inner = _mk_model(summ)
    model._on_compact = lambda: fired.append(1)

    items = _msgs(10)
    _invoke(model, items)                      # 首次压缩 → 新摘要 → 触发
    assert len(fired) == 1
    _invoke(model, items)                      # 无新增内容 → 粘性复用 → 不触发
    assert len(fired) == 1
    items2 = items + _msgs(3)                  # 追加内容 → 增量重压 → 再触发
    _invoke(model, items2)
    assert len(fired) == 2


def test_on_compact_exception_never_breaks_request():
    """回调抛异常被吞掉：请求照常完成，压缩结果不受影响。"""
    def summ(items, si=None):
        return "摘要内容"

    model, inner = _mk_model(summ)

    def _boom():
        raise RuntimeError("checkpoint boom")

    model._on_compact = _boom
    items = _msgs(10)
    _invoke(model, items)                      # 不抛异常即通过
    assert inner.calls[0]["input"][0]["content"].startswith("[历史压缩摘要]")


# --- 9. 摘要指令统一：_compress_history 以八段指令为 system prompt -------------
def test_compress_history_uses_summary_instruction_and_strips_duplicate():
    """摘要指令单一口径：_compress_history 的 system prompt = 八段 SUMMARY_INSTRUCTION；
    _CompactionModel 追加的同名指令消息被剔除，不重复混进待压正文。"""
    from omni_core.brain.llm import BrainReply
    from omni_core.local.loop.sdk_bridge import SdkBridgeMixin

    seen = {}

    class _Brain:
        def chat(self, messages, tools=None, tool_choice="auto"):
            seen["msgs"] = messages
            return BrainReply(content="1. 任务目标：无\n2. 已完成动作：无")

    class _Bridge:
        brain_long_task = {"compress": True}

        def _log(self, *a, **k):
            pass

    turns = [[{"role": "user", "content": "旧上下文一行"},
              {"role": "user", "content": SUMMARY_INSTRUCTION}]]
    out = SdkBridgeMixin._compress_history(_Bridge(), _Brain(), turns)
    system_msg, user_msg = seen["msgs"][0], seen["msgs"][1]
    assert system_msg["role"] == "system" and system_msg["content"] == SUMMARY_INSTRUCTION
    assert "150字" not in system_msg["content"]
    assert "关键屏幕状态" not in system_msg["content"]
    assert SUMMARY_INSTRUCTION not in user_msg["content"]
    assert "旧上下文一行" in user_msg["content"]
    assert out[0]["content"].startswith("[历史压缩摘要]")
