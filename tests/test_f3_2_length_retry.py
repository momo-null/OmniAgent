"""F3.2（P4）— length 空产出兜底重试。

覆盖：
1. 特征识别 _is_length_truncation：ModelBehaviorError + length → True；其它 → False。
2. 首次抛 length、二次正常 → 头部缩减一次（items 的 function_call 轮数下降）且任务继续。
3. 连续两次 length → 原失败路径（escalated + reason 含 length），不无限重试。
4. 非 length 的 400（参数错误）→ 不触发恢复（仅 1 次调用）。
"""
import types

import pytest

from omni_core.brain import sdk_loop as sl


class ModelBehaviorError(Exception):
    """本地同名替身：helper 按类名匹配，无需引入 SDK 真实异常。"""


class BadRequestError(Exception):
    """带 status_code 的 400 替身（供 _provider_status_code 取码）。"""

    def __init__(self, msg):
        super().__init__(msg)
        self.status_code = 400


def _fcall(i):
    return {"type": "function_call", "name": "t", "arguments": "{}", "call_id": str(i)}


def _fout(i):
    return {"type": "function_call_output", "call_id": str(i), "output": "ok"}


class _Gate:
    def __init__(self):
        self.verify_count = 0
        self.has_condition = False

    def verify_done(self):
        return (False, "未完成")

    def peek(self):
        return (False, "")

    def note(self, t):
        return None


def _brain():
    return {"model": "m", "base_url": "http://x", "api_key": "k"}


def _run(monkeypatch, fake_runner, max_steps=4):
    monkeypatch.setattr(sl.Runner, "run", fake_runner)
    return sl.run_subtask_sdk(
        _brain(), instructions="sys", user_input="hi", tools=[], gate=_Gate(),
        max_steps=max_steps, chunk_turns=1, should_stop=lambda: False,
    )


def _n_fcalls(items):
    return sum(1 for it in items if sl._kind(it) == "function_call")


# --- 1. 特征识别 --------------------------------------------------------------
def test_is_length_truncation_recognizes():
    assert sl._is_length_truncation(
        ModelBehaviorError("Chat Completions response terminated with finish_reason='length' ...")
    ) is True
    # 非 length 文案的 ModelBehaviorError → 不识别
    assert sl._is_length_truncation(ModelBehaviorError("no choices")) is False
    # 类名不符（哪怕含 length）→ 不识别
    assert sl._is_length_truncation(ValueError("length exceeded")) is False
    assert sl._is_length_truncation(None) is False


# --- 2. 首次 length、二次正常 → 缩减一次且继续 --------------------------------
def test_length_retries_once_with_reduced_items(monkeypatch):
    captured = []

    async def _fake(agent, items, max_turns=None, hooks=None, run_config=None):
        captured.append(list(items))
        n = len(captured)
        if n == 2:
            raise ModelBehaviorError(
                "Chat Completions response terminated with finish_reason='length' "
                "but produced no assistant text, tool call, or refusal.")
        if n == 1:
            # 第 1 块正常返回：累积 12 轮 function_call（后续块入参因此变长）
            items12 = []
            for i in range(12):
                items12.append(_fcall(i))
                items12.append(_fout(i))
            return types.SimpleNamespace(to_input_list=lambda: items12)
        return types.SimpleNamespace(to_input_list=lambda: [{"role": "user", "content": "go"}])

    res = _run(monkeypatch, _fake)

    assert len(captured) >= 3, "length 异常后应发生一次重试，任务继续"
    assert _n_fcalls(captured[0]) == 0, "初始块无 function_call"
    assert _n_fcalls(captured[1]) == 12, "报错块已累积 12 轮 function_call"
    assert _n_fcalls(captured[2]) <= 8, "重试块应做头部硬滑窗缩减（保留 ≤8 轮）"
    # 任务继续推进到步数预算耗尽，而非被 length 直接升级终止
    assert res["escalated"] is False
    assert "budget_exhausted" in res["reason"]


# --- 3. 连续多次 length → 限次重试后原失败路径 + reason ------------------------
def test_two_length_errors_escalate_with_reason(monkeypatch):
    captured = []

    async def _fake(agent, items, max_turns=None, hooks=None, run_config=None):
        captured.append(list(items))
        raise ModelBehaviorError(
            "Chat Completions response terminated with finish_reason='length' "
            "but produced no assistant text, tool call, or refusal.")

    res = _run(monkeypatch, _fake)

    # A2（2026-09-21）：length 重试上限 LENGTH_RETRY_LIMIT=3——
    # 前 3 次命中走「截历史 + 追加纠正提示」重试；第 3 次耗尽后升级失败。
    assert len(captured) == 3, "限次重试（LENGTH_RETRY_LIMIT=3），不得无限循环"
    assert res["escalated"] is True
    assert "length" in res["reason"], "失败原因应落 length 信号（衔接 F1.3）"
    # 重试时应追加纠正提示（A2：让弱模型跳出空转）
    assert any("截断" in str(it.get("content", "")) for it in captured[-1]
               if isinstance(it, dict)), "重试 items 应含 length 纠正提示"


# --- 4. 非 length 的 400 → 不触发恢复 -----------------------------------------
def test_non_length_400_not_recovered(monkeypatch):
    captured = []

    async def _fake(agent, items, max_turns=None, hooks=None, run_config=None):
        captured.append(list(items))
        raise BadRequestError("invalid parameter: tools schema malformed")

    res = _run(monkeypatch, _fake)

    assert len(captured) == 1, "非 length 的 400 不应触发缩减重试"
    assert res["success"] is False
