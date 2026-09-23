"""M4a.2 telemetry 测试：四指标公式正确，边界安全（0 分母不崩）。"""
from omni_core.local import telemetry


def _run(**kw):
    base = dict(
        run_id="r", objective="o", done_when="", expected=None, backend="emulator",
        brain_model="m1", exec_model="m2", success=True, steps=1,
        brain_calls=0, decision_steps=1, action_count=1, retry_count=0,
        failures=0, recoveries=0, trajectory_file="x",
    )
    base.update(kw)
    return base


def test_success_metrics():
    m = telemetry.compute(_run(success=True, brain_calls=1, decision_steps=10, action_count=5, retry_count=1, failures=0, recoveries=0))
    assert m["success_rate"] == 1.0
    assert abs(m["brain_intervention_rate"] - 0.1) < 1e-9
    assert abs(m["tool_efficiency"] - (4 / 5)) < 1e-9
    assert m["recovery_rate"] == 1.0  # 成功且无失败 -> 1.0


def test_failure_recovery():
    # 1 次失败 + 1 次恢复（升级后完成）
    m = telemetry.compute(_run(success=True, failures=1, recoveries=1))
    assert m["recovery_rate"] == 1.0
    # 失败且未恢复
    m2 = telemetry.compute(_run(success=False, failures=2, recoveries=0))
    assert m2["recovery_rate"] == 0.0
    assert m2["success_rate"] == 0.0


def test_boundary_zero_division():
    # 0 决策步 -> 0.0；0 动作 -> 1.0；0 失败且失败 run -> 0.0
    m = telemetry.compute(_run(success=False, brain_calls=0, decision_steps=0, action_count=0, retry_count=0, failures=0, recoveries=0))
    assert m["brain_intervention_rate"] == 0.0
    assert m["tool_efficiency"] == 1.0
    assert m["recovery_rate"] == 0.0
    assert m["success_rate"] == 0.0


def test_two_layer_low_intervention():
    # 两层：plan(1) + 反射(1) = 2 次在线大脑调用，10 个 worker 决策步 -> 0.2
    m = telemetry.compute(_run(success=True, brain_calls=2, decision_steps=10))
    assert abs(m["brain_intervention_rate"] - 0.2) < 1e-9


def test_emit_report_writes_file(tmp_path):
    run = _run(success=True)
    path = telemetry.emit_run_report(run, str(tmp_path))
    assert path and __import__("pathlib").Path(path).exists()
    import json
    data = json.loads(__import__("pathlib").Path(path).read_text(encoding="utf-8"))
    assert "metrics" in data and data["run_id"] == "r"
