"""K2–K5 单元测试（信号分类 / 一致率 / 聚合 / 校准 / 稳态）。

隔离：把 runtime_paths._GLOBAL 指到 tmp_path，避免污染真实 ~/.omniagent；
LLM 分类器经 monkeypatch 显式关闭（不真调 brain），离线 + 确定性。
"""
import importlib

import pytest

from omni_core.local import runtime_paths


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # 重定向全局持久化根，确保测试零副作用
    monkeypatch.setattr(runtime_paths, "_GLOBAL", tmp_path)
    yield


def _reload(monkeypatch):
    from omni_core.local import signals, steady_state
    importlib.reload(signals)
    importlib.reload(steady_state)
    # 单测禁用 LLM 分类器（避免真调 brain），保持离线 + 确定性，走保守 ambiguous 兜底
    monkeypatch.setattr(signals, "_llm_classifier_enabled", lambda: False)
    return signals, steady_state


# ---------------------------------------------------------------------------
# K2 分类器（确定性路径：classifier 注入 / 关闭兜底）
# ---------------------------------------------------------------------------
def test_classify_c1(monkeypatch):
    from omni_core.local import signals
    # LLM 分类器关闭（runtime.signals.llm_classifier=false）时保守兜底 ambiguous，
    # 不靠关键字写死场景
    monkeypatch.setattr(signals, "_llm_classifier_enabled", lambda: False)
    assert signals.classify_c1("我做了X", "不对，应该是Y") == "ambiguous"
    assert signals.classify_c1("我做了X", "好的，继续") == "ambiguous"
    assert signals.classify_c1("", "") == "ambiguous"


def test_classify_c1_injectable():
    from omni_core.local.signals import classify_c1
    # 注入 LLM 通道：恒返 approved（注入优先于开关/熔断）
    assert classify_c1("a", "u", classifier=lambda a, u: "approved") == "approved"
    assert classify_c1("a", "u", classifier=lambda a, u: "refuted") == "refuted"
    assert classify_c1("a", "u", classifier=lambda a, u: "new_task") == "new_task"


def test_llm_call_breaker_window(monkeypatch):
    signals, _ = _reload(monkeypatch)
    import time as _time
    # 熔断窗口内：直接短路返回 None，不做任何 LLM 调用
    monkeypatch.setattr(signals, "_LLM_CLASSIFIER_BROKEN", True)
    monkeypatch.setattr(signals, "_LLM_CLASSIFIER_BROKEN_AT", _time.monotonic())
    assert signals._llm_call("system", "user") is None


def test_classify_c2():
    from omni_core.local.signals import classify_c2
    assert classify_c2("生成报告", "任务完成，已生成 report.pdf") == "success"
    assert classify_c2("生成报告", "错误：未找到模板文件") == "fail"
    assert classify_c2("生成报告", "") == "unknown"
    assert classify_c2("生成报告", "一些无关文本", classifier=lambda o, t: "unknown") == "unknown"


def test_consistency_contribution():
    from omni_core.local.signals import consistency_contribution
    assert consistency_contribution(True, "refuted")["fake_success"] == 1
    assert consistency_contribution(False, "success")["miss_rate"] == 1
    assert consistency_contribution(True, "success")["divergence"] == 0
    assert consistency_contribution(True, "unknown")["uncertain"] == 1


def test_calibrate_c1c2():
    from omni_core.local.signals import calibrate_c1c2
    sample = [
        {"predicted": "approved", "human": "approved"},
        {"predicted": "refuted", "human": "refuted"},
        {"predicted": "approved", "human": "refuted"},  # 1 不一致
    ]
    assert calibrate_c1c2(sample) == 0.3333
    assert calibrate_c1c2([]) == 1.0


