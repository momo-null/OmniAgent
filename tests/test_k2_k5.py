"""K2–K5 单元测试（信号分类 / 一致率 / 聚合 / 校准 / 稳态）。

隔离：把 runtime_paths._GLOBAL 指到 tmp_path，避免污染真实 ~/.omniagent。
"""
import importlib
import json

import pytest

from omni_core.local import runtime_paths


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # 重定向全局持久化根，确保测试零副作用
    monkeypatch.setattr(runtime_paths, "_GLOBAL", tmp_path)
    yield


def _reload():
    from omni_core.local import signals, steady_state
    importlib.reload(signals)
    importlib.reload(steady_state)
    # 单测禁用 LLM 分类器（避免真调 brain），保持离线 + 确定性，走启发式路径
    signals._LLM_CLASSIFIER_BROKEN = True
    return signals, steady_state


# ---------------------------------------------------------------------------
# K2 分类器（启发式，确定性）
# ---------------------------------------------------------------------------
def test_classify_c1():
    from omni_core.local import signals
    # LLM 分类器不可用（测试环境无 brain）时保守兜底 ambiguous，不靠关键字写死场景
    signals._LLM_CLASSIFIER_BROKEN = True
    assert signals.classify_c1("我做了X", "不对，应该是Y") == "ambiguous"
    assert signals.classify_c1("我做了X", "好的，继续") == "ambiguous"
    assert signals.classify_c1("", "") == "ambiguous"


def test_classify_c1_injectable():
    from omni_core.local.signals import classify_c1
    # 注入 LLM 通道：恒返 approved
    assert classify_c1("a", "u", classifier=lambda a, u: "approved") == "approved"
    assert classify_c1("a", "u", classifier=lambda a, u: "refuted") == "refuted"
    assert classify_c1("a", "u", classifier=lambda a, u: "new_task") == "new_task"


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
def test_aggregate_and_query():
    signals, _ = _reload()
    signals.collect_run_signal("t1", "r1", True, "完成了", "目标", "上一轮结论", "好的继续")
    signals.collect_run_signal("t1", "r2", True, "又完成", "目标", "上一轮结论", "不对，重做")
    summary = signals.query_summary()
    assert summary["total"] == 2
    # LLM 分类器不可用（测试环境）→ 保守 ambiguous
    assert summary["c1_dist"]["ambiguous"] == 2
    # 介入频率（交互段 refuted 占比）= 0（无 refuted 判定）
    assert summary["fake_success_rate"]["interactive"] == 0
    assert len(summary["timeline"]) == 2
    # 幂等：重复写入同一 run 不应重复计数
    signals.collect_run_signal("t1", "r1", True, "完成了", "目标", "上一轮结论", "好的继续")
    assert signals.query_summary()["total"] == 2


def test_query_signals_by_task():
    signals, _ = _reload()
    signals.collect_run_signal("t9", "r1", False, "失败", "目标", "上一轮", "重新")
    out = signals.query_signals("t9")
    assert len(out) == 1
    assert out[0]["a"] is False
    assert out[0]["c1"] == "ambiguous"  # LLM 不可用 → 保守


# ---------------------------------------------------------------------------
# K5 稳态
# ---------------------------------------------------------------------------
def test_steady_evaluate_structure():
    signals, steady = _reload()
    # 无数据：应返回结构化结果且 converged=False，不抛异常
    state = steady.evaluate_steady()
    assert "signals" in state
    assert isinstance(state["converged"], bool)
    assert state["domain"].startswith("single-domain")
    # 介入时间线空（无交互样本）
    assert state["signals"]["intervention_timeline"] == []


def test_intervention_timeline():
    signals, steady = _reload()
    # 构造聚合 timeline：交替 refuted/approved
    for i, c1 in enumerate(["refuted", "approved", "refuted", "approved", "refuted"]):
        signals.update_aggregate(_fake_rec(c1))
    tl = steady._intervention_timeline(window=3)
    assert len(tl) == 5
    # 末尾 3 点含 2 个 refuted → 2/3（代码四舍五入到 4 位）
    assert tl[-1] == 0.6667


def _fake_rec(c1: str):
    from omni_core.local.signals import SignalRecord, consistency_contribution
    rec = SignalRecord(
        task_id="t", run_id=f"r{int(__import__('time').time()*1000)%100000}",
        a=True, b="x", c1=c1, mode="interactive",
        consistency=consistency_contribution(True, c1),
        ts="2026-01-01T00:00:00+00:00",
    )
    return rec


