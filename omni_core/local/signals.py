"""K2 信号基础设施：三实体一致率模型 + 三级 C 真值 + 聚合基线。

设计：知识级自升级 K 系列 §5。纯前向采集（仅在任务运行时内嵌采集，不回溯历史、
不处理 30 天窗口），只读 trajectory/world_model，零写入任务原始数据。

三实体：
- A  系统 success：run 结束 success 布尔（来自 ``tool_loop._finish`` 的 result）。
- B  模型自报：assistant 结论口播（本轮 agent 终态文本）。
- C  真值：交互段用 C₁（下一轮 user 消息裁决）/ 自主段用 C₂（终态观测评审）。

分类器默认启发式实现（确定性、零额外 LLM 成本、可单测）；``classifier`` 参数可注入
LLM 通道（设计 §5.6.3「复用 config brain」）以升级精度，校准用 C₃ 人工抽检比对。
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from omni_core.local import runtime_paths as P


C1_LABELS = ("approved", "refuted", "new_task", "ambiguous")
C2_LABELS = ("success", "fail", "unknown")

_SIGNAL_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.signal\.json$")


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def memory_signals_aggregate() -> Path:
    """聚合基线 ``memory/signals_aggregate.json``（仅信号层可写，可重算）。"""
    return P.global_memory() / "signals_aggregate.json"


def steady_state_file() -> Path:
    """K5 稳态判据持久化 ``memory/steady_state.json``。"""
    return P.global_memory() / "steady_state.json"


def signal_file(task_id: str, run_id: str) -> Path:
    """单 run 派生信号 ``tasks/<tid>/<run_id>.signal.json``。"""
    from omni_core.local.runtime_paths import validate_identifier
    tid = validate_identifier(task_id, "task_id")
    rid = validate_identifier(run_id, "run_id")
    return P.task_dir(tid) / f"{rid}.signal.json"


# ---------------------------------------------------------------------------
# 运行信号记录
# ---------------------------------------------------------------------------
@dataclass
class SignalRecord:
    task_id: str
    run_id: str
    a: bool                       # 系统 success
    b: str                       # 模型自报（assistant 结论）
    c1: str = "unknown"          # 对话批准（交互段）
    c2: str = "unknown"          # 观测评审（自主段）
    mode: str = "interactive"    # interactive / autonomous（由 c1/c2 来源判定）
    labels: Dict[str, Any] = field(default_factory=dict)
    consistency: Dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignalRecord":
        return cls(
            task_id=d.get("task_id", ""),
            run_id=d.get("run_id", ""),
            a=bool(d.get("a", False)),
            b=str(d.get("b", "")),
            c1=d.get("c1", "unknown"),
            c2=d.get("c2", "unknown"),
            mode=d.get("mode", "interactive"),
            labels=d.get("labels", {}) or {},
            consistency=d.get("consistency", {}) or {},
            ts=d.get("ts", ""),
        )


# ---------------------------------------------------------------------------
# 分类器（默认启发式；可注入 LLM 通道升级）
# ---------------------------------------------------------------------------
_REFUTE_MARKERS = [
    "不对", "错了", "不正确", "不是这样", "wrong", "incorrect", "你应该",
    "重新", "重做", "别", "不要", "没做到", "没完成", "失败", "没成功",
]
_APPROVE_MARKERS = [
    "继续", "好的", "可以", "是的", "对", "ok", "yes", "没问题", "收到",
    "明白了", "正确", "没问题了", "就这样",
]
_NEW_TASK_MARKERS = ["新任务", "帮我做", "请帮我", "请做", "现在做", "开始做", "再帮我"]


def classify_c1(agent_msg: str, next_user_msg: str,
                classifier: Optional[Callable[[str, str], str]] = None) -> str:
    """C₁ 对话批准：agent 轮后下一个 user 消息即裁决。

    approved / refuted / new_task / ambiguous。``classifier`` 可注入 LLM 实现。
    """
    if classifier is not None:
        try:
            lab = classifier(agent_msg or "", next_user_msg or "")
            if lab in C1_LABELS:
                return lab
        except Exception:
            pass
    nu = (next_user_msg or "").strip()
    if not nu:
        return "ambiguous"
    if any(m in nu for m in _REFUTE_MARKERS):
        return "refuted"
    if any(m in nu for m in _APPROVE_MARKERS):
        return "approved"
    if any(m in nu for m in _NEW_TASK_MARKERS):
        return "new_task"
    # 默认：短消息视为认可续做，长指令视为新任务
    return "new_task" if len(nu) > 30 else "approved"


_FAIL_MARKERS = ["失败", "错误", "error", "exception", "未达成", "未找到", "超时", "traceback"]
_SUCC_MARKERS = ["完成", "成功", "done", "success", "达成", "已生成", "已创建", "ok"]


def classify_c2(objective: str, terminal_observation: str,
                classifier: Optional[Callable[[str, str], str]] = None) -> str:
    """C₂ 观测评审：仅 objective + 终态观测原文，**绝不给**轨迹/推理/结论。

    success / fail / unknown。``classifier`` 可注入 LLM 实现。
    """
    if classifier is not None:
        try:
            lab = classifier(objective or "", terminal_observation or "")
            if lab in C2_LABELS:
                return lab
        except Exception:
            pass
    obs = (terminal_observation or "").strip()
    if not obs:
        return "unknown"
    if any(m in obs for m in _FAIL_MARKERS):
        return "fail"
    obj_kw = re.findall(r"[\w\u4e00-\u9fff]{2,}", objective or "")
    obj_hit = any(kw in obs for kw in obj_kw[:8])
    if obj_hit or any(m in obs for m in _SUCC_MARKERS):
        return "success"
    return "unknown"


# ---------------------------------------------------------------------------
# 一致性贡献（§5.3）
# ---------------------------------------------------------------------------
def consistency_contribution(a_success: bool, c_label: str) -> Dict[str, int]:
    """单条记录对一致率指标的贡献计数（供聚合累加）。

    - fake_success：A=true 且 C 证伪（refuted/fail）
    - miss_rate    ：A=false 且 C 证真（approved/success）
    - divergence    ：A 与 C 证真性不一致（分歧点）
    - uncertain     ：C 不可判（unknown/ambiguous）
    """
    c_neg = c_label in ("refuted", "fail")
    c_pos = c_label in ("approved", "success")
    decidable = c_neg or c_pos
    return {
        "fake_success": 1 if (a_success and c_neg) else 0,
        "miss_rate": 1 if (not a_success and c_pos) else 0,
        # 仅当 C 可判时才算分歧；C=unknown/ambiguous 计入 uncertain，不虚高分歧率
        "divergence": 1 if (decidable and (a_success != c_pos)) else 0,
        "uncertain": 1 if c_label in ("unknown", "ambiguous") else 0,
        "n": 1,
    }


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


# ---------------------------------------------------------------------------
# 读写（派生层，可重算）
# ---------------------------------------------------------------------------
def write_run_signal(rec: SignalRecord) -> Optional[Path]:
    """写单 run 信号；异常静默返回 None。"""
    try:
        p = signal_file(rec.task_id, rec.run_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return p
    except Exception:
        return None


def read_run_signal(task_id: str, run_id: str) -> Optional[SignalRecord]:
    try:
        p = signal_file(task_id, run_id)
        if p.exists():
            return SignalRecord.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        pass
    return None


def read_aggregate() -> Dict[str, Any]:
    try:
        p = memory_signals_aggregate()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _blank_bucket() -> Dict[str, int]:
    return {"fake_success": 0, "miss_rate": 0, "divergence": 0,
            "uncertain": 0, "n": 0}


def update_aggregate(rec: SignalRecord) -> Dict[str, Any]:
    """把一条 run 信号累加进聚合基线（幂等：同 run_id 先减旧贡献再加新）。"""
    agg = read_aggregate()
    agg.setdefault("interactive", _blank_bucket())
    agg.setdefault("autonomous", _blank_bucket())
    agg.setdefault("c1_dist", {k: 0 for k in C1_LABELS})
    agg.setdefault("c2_dist", {k: 0 for k in C2_LABELS})
    agg.setdefault("seen_runs", [])

    run_key = f"{rec.task_id}/{rec.run_id}"
    # 幂等：以 seen_runs 列表判定（信号文件已先于本函数写出，不可再回读文件判定）
    already = run_key in agg.get("seen_runs", [])
    if already:
        old = read_run_signal(rec.task_id, rec.run_id)
        if old is not None:
            _apply_bucket(agg[old.mode], old.consistency, -1)
            if old.mode == "interactive":
                agg["c1_dist"][old.c1] = max(0, agg["c1_dist"].get(old.c1, 0) - 1)
            else:
                agg["c2_dist"][old.c2] = max(0, agg["c2_dist"].get(old.c2, 0) - 1)

    bucket = agg[rec.mode]
    _apply_bucket(bucket, rec.consistency, 1)
    if rec.mode == "interactive":
        agg["c1_dist"][rec.c1] = agg["c1_dist"].get(rec.c1, 0) + 1
    else:
        agg["c2_dist"][rec.c2] = agg["c2_dist"].get(rec.c2, 0) + 1

    if not already:
        agg["seen_runs"].append(run_key)
    agg["seen_runs"] = agg["seen_runs"][-500:]  # 控制体积

    # 时间序列（供 K5 衰减曲线）：每条 run 追加一点，前端按窗口平滑绘制
    agg.setdefault("timeline", [])
    agg["timeline"].append({
        "ts": rec.ts, "c1": rec.c1, "c2": rec.c2, "a": rec.a, "mode": rec.mode,
    })
    agg["timeline"] = agg["timeline"][-500:]

    agg["updated_at"] = datetime.now(timezone.utc).isoformat()
    agg["total"] = agg.get("total", 0) + (0 if already else 1)

    # 派生指标（§5.3，带置信标注）
    agg["fake_success_rate"] = _derive_rate(agg, "fake_success")
    agg["miss_rate"] = _derive_rate(agg, "miss_rate")
    agg["divergence_rate"] = _derive_rate(agg, "divergence")
    agg["uncertain_rate"] = _derive_rate(agg, "uncertain")
    agg["c1_calibration_error"] = agg.get("c1_calibration_error")  # C₃ 人工填写
    try:
        memory_signals_aggregate().parent.mkdir(parents=True, exist_ok=True)
        memory_signals_aggregate().write_text(
            json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return agg


def _apply_bucket(bucket: Dict[str, int], contrib: Dict[str, Any], sign: int) -> None:
    for k in ("fake_success", "miss_rate", "divergence", "uncertain", "n"):
        bucket[k] = bucket.get(k, 0) + sign * int(contrib.get(k, 0))


def _derive_rate(agg: Dict[str, Any], key: str) -> Dict[str, Any]:
    """按模式分解的率 + 置信（样本数）。"""
    def _r(b: Dict[str, int]) -> Optional[float]:
        return _rate(b.get(key, 0), b.get("n", 0))
    return {
        "interactive": _r(agg.get("interactive", {})),
        "autonomous": _r(agg.get("autonomous", {})),
        "overall": _r({
            "fake_success": agg.get("interactive", {}).get(key, 0) + agg.get("autonomous", {}).get(key, 0),
            "n": agg.get("interactive", {}).get("n", 0) + agg.get("autonomous", {}).get("n", 0),
        }),
        "n_interactive": agg.get("interactive", {}).get("n", 0),
        "n_autonomous": agg.get("autonomous", {}).get("n", 0),
    }


# ---------------------------------------------------------------------------
# 便捷采集（后端 _dispatch_chat 调用）
# ---------------------------------------------------------------------------
def read_terminal_observation(task_id: str, run_id: str = "") -> str:
    """读终态观测原文（只读，绝不返回轨迹/推理/结论）。

    TrajectoryStore 实际落盘为 ``tasks/<tid>/<date>_<run_id>.jsonl``（见
    ``omni_core/local/trajectory.py``），故先按 run_id 定位，再回退通用名。
    仅取观测字段（state_text / facts / 工具原始 stdout），
    **排除 observation.objective 自身**（否则目标词必然命中，C₂ 恒判 success），
    并跳过 brain 推理行（kind/role）。
    """
    try:
        d = P.task_dir(task_id)
        if not d.exists():
            return ""
        cand: List[Path] = []
        if run_id:
            cand = sorted(d.glob(f"*_{run_id}.jsonl"))
        if not cand:
            tj = P.task_trajectory(task_id)
            if tj.exists():
                cand = [tj]
        if not cand:
            cand = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                          reverse=True)
        for p in cand[-3:]:
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except Exception:
                continue
            for ln in reversed(lines):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    obj = json.loads(ln)
                except Exception:
                    continue
                if obj.get("kind") or obj.get("role"):
                    continue
                parts: List[str] = []
                obs = obj.get("observation")
                if isinstance(obs, str) and obs.strip():
                    parts.append(obs.strip())
                elif isinstance(obs, dict):
                    st = obs.get("state_text")
                    if isinstance(st, str) and st.strip():
                        parts.append(st.strip())
                    facts = obs.get("facts")
                    if isinstance(facts, list):
                        parts.extend(str(f) for f in facts if f)
                res = obj.get("result")
                if isinstance(res, dict):
                    so = res.get("stdout")
                    if isinstance(so, str) and so.strip():
                        parts.append(so.strip())
                elif isinstance(res, str) and res.strip():
                    parts.append(res.strip())
                txt = "\n".join(parts).strip()
                if txt:
                    return txt[:2000]
        return ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# LLM 分类器（设计 §5.6.3：复用 config brain 通道）
# ---------------------------------------------------------------------------
# 熔断：brain 不可达时置 True，后续 run 直接走启发式，避免每轮都等超时。
_LLM_CLASSIFIER_BROKEN = False


def _llm_classifier_enabled() -> bool:
    """``runtime.signals.llm_classifier``，默认 true（有 brain 时用 LLM 提精度）。"""
    try:
        import config as _cfg
        sig_cfg = ((_cfg.load_config() or {}).get("runtime") or {}).get("signals") or {}
        return bool(sig_cfg.get("llm_classifier", True))
    except Exception:
        return False


def _llm_call(system: str, user: str) -> Optional[str]:
    """用 config brain 做一次轻量分类；异常/超时一律返回 None（回退启发式）。"""
    global _LLM_CLASSIFIER_BROKEN
    if _LLM_CLASSIFIER_BROKEN:
        return None
    try:
        import config as _cfg
        brain_cfg = (_cfg.load_config() or {}).get("brain") or {}
        if not brain_cfg.get("base_url") or not brain_cfg.get("model"):
            _LLM_CLASSIFIER_BROKEN = True
            return None
        from omni_core.brain.llm import LLMClient
        client = LLMClient(brain_cfg, timeout=float(brain_cfg.get("classify_timeout", 15.0)))
        reply = client.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return (getattr(reply, "content", "") or "").strip()
    except Exception:
        _LLM_CLASSIFIER_BROKEN = True
        return None


def classify_c1_llm(agent_msg: str, next_user_msg: str) -> Optional[str]:
    """C₁ 的 LLM 裁决；返回 None 表示不可用（调用方回退启发式）。"""
    if not (next_user_msg or "").strip():
        return None
    out = _llm_call(
        "你是严格的对话裁决员。仅依据【用户下一条消息】对【上一轮助手输出】的态度分类。"
        "只输出一个词：approved（认可/继续）/ refuted（纠偏/否定）/ "
        "new_task（新任务，与上一轮无关）/ ambiguous（无法判断）。",
        f"【上一轮助手输出】\n{(agent_msg or '')[:1500]}\n\n"
        f"【用户下一条消息】\n{next_user_msg[:1500]}\n\n只输出一个词：",
    )
    if not out:
        return None
    low = out.lower()
    for lab in ("approved", "refuted", "new_task", "ambiguous"):
        if lab in low:
            return lab
    return None


def classify_c2_llm(objective: str, terminal_observation: str) -> Optional[str]:
    """C₂ 的 LLM 评审（仅 objective + 终态观测，绝不给轨迹/推理/结论）。"""
    if not (terminal_observation or "").strip():
        return None
    out = _llm_call(
        "你是严格的任务成败评审员。只依据【目标】与【终态观测】判断目标是否达成；"
        "不得臆测，不得参考任何推理或结论文本。只输出一个词：success / fail / unknown。",
        f"【目标】\n{(objective or '')[:1500]}\n\n"
        f"【终态观测】\n{terminal_observation[:2000]}\n\n只输出一个词：",
    )
    if not out:
        return None
    low = out.lower()
    for lab in ("success", "fail", "unknown"):
        if lab in low:
            return lab
    return None


def collect_run_signal(
    task_id: str,
    run_id: str,
    success: bool,
    assistant_text: str,
    objective: str,
    prev_assistant: str,
    next_user_msg: str,
    terminal_observation: Optional[str] = None,
) -> Optional[SignalRecord]:
    """后端运行时内嵌采集一条 run 信号并累加聚合基线。

    - 交互段（有 prev_assistant）→ C₁ 裁决，mode=interactive
    - 自主段（无 prev_assistant，如首条即长任务）→ C₂ 评审，mode=autonomous
    - 分级判定：先启发式，brain 可用时以 LLM 覆盖（失败静默回退）
    """
    try:
        has_prev = bool((prev_assistant or "").strip())
        c1 = classify_c1(prev_assistant, next_user_msg) if has_prev else "unknown"
        obs = terminal_observation if terminal_observation is not None \
            else read_terminal_observation(task_id, run_id)
        c2 = classify_c2(objective, obs)

        if _llm_classifier_enabled():
            if has_prev:
                lab = classify_c1_llm(prev_assistant, next_user_msg)
                if lab:
                    c1 = lab
            lab2 = classify_c2_llm(objective, obs)
            if lab2:
                c2 = lab2

        mode = "interactive" if has_prev else "autonomous"
        truth_label = c1 if mode == "interactive" else c2
        contrib = consistency_contribution(success, truth_label)
        rec = SignalRecord(
            task_id=task_id, run_id=run_id, a=success, b=(assistant_text or "")[:2000],
            c1=c1, c2=c2, mode=mode,
            labels={"c1": c1, "c2": c2, "a": success},
            consistency=contrib,
            ts=datetime.now(timezone.utc).isoformat(),
        )
        write_run_signal(rec)
        update_aggregate(rec)
        return rec
    except Exception:
        return None


# ---------------------------------------------------------------------------
# C₃ 校准（人工抽检 vs 分级判定不一致率）
# ---------------------------------------------------------------------------
def calibrate_c1c2(sample: List[Dict[str, str]]) -> float:
    """sample: [{predicted, human}]；返回不一致率（0~1）。≤10% 方采信。"""
    if not sample:
        return 1.0
    mism = sum(1 for s in sample if s.get("predicted") != s.get("human"))
    return round(mism / len(sample), 4)


# ---------------------------------------------------------------------------
# 端点查询
# ---------------------------------------------------------------------------
def query_signals(task_id: str) -> List[Dict[str, Any]]:
    """某 task 各 run 的三实体一致率 + 标签（GET /signals?task_id=）。"""
    out: List[Dict[str, Any]] = []
    try:
        d = P.task_dir(task_id)
        if not d.exists():
            return out
        for p in sorted(d.glob("*.signal.json")):
            if not _SIGNAL_FILE_RE.match(p.name):
                continue
            try:
                rec = SignalRecord.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
            out.append({
                "run_id": rec.run_id,
                "a": rec.a, "b": rec.b[:200], "c1": rec.c1, "c2": rec.c2,
                "mode": rec.mode, "consistency": rec.consistency, "ts": rec.ts,
            })
    except Exception:
        pass
    return out


def query_summary() -> Dict[str, Any]:
    """聚合摘要（GET /signals/summary）。"""
    agg = read_aggregate()
    return {
        "fake_success_rate": agg.get("fake_success_rate"),
        "miss_rate": agg.get("miss_rate"),
        "divergence_rate": agg.get("divergence_rate"),
        "uncertain_rate": agg.get("uncertain_rate"),
        "c1_dist": agg.get("c1_dist", {k: 0 for k in C1_LABELS}),
        "c2_dist": agg.get("c2_dist", {k: 0 for k in C2_LABELS}),
        "c1_calibration_error": agg.get("c1_calibration_error"),
        "total": agg.get("total", 0),
        "updated_at": agg.get("updated_at", ""),
        "timeline": agg.get("timeline", []),
        "note": "率按交互/自主分模式；校准误差需 C₃ 人工抽检填写；初值无数据时为 null",
    }
