"""回归：流式下「每回合独立成块」——修复终态「结果与思考跑到最前、工具调用挤到后面」。

根因（2026-09-23）：块 key 里的回合号 ``seq`` 只在 ``emitter._emit`` 内自增，而流式路径下
``_emit`` 每个 chunk 才被调一次（``_run_loop_streamed`` 结尾）→ 整 chunk 的所有回合共用
``{role}-R0`` / ``{role}-M0`` 一组 key，被 ``_merge_thinking_step`` / ``_merge_message_step``
按 id upsert 合并成「一条巨型思考 + 一条巨型口播」，且位置停在首次出现处（首个工具调用之前）。

覆盖：
1. ``emitter._turn_end`` 切块（下一回合换 key）并清空缓冲（不再跨回合拼接）；
2. ``_run_loop_streamed`` 以「工具调用项」为回合边界逐回合 flush，并回调 ``on_turn_end``；
3. 全程无增量（端点不支持流式）时仍退回整块 ``_parse_items``，旧行为不变。
"""
import types

from omni_core.brain import sdk_loop as sl
from omni_core.local.loop import ToolLoop


def _loop():
    return ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}, verbose=False)


# --- 1. emitter：回合边界切块 + 清缓冲 ---------------------------------------
def test_turn_end_splits_blocks_and_clears_buffer():
    seen = []
    loop = _loop()
    loop.on_llm_delta = lambda role, kind, bid, text, model: seen.append((kind, bid, text))
    emit, delta, turn_end = loop._make_llm_emitter("brain")

    delta("reasoning", "r0-")
    delta("reasoning", "part2")
    delta("message", "m0")
    emit("r0-part2", "m0", [])   # 回合 0 收尾（_emit 内 seq++）
    turn_end()                   # 回合边界（再切块）

    delta("reasoning", "r1")
    delta("message", "m1")

    r_keys = [b for k, b, _ in seen if k == "reasoning"]
    m_keys = [b for k, b, _ in seen if k == "message"]
    # 同回合共用 key（原地增长，前端 upsert 成一条）
    assert r_keys[0] == r_keys[1] == "brain-R0"
    assert m_keys[0] == "brain-M0"
    # 跨回合换 key：不再被合并成同一条
    assert r_keys[-1] != "brain-R0" and m_keys[-1] != "brain-M0"
    # 缓冲已清空：新回合只含本回合增量，不拼接上回合内容
    assert seen[-2][2] == "r1"
    assert seen[-1][2] == "m1"


# --- 2/3. _run_loop_streamed：逐回合 flush --------------------------------
class _Ev:
    def __init__(self, type_, data=None, item=None):
        self.type = type_
        self.data = data
        self.item = item


class _Raw:
    """raw_response_event.data：只带 type/delta（_deltas_of 的契约）。"""

    def __init__(self, type_, delta):
        self.type = type_
        self.delta = delta


class _ToolItem:
    type = "tool_call_item"

    def __init__(self, name, arguments):
        self.raw_item = types.SimpleNamespace(name=name, arguments=arguments)


class _FakeResult:
    def __init__(self, events, raw_responses=None):
        self._events = events
        self.raw_responses = raw_responses or []

    async def stream_events(self):
        for e in self._events:
            yield e


class _FakeRunner:
    def __init__(self, result):
        self._result = result

    def run_streamed(self, agent, items, max_turns=None, hooks=None, run_config=None):
        return self._result


def _drive(monkeypatch, result):
    monkeypatch.setattr(sl, "Runner", _FakeRunner(result))
    finals, deltas, turns = [], [], []
    sl.run_async(sl._run_loop_streamed(
        object(), [], 5, None,
        lambda kind, text: deltas.append((kind, text)),
        lambda r, m, c: finals.append((r, m, c)),
        lambda: turns.append(1),
    ))
    return finals, deltas, turns


def test_streamed_run_flushes_each_turn_at_tool_boundary(monkeypatch):
    events = [
        _Ev("raw_response_event", _Raw("response.reasoning_summary_text.delta", "R0")),
        _Ev("raw_response_event", _Raw("response.output_text.delta", "M0")),
        _Ev("run_item_stream_event", item=_ToolItem("web_search", '{"q":1}')),
        _Ev("raw_response_event", _Raw("response.reasoning_summary_text.delta", "R1")),
        _Ev("raw_response_event", _Raw("response.output_text.delta", "M1")),
    ]
    finals, deltas, turns = _drive(monkeypatch, _FakeResult(events))

    # 两个回合各自成块：不再是整 chunk 一条聚合
    assert finals == [
        ("R0", "M0", [{"name": "web_search", "arguments": '{"q":1}'}]),
        ("R1", "M1", []),
    ]
    # 实时增量原样透传（未改动打字机链路）
    assert deltas == [("reasoning", "R0"), ("message", "M0"),
                      ("reasoning", "R1"), ("message", "M1")]
    # 回合边界 1 次 + 流结束 1 次
    assert len(turns) == 2


def test_streamed_run_falls_back_to_aggregate_without_deltas(monkeypatch):
    """端点不支持流式（全程无 delta）→ 仍按整块解析回传，行为与旧链路一致。"""
    agg = types.SimpleNamespace(output=[
        types.SimpleNamespace(type="message", content=[
            types.SimpleNamespace(type="output_text", text="聚合结论"),
        ]),
    ])
    events = [_Ev("run_item_stream_event", item=types.SimpleNamespace(type="message_output_item"))]
    finals, deltas, turns = _drive(monkeypatch, _FakeResult(events, raw_responses=[agg]))

    assert finals == [("", "聚合结论", [])]
    assert deltas == []
    assert turns == []
