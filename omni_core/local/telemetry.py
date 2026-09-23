"""M4a.2 Evaluation / telemetry 指标。

把「agent 是否变强」变成可量化数字（两份评审 Review-001/003/006）。
输入为 ``RunRecord.to_dict()``（来自 M4a.1 TrajectoryStore），输出四指标 + 报告。

指标语义：
- success_rate           : 成功 run 占比（跨多次 run 聚合时用）
- brain_intervention_rate: brain_calls / decision_steps，越低 = 本地执行器越自主（核心）
- tool_efficiency        : (action_count - retry_count) / action_count，无效动作越少越高
- recovery_rate          : recoveries / failures，失败后自主恢复能力
所有除法均做边界保护（0 分母 -> 0.0；success_rate 单 run 时直接用 success 布尔）。
"""
import json
from pathlib import Path
from typing import Dict, Any, Optional


def compute(run: Dict[str, Any]) -> Dict[str, float]:
    """由单条 RunRecord 计算四指标（单 run 的 success_rate 即 success 布尔转 0/1）。"""
    brain_calls = run.get("brain_calls", 0)
    decision_steps = run.get("decision_steps", 0) or run.get("steps", 0)
    action_count = run.get("action_count", 0)
    retry_count = run.get("retry_count", 0)
    failures = run.get("failures", 0)
    recoveries = run.get("recoveries", 0)

    success = bool(run.get("success", False))
    success_rate = 1.0 if success else 0.0

    brain_intervention_rate = (brain_calls / decision_steps) if decision_steps > 0 else 0.0
    tool_efficiency = ((action_count - retry_count) / action_count) if action_count > 0 else 1.0
    recovery_rate = (recoveries / failures) if failures > 0 else (1.0 if success else 0.0)

    return {
        "success_rate": success_rate,
        "brain_intervention_rate": brain_intervention_rate,
        "tool_efficiency": tool_efficiency,
        "recovery_rate": recovery_rate,
    }


def emit_run_report(run: Dict[str, Any], report_dir: str) -> Optional[str]:
    """写 ``<report_dir>/<run_id>.json``（人类 + 未来 Curator 可读）。返回路径或 None。"""
    try:
        d = Path(report_dir)
        d.mkdir(parents=True, exist_ok=True)
        metrics = compute(run)
        payload = {
            "run_id": run.get("run_id"),
            "objective": run.get("objective"),
            "success": run.get("success"),
            "steps": run.get("steps"),
            "brain_calls": run.get("brain_calls"),
            "decision_steps": run.get("decision_steps"),
            "action_count": run.get("action_count"),
            "retry_count": run.get("retry_count"),
            "failures": run.get("failures"),
            "recoveries": run.get("recoveries"),
            "metrics": metrics,
            "trajectory_file": run.get("trajectory_file"),
        }
        path = d / f"{run.get('run_id')}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return str(path)
    except Exception:
        return None