# ---------------------------------------------------------------------------
# K2 聚合 + 查询
# ---------------------------------------------------------------------------
def test_aggregate_and_query(monkeypatch):
    signals, _ = _reload(monkeypatch)
    signals.collect_run_signal("t1", "r1", True, "完成了", "目标", "上一轮结论", "好的继续")
    signals.collect_run_signal("t1", "r2", True, "又完成", "目标", "上一轮结论", "不对，重做")
    summary = signals.query_summary()
    assert summary["total"] == 2
    # LLM 分类器关闭（单测）→ 保守 ambiguous
    assert summary["c1_dist"]["ambiguous"] == 2
    # ambiguous 计入 uncertain，不虚高 fake_success
    assert summary["fake_success_rate"]["interactive"] == 0
    assert len(summary["timeline"]) == 2
    # 幂等：重复写入同一 run 不应重复计数
    signals.collect_run_signal("t1", "r1", True, "完成了", "目标", "上一轮结论", "好的继续")
    assert signals.query_summary()["total"] == 2


def test_query_signals_by_task(monkeypatch):
    signals, _ = _reload(monkeypatch)
    signals.collect_run_signal("t9", "r1", False, "失败", "目标", "上一轮", "重新")
    out = signals.query_signals("t9")
    assert len(out) == 1
    assert out[0]["a"] is False
    assert out[0]["c1"] == "ambiguous"  # LLM 关闭 → 保守


# ---------------------------------------------------------------------------
# K5 稳态
# ---------------------------------------------------------------------------
def test_steady_evaluate_structure(monkeypatch):
    signals, steady = _reload(monkeypatch)
    # 无数据：应返回结构化结果且 converged=False，不抛异常
    state = steady.evaluate_steady()
    assert "signals" in state
    assert isinstance(state["converged"], bool)
    assert state["domain"].startswith("single-domain")
    # 介入时间线空（无可判交互样本）
    assert state["signals"]["intervention_timeline"] == []


def test_intervention_rate_ignores_ambiguous(monkeypatch):
    signals, steady = _reload(monkeypatch)
    # 全 ambiguous（LLM 不可用兜底的典型形态）→ rate=None，介入频率判据不得假通过
    for i in range(3):
        signals.update_aggregate(_fake_rec("ambiguous", f"amb{i}"))
    interv = steady._intervention_rate()
    assert interv["judged"] == 0
    assert interv["rate"] is None
    state = steady.evaluate_steady()
    assert state["checks"]["intervention_le_5"] is False
    # 混入可判轮：2 refuted + 6 approved → judged=8，rate=0.25（ambiguous 不进分母）
    for i in range(2):
        signals.update_aggregate(_fake_rec("refuted", f"ref{i}"))
    for i in range(6):
        signals.update_aggregate(_fake_rec("approved", f"app{i}"))
    interv = steady._intervention_rate()
    assert interv["judged"] == 8
    assert interv["refuted"] == 2
    assert interv["ambiguous"] == 3
    assert interv["n_interactive"] == 11
    assert interv["rate"] == 0.25


def test_intervention_timeline(monkeypatch):
    signals, steady = _reload(monkeypatch)
    # 构造聚合 timeline：交替 refuted/approved
    for i, c1 in enumerate(["refuted", "approved", "refuted", "approved", "refuted"]):
        signals.update_aggregate(_fake_rec(c1, f"tl{i}"))
    # ambiguous（LLM 不可用兜底）不计入衰减曲线窗口，避免画出假 0 衰减
    signals.update_aggregate(_fake_rec("ambiguous", "tlamb"))
    tl = steady._intervention_timeline(window=3)
    assert len(tl) == 5
    # 末尾 3 点含 2 个 refuted → 2/3（代码四舍五入到 4 位）
    assert tl[-1] == 0.6667


def _fake_rec(c1: str, rid: str = ""):
    from omni_core.local.signals import SignalRecord, consistency_contribution
    if not rid:
        rid = f"r{int(__import__('time').time()*1000)%100000}"
    rec = SignalRecord(
        task_id="t", run_id=rid,
        a=True, b="x", c1=c1, mode="interactive",
        consistency=consistency_contribution(True, c1),
        ts="2026-01-01T00:00:00+00:00",
    )
    return rec
