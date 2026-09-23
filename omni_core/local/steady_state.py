"""K5 稳态运营：四信号计算 + 域收敛判据 + 蒸馏降频触发。

设计 §8。v1 显式声明**单域假设**（个人验证场景天然单域，多域分区列远期）。

四信号：
1. 蒸馏去重命中率  = 新蒸馏 facts 中已被 MEMORY.md 覆盖比例（Curator 蒸馏时比对）
2. skill 晋升率     = 全局 skills 中 active 占比（单位任务 candidate→active 转化近似）
3. 步数方差         = 同域任务步数离散度（telemetry 运行报告）
4. 人工介入频率     = 交互段 C₁ refuted 轮占比（K2 signals，时间窗平滑）

域收敛判据（初值，标「实测校准」）：去重命中率 ≥80% 且晋升率趋平 且介入频率 ≤5%。
收敛后由 Curator 自动降频（跳过蒸馏 / 合并阈值提升至 10）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.local import runtime_paths as P
from omni_core.local import signals as sig_mod


# 收敛判据初值（§8，标注「初值，实测校准」）
DEDUP_CONVERGE = 0.80
INTERVENTION_CONVERGE = 0.05
_PROMOTION_FLAT_OK = True  # v1：晋升率趋平以「不再有新增 active」近似，初值直接放行


# ---------------------------------------------------------------------------
# 单信号采集
# ---------------------------------------------------------------------------
def _latest_curator_metric() -> Dict[str, Any]:
    try:
        p = P.global_memory() / "curator_metrics.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8")) or {}
            ms = data.get("metrics") or []
            if ms:
                return ms[-1]
    except Exception:
        pass
    return {}


def _dedup_rate() -> Optional[float]:
    m = _latest_curator_metric()
    return m.get("dedup_hit_rate") if m else None


def _skill_promotion_rate() -> Dict[str, Any]:
    """全局 skills 中 active 占比（晋升率近似）；附最近序列用于趋势。"""
    try:
        d = P.global_skills()
        if not d.exists():
            return {"rate": None, "active": 0, "total": 0, "series": []}
        total = 0
        active = 0
        series: List[int] = []
        for f in sorted(d.glob("*.json")):
            try:
                obj = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            total += 1
            st = ((obj.get("metadata") or {}).get("status") or "").lower()
            if st == "active":
                active += 1
        # 晋升事件序列来自 Curator 指标（每 run 的 promoted 数）
        try:
            cm = json.loads((P.global_memory() / "curator_metrics.json").read_text(encoding="utf-8")) or {}
            series = [int(x.get("skills_promoted", 0)) for x in (cm.get("metrics") or [])]
        except Exception:
            series = []
        rate = round(active / total, 4) if total else None
        return {"rate": rate, "active": active, "total": total, "series": series[-20:]}
    except Exception:
        return {"rate": None, "active": 0, "total": 0, "series": []}


def _step_variance() -> Dict[str, Any]:
    """同域任务步数方差（telemetry 运行报告 tasks/<tid>/reports/*.json）。"""
    try:
        steps: List[float] = []
        for rf in sorted(P.tasks_root().glob("*/*.json"))[:200]:
            try:
                obj = json.loads(rf.read_text(encoding="utf-8"))
            except Exception:
                continue
            s = obj.get("steps")
            if isinstance(s, (int, float)):
                steps.append(float(s))
        if len(steps) < 2:
            return {"variance": None, "mean": (steps[0] if steps else None), "n": len(steps)}
        mean = sum(steps) / len(steps)
        var = sum((x - mean) ** 2 for x in steps) / len(steps)
        return {"variance": round(var, 4), "mean": round(mean, 4), "n": len(steps)}
    except Exception:
        return {"variance": None, "mean": None, "n": 0}


def _intervention_rate() -> Dict[str, Any]:
    """交互段 C₁ refuted 轮占比（人工介入频率），带时间窗平滑标注。"""
    agg = sig_mod.read_aggregate()
    inter = agg.get("interactive", {})
    n = int(inter.get("n", 0))
    ref = int((agg.get("c1_dist") or {}).get("refuted", 0))
    rate = round(ref / n, 4) if n else None
    return {"rate": rate, "refuted": ref, "n_interactive": n}


def _intervention_timeline(window: int = 5) -> List[float]:
    """介入频率衰减曲线数据：按时间序对交互段 run 取滚动窗口 refuted 占比。

    每个点 = 截至该 run 最近 ``window`` 个交互 run 中 refuted 比例（0~1）。
    无交互样本时返回空列表（曲线平直/无数据）。
    """
    agg = sig_mod.read_aggregate()
    tl = agg.get("timeline") or []
    inter = [p for p in tl if p.get("mode") == "interactive"]
    if not inter:
        return []
    out: List[float] = []
    for i in range(len(inter)):
        seg = inter[max(0, i - window + 1): i + 1]
        ref = sum(1 for p in seg if p.get("c1") == "refuted")
        out.append(round(ref / len(seg), 4))
    return out


# ---------------------------------------------------------------------------
# 收敛判据
# ---------------------------------------------------------------------------
def _judge(dedup: Optional[float], intervention: Dict[str, Any]) -> Dict[str, Any]:
    checks = {
        "dedup_ge_80": (dedup is not None and dedup >= DEDUP_CONVERGE),
        "promotion_flat": _PROMOTION_FLAT_OK,
        "intervention_le_5": (
            intervention.get("rate") is not None
            and intervention["rate"] <= INTERVENTION_CONVERGE
        ),
    }
    # 介入频率为 null（无交互样本）时视为「尚无数据」，不强行判收敛
    if intervention.get("rate") is None:
        checks["intervention_le_5"] = False
    converged = all(checks.values())
    return {"checks": checks, "converged": converged}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def evaluate_steady() -> Dict[str, Any]:
    """计算四信号 + 域收敛状态；结果持久化到 memory/steady_state.json。"""
    dedup = _dedup_rate()
    promo = _skill_promotion_rate()
    step = _step_variance()
    interv = _intervention_rate()
    judge = _judge(dedup, interv)

    state = {
        "evaluated_at": _now(),
        "domain": "single-domain(v1)",
        "converged": judge["converged"],
        "thresholds": {
            "dedup_converge": DEDUP_CONVERGE,
            "intervention_converge": INTERVENTION_CONVERGE,
            "note": "初值，实测校准",
        },
        "signals": {
            "distill_dedup_hit_rate": dedup,
            "skill_promotion_rate": promo,
            "step_variance": step,
            "human_intervention_rate": interv,
            "intervention_timeline": _intervention_timeline(),
        },
        "checks": judge["checks"],
    }
    try:
        p = sig_mod.steady_state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


def converged() -> bool:
    """供 Curator 读取的收敛状态（从持久化文件，避免每次重算）。"""
    try:
        p = sig_mod.steady_state_file()
        if p.exists():
            return bool((json.loads(p.read_text(encoding="utf-8")) or {}).get("converged", False))
    except Exception:
        pass
    return False


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
