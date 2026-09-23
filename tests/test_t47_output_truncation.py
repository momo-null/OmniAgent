"""T4.7：输出截断可观测（_OutputTruncationModel）专属单测。

覆盖：usage 触顶触发 / 未触顶不触发 / 旧字段名兼容 / 无 usage 静默跳过 /
流式 response.completed 判定 / 回调抛异常不影响主流程。
"""
from types import SimpleNamespace

from omni_core.async_bridge import run_async
from omni_core.brain.sdk_loop import _OutputTruncationModel


class _FakeInner:
    """最小 Model 替身：只提供 get_response / stream_response。"""

    def __init__(self, resp=None, chunks=None):
        self.resp = resp
        self.chunks = chunks or []
        self.calls = 0

    async def get_response(self, *args, **kwargs):
        self.calls += 1
        return self.resp

    async def stream_response(self, *args, **kwargs):
        for c in self.chunks:
            yield c


def _resp(output_tokens=None, legacy=False):
    if output_tokens is None:
        return SimpleNamespace(usage=None)
    if legacy:
        return SimpleNamespace(usage=SimpleNamespace(completion_tokens=output_tokens))
    return SimpleNamespace(usage=SimpleNamespace(output_tokens=output_tokens))


def test_triggers_when_output_hits_limit():
    got = []
    m = _OutputTruncationModel(_FakeInner(_resp(2048)), 2048, lambda u, l: got.append((u, l)))
    run_async(m.get_response())
    assert got == [(2048, 2048)]


def test_no_trigger_when_below_limit():
    got = []
    m = _OutputTruncationModel(_FakeInner(_resp(1000)), 2048, lambda u, l: got.append((u, l)))
    run_async(m.get_response())
    assert got == []


def test_legacy_usage_field_compatible():
    got = []
    m = _OutputTruncationModel(_FakeInner(_resp(512, legacy=True)), 512, lambda u, l: got.append((u, l)))
    run_async(m.get_response())
    assert got == [(512, 512)]


def test_missing_usage_is_silent():
    got = []
    m = _OutputTruncationModel(_FakeInner(_resp(None)), 2048, lambda u, l: got.append((u, l)))
    run_async(m.get_response())  # 不抛异常即通过
    assert got == []


def test_zero_limit_disables_detection():
    got = []
    m = _OutputTruncationModel(_FakeInner(_resp(9999)), 0, lambda u, l: got.append((u, l)))
    run_async(m.get_response())
    assert got == []


def test_stream_completed_event_checked():
    got = []
    chunks = [
        SimpleNamespace(type="response.output_text.delta"),
        SimpleNamespace(type="response.completed", response=_resp(300)),
    ]
    m = _OutputTruncationModel(_FakeInner(chunks=chunks), 300, lambda u, l: got.append((u, l)))

    async def _drain():
        return [c async for c in m.stream_response()]

    out = run_async(_drain())
    assert len(out) == 2  # 事件原样透传
    assert got == [(300, 300)]


def test_callback_exception_does_not_break_flow():
    def _boom(u, l):
        raise RuntimeError("boom")

    m = _OutputTruncationModel(_FakeInner(_resp(2048)), 2048, _boom)
    assert run_async(m.get_response()) is not None  # 异常被吞，响应照常返回
