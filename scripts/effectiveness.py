"""K3 有效性裁决 · V3 ablation 聚合器。

读 telemetry 运行报告 + K2 信号聚合，套用判据输出「通过/不通过 + 归因建议」。
判据（设计 §6）：步数降 ≥15% 且成功率不降。

两种用法：
- 自动模式（默认）：读 ``tasks/<tid>/reports/*.json`` 全部 run，按时间切成
  早/晚两半，近似「记忆注入前/后」，比较步数与成功率。
- 显式 ablation（--ablation file.jsonl）：每行 {"group":"on"|"off","steps":int,
  "success":bool}，严格两组对照（用户按设计各跑 ≥10 次）。

失败归因顺序（设计 §6）：轨迹质量 → 蒸馏无信息量 → 注入干扰 → 该域无增益。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omni_core.local import runtime_paths as P  # noqa: E402

STEPS_DROP_THRESHOLD = 0.15   # 步数降 ≥15%
SUCCESS_NOT_DOWN = True       # 成功率不降


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def load_runs_from_telemetry() -> List[Dict[str, Any]]:
    """读全部 telemetry 运行报告（tasks/<tid>/reports/*.json）。"""
    runs: List[Dict[str, Any]] = []
    try:
        for rf in sorted(P.tasks_root().glob("*/*.json")):
            try:
                obj = json.loads(rf.read_text(encoding="utf-8"))
            except Exception:
                continue
            steps = obj.get("steps")
            success = obj.get("success")
            if isinstance(steps, (int, float)) and isinstance(success, bool):
                runs.append({"steps": float(steps), "success": success,
                             "ts": obj.get("run_id", "")})
    except Exception:
        pass
    return runs


def load_runs_from_file(path: str) -> List[Dict[str, Any]]:
    """读显式 ablation 文件（jsonl，每行一组标签）。"""
    runs: List[Dict[str, Any]] = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        o = json.loads(ln)
        grp = o.get("group")
        if grp not in ("on", "off"):
            continue
        runs.append({"group": grp, "steps": float(o["steps"]),
                     "success": bool(o["success"])})
    return runs


# ---------------------------------------------------------------------------
# 聚合 + 判据
# ---------------------------------------------------------------------------
def _agg(runs: List[Dict[str, Any]]) -> Dict[str, float]:
    n = len(runs)
    if n == 0:
        return {"n": 0, "avg_steps": 0.0, "success_rate": 0.0}
    avg_steps = sum(r["steps"] for r in runs) / n
    sr = sum(1 for r in runs if r["success"]) / n
    return {"n": n, "avg_steps": round(avg_steps, 4), "success_rate": round(sr, 4)}


def judge_ablation(baseline: Dict[str, float], treatment: Dict[str, float]) -> Dict[str, Any]:
    """套用 K3 判据：步数降 ≥15% 且成功率不降。"""
    if baseline["n"] == 0 or treatment["n"] == 0:
        return {"pass": False, "reason": "样本不足（两组均需 ≥1 次）",
                "baseline": baseline, "treatment": treatment}
    drop = (baseline["avg_steps"] - treatment["avg_steps"]) / baseline["avg_steps"] \
        if baseline["avg_steps"] else 0.0
    steps_ok = drop >= STEPS_DROP_THRESHOLD
    success_ok = treatment["success_rate"] >= baseline["success_rate"] - 1e-9
    passed = steps_ok and success_ok

    if passed:
        reason = f"通过：步数降 {drop*100:.1f}% ≥15%，成功率 {treatment['success_rate']*100:.0f}% 不降"
        attribution = "记忆注入带来稳态提前（范式推论⑤）"
    else:
        if not steps_ok and not success_ok:
            attribution = "轨迹质量（K2 应拦）→ 蒸馏无信息量（V2 应拦）→ 注入干扰 → 该域无增益"
        elif not steps_ok:
            attribution = "步数未降：可能注入干扰（降强度/换位置）或该域无增益"
        else:
            attribution = "成功率下降：轨迹/蒸馏质量优先排查"
        reason = (f"不通过：步数降 {drop*100:.1f}%（需≥15%）"
                  f"，成功率 {treatment['success_rate']*100:.0f}%"
                  f"（基线 {baseline['success_rate']*100:.0f}%）")

    return {"pass": passed, "steps_drop": round(drop, 4),
            "steps_ok": steps_ok, "success_ok": success_ok,
            "reason": reason, "attribution": attribution,
            "baseline": baseline, "treatment": treatment}


def _split_early_late(runs: List[Dict[str, Any]]) -> (List[Dict], List[Dict]):
    """按时间序（run_id 含时间戳）切早/晚两半，近似注入前/后。"""
    rs = sorted(runs, key=lambda r: str(r.get("ts", "")))
    k = len(rs) // 2
    return rs[:k], rs[k:]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="K3 ablation 聚合器")
    ap.add_argument("--ablation", help="显式 ablation 文件（jsonl，group=on/off）")
    args = ap.parse_args()

    if args.ablation:
        runs = load_runs_from_file(args.ablation)
        off = [r for r in runs if r["group"] == "off"]
        on = [r for r in runs if r["group"] == "on"]
        baseline, treatment = _agg(off), _agg(on)
        label = "显式对照（off=基线, on=实验）"
    else:
        runs = load_runs_from_telemetry()
        early, late = _split_early_late(runs)
        baseline, treatment = _agg(early), _agg(late)
        label = "自动模式（早=基线, 晚=实验，近似注入前/后）"

    verdict = judge_ablation(baseline, treatment)
    out = {"mode": label, **verdict, "n_total": len(runs)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if verdict["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
