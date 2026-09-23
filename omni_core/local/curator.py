"""M4b.3 Curator：触发式静默维护知识库锐度。

Hermes Autonomous Background Curator 范式；Master Spec §6 决策「仅触发式，不挂定时器」。
任务完成后触发一次（由 ``tool_loop._finish`` 调 ``run_once``），做四件维护：

1. **prune_trajectories**：清理过期轨迹（raw 30 天 / failures 14 天），调 TrajectoryStore.prune。
2. **refine_world_model**：精炼 world-model facts —— 去重已 Merge 的冗余、移除陈旧事实。
3. **review_candidate_skills**：从成功 RunRecord 提取 candidate + 晋升判定（N=3 门）。
4. **flag_low_quality**：高 retry / 高 brain_intervention / 非稳定成功的轨迹 → 标
   ``excluded_from_sft``，供未来数据集构建过滤。

设计红线：
- 零场景硬编码（task_id 来自参数，资产跟 task 走）。
- **非决策者**：不介入任务执行，只在任务后静默维护。
- 所有操作幂等、容错（单文件损坏不中断整体 prune）。
- 返回 CuratorReport 供 telemetry / debug。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.local.trajectory import TrajectoryStore
from omni_core.local.world_model import WorldModel
from omni_core.local.skill_library import SkillLibrary, Skill
from omni_core.local.runtime_paths import (
    task_dir,
    global_memory,
    memory_rollouts,
    memory_rollout_file,
    memory_master,
    memory_summary,
)


def _steady_converged() -> bool:
    """K5：域是否已收敛（稳态降频判据）。异常时保守返回 False（照常蒸馏）。"""
    try:
        from omni_core.local import steady_state as _ss
        return bool(_ss.converged())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 质量判定阈值（Master Spec §6 / §5.2；全可配，不硬编码到业务）
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "prune_raw_days": 30,
    "prune_failure_days": 14,
    # flag_low_quality 阈值
    "high_retry_threshold": 3,        # retry_count >= 此值 → low_quality
    "high_intervention_threshold": 0.5,  # brain_intervention_rate >= 此值 → low_quality
    "min_steps_for_skill": 2,         # 少于此步数的成功轨迹不提取 skill（太短无信息）
}

# 注入视图（memory_summary.md）截断上限（字符）。唯一出处：PUT /memory 与合并再生
# 都复用此常量，杜绝魔法数散落（设计 K1 §4.2 红线：勿复制魔数）。
SUMMARY_TRUNCATE = 20000


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class CuratorReport:
    """单次 Curator 运行的维护报告。"""
    triggered_at: str = ""
    task_id: str = ""
    pruned_files: int = 0
    refined_facts_removed: int = 0
    skills_reviewed: int = 0
    skills_promoted: int = 0
    skills_created: int = 0
    flagged_low_quality: List[str] = field(default_factory=list)
    rollouts_distilled: int = 0
    memory_merged: int = 0
    dedup_hit_rate: float = 0.0   # K5：新蒸馏 facts 中已被 MEMORY.md 覆盖比例
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "triggered_at": self.triggered_at,
            "task_id": self.task_id,
            "pruned_files": self.pruned_files,
            "refined_facts_removed": self.refined_facts_removed,
            "skills_reviewed": self.skills_reviewed,
            "skills_promoted": self.skills_promoted,
            "skills_created": self.skills_created,
            "flagged_low_quality": self.flagged_low_quality,
            "rollouts_distilled": self.rollouts_distilled,
            "memory_merged": self.memory_merged,
            "dedup_hit_rate": self.dedup_hit_rate,
            "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------

class Curator:
    """触发式知识库维护者。非决策者，不介入任务执行。"""

    def __init__(
        self,
        task_id: str,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.task_id = task_id
        # 资产根 = tasks/<task_id>/
        d = task_dir(task_id)
        self.traj_dir = str(d)
        self.skills_dir = str(d / "skills")
        self.world_model_dir = str(d)
        cfg = {**DEFAULTS, **(config or {})}
        self.prune_raw_days: int = int(cfg["prune_raw_days"])
        self.prune_failure_days: int = int(cfg["prune_failure_days"])
        self.high_retry_threshold: int = int(cfg["high_retry_threshold"])
        self.high_intervention_threshold: float = float(cfg["high_intervention_threshold"])
        self.min_steps_for_skill: int = int(cfg["min_steps_for_skill"])
        # K4：纠偏采集开关（红线②：默认关）。仅当显式 true 才把 user_corrections 入库。
        self.corrective_source: bool = bool(cfg.get("corrective_source", False))
        self._last_dedup_hit_rate: float = 0.0  # K5：最近一次蒸馏的去重命中率

    # ==================================================================
    # 公开入口：任务完成后触发一次
    # ==================================================================

    def run_once(
        self,
        run_record: Optional[Dict[str, Any]] = None,
    ) -> CuratorReport:
        """任务完成后触发一次全量维护。

        Args:
            run_record: 刚结束的 RunRecord dict（含 success/steps/retry_count 等）。
                        若提供，则用于 review_candidate_skills + flag_low_quality。

        Returns:
            CuratorReport
        """
        report = CuratorReport(
            triggered_at=datetime.now(timezone.utc).isoformat(),
            task_id=self.task_id,
        )

        # 1. prune 过期轨迹
        try:
            report.pruned_files = self.prune_trajectories()
        except Exception as e:
            report.errors.append(f"prune_trajectories: {type(e).__name__}: {e}")

        # 2. refine world-model
        try:
            report.refined_facts_removed = self.refine_world_model()
        except Exception as e:
            report.errors.append(f"refine_world_model: {type(e).__name__}: {e}")

        # 3. review candidate skills（从刚结束的 run_record 提取）
        if run_record is not None:
            try:
                skill_result = self.review_candidate_skills(run_record)
                report.skills_reviewed = skill_result["reviewed"]
                report.skills_promoted = skill_result["promoted"]
                report.skills_created = skill_result["created"]
            except Exception as e:
                report.errors.append(f"review_candidate_skills: {type(e).__name__}: {e}")

        # 4. flag low-quality（从刚结束的 run_record 判定）
        if run_record is not None:
            try:
                flagged = self.flag_low_quality(run_record)
                report.flagged_low_quality = flagged
            except Exception as e:
                report.errors.append(f"flag_low_quality: {type(e).__name__}: {e}")

        # 5. 蒸馏任务记忆（第五件核心维护）+ 惰性合并校验
        #    K5 稳态降频：已收敛则跳过蒸馏（释放人力转下一域），仅记日志；
        #    未收敛则正常蒸馏，合并阈值在收敛后提升至 10（更稀疏合并）。
        if run_record is not None:
            try:
                if _steady_converged():
                    self._log_steady("已收敛：跳过蒸馏（低频维护）")
                    report.rollouts_distilled = 0
                else:
                    rollout = self.distill_task_memory(run_record)
                    if rollout is not None:
                        report.rollouts_distilled = 1
                        _merge_threshold = 10 if _steady_converged() else 5
                        report.memory_merged = self.maybe_merge_rollouts(threshold=_merge_threshold)
                report.dedup_hit_rate = getattr(self, "_last_dedup_hit_rate", 0.0)
                self._persist_curator_metric(
                    report.dedup_hit_rate, report.skills_promoted, report.skills_created)
            except Exception as e:
                report.errors.append(f"distill_task_memory: {type(e).__name__}: {e}")

        return report

    # ==================================================================
    # 1. prune_trajectories
    # ==================================================================

    def prune_trajectories(self) -> int:
        """清理过期轨迹文件（raw 30 天 / failures 14 天）。

        直接按 mtime 扫描删除，**不构造 TrajectoryStore**（避免 open 新空文件干扰）。
        """
        store_dir = task_dir(self.task_id)
        if not store_dir.exists():
            return 0
        return self._manual_prune(store_dir)

    def _manual_prune(self, store_dir: Path) -> int:
        """TrajectoryStore 构造失败时的兜底手动 prune。"""
        cutoff = time.time() - self.prune_raw_days * 86400
        fail_cutoff = time.time() - self.prune_failure_days * 86400
        removed = 0
        for p in store_dir.glob("*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except Exception:
                pass
        for p in store_dir.glob("*.run.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except Exception:
                pass
        fdir = store_dir / "failures"
        if fdir.exists():
            for p in fdir.glob("*.json"):
                try:
                    if p.stat().st_mtime < fail_cutoff:
                        p.unlink()
                        removed += 1
                except Exception:
                    pass
        return removed

    # ==================================================================
    # 2. refine_world_model
    # ==================================================================

    def refine_world_model(self) -> int:
        """精炼 world-model facts —— 去重 + 移除空/陈旧事实。

        只对磁盘 current.md 操作（不依赖���存 WorldModel 实例）。
        返回移除的冗余事实条数。
        """
        wm_path = task_dir(self.task_id) / "world_model.md"
        if not wm_path.exists():
            return 0

        content = wm_path.read_text(encoding="utf-8")
        # 复用 WorldModel.load 解析（但不覆盖内存实例）
        wm = WorldModel(task_id=self.task_id)
        wm.load()

        original_count = len(wm.facts)
        if original_count == 0:
            return 0

        # 去重（保留顺序）
        seen: set = set()
        refined: List[str] = []
        for f in wm.facts:
            key = f.strip().lower()
            if key and key not in seen:
                seen.add(key)
                refined.append(f.strip())
        # 移除空事实
        refined = [f for f in refined if f and f != "_(暂无)_"]

        removed = original_count - len(refined)
        if removed > 0:
            wm.facts = refined
            wm.save()
        return removed

    # ==================================================================
    # 3. review_candidate_skills
    # ==================================================================

    def review_candidate_skills(self, run_record: Dict[str, Any]) -> Dict[str, Any]:
        """从成功 RunRecord 提取 candidate skill + 晋升判定。

        Returns:
            {reviewed, promoted, created}
        """
        result = {"reviewed": 0, "promoted": 0, "created": 0}

        if not run_record.get("success"):
            return result

        steps = int(run_record.get("steps", 0))
        if steps < self.min_steps_for_skill:
            return result

        # 从 run_record 提取 candidate
        lib = SkillLibrary(task_id=self.task_id)
        candidate = SkillLibrary.from_run_record(run_record, task_id=self.task_id)
        if candidate is None:
            return result

        result["reviewed"] = 1
        promote_result = lib.promote_or_insert(candidate)
        if promote_result.get("action") == "created":
            result["created"] = 1
        if promote_result.get("promoted"):
            result["promoted"] = 1

        return result

    # ==================================================================
    # 4. flag_low_quality
    # ==================================================================

    def flag_low_quality(self, run_record: Dict[str, Any]) -> List[str]:
        """标记低质量执行记录（高 retry / 高 intervention / 非稳定成功）。

        判定规则（任一命中即标 excluded_from_sft）：
        - retry_count >= high_retry_threshold
        - brain_intervention_rate >= high_intervention_threshold
          （= brain_calls / max(decision_steps, 1)）
        - success == False（失败轨迹默认 low_quality）

        Returns:
            被标记的 run_id 列表（写 ``excluded_from_sft`` 标记文件）。
        """
        flagged: List[str] = []
        run_id = run_record.get("run_id", "")
        if not run_id:
            return flagged

        retry_count = int(run_record.get("retry_count", 0))
        brain_calls = int(run_record.get("brain_calls", 0))
        decision_steps = int(run_record.get("decision_steps", 0))
        success = bool(run_record.get("success", False))

        is_low = False
        reasons: List[str] = []

        if retry_count >= self.high_retry_threshold:
            is_low = True
            reasons.append(f"high_retry={retry_count}")

        intervention_rate = brain_calls / max(decision_steps, 1) if decision_steps > 0 else 0.0
        if intervention_rate >= self.high_intervention_threshold and decision_steps > 0:
            is_low = True
            reasons.append(f"high_intervention={intervention_rate:.2f}")

        if not success:
            is_low = True
            reasons.append("task_failed")

        if not is_low:
            return flagged

        # 写标记文件：tasks/<task_id>/excluded/<run_id>.json
        excl_dir = task_dir(self.task_id) / "excluded"
        excl_dir.mkdir(parents=True, exist_ok=True)
        marker = {
            "run_id": run_id,
            "reasons": reasons,
            "retry_count": retry_count,
            "brain_calls": brain_calls,
            "decision_steps": decision_steps,
            "intervention_rate": round(intervention_rate, 4),
            "success": success,
            "flagged_at": datetime.now(timezone.utc).isoformat(),
        }
        marker_path = excl_dir / f"{run_id}.json"
        try:
            marker_path.write_text(
                json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            flagged.append(run_id)
        except Exception:
            pass

        return flagged

    # ==================================================================
    # 5. distill_task_memory（第五件核心维护）
    # ==================================================================

    def distill_task_memory(self, run_record: Dict[str, Any]) -> Optional[Path]:
        """第五件维护·蒸馏：单任务轨迹 → ``memory/rollouts/<task_id>.md``。

        规则（§3.1 / §3.3）：
        - 步数低于最小技能萃取阈值 → 直接返回 None（短路，太短无信息）。
        - 成功任务：读取磁盘 world-model facts 生成 ``## facts``（过滤空与「_(暂无)_」），
          并计算新 facts 相对既有 MEMORY.md 的去重命中率（K5 信号）。
        - 失败 / 高重试任务：生成带归因的 ``## lessons``。
        - K4（默认关）：``user_corrections`` 经 C₁ 判定为证伪且含纠正的用户消息 →
          ``## user_corrections`` 段落（回指对话来源，append-only 去重）。
        - 无有效内容 → 返回 None；否则按规范模板落盘并返回路径。
        """
        steps = int(run_record.get("steps", 0) or 0)
        # K4 校正：纠偏是**人工提供**的高质量原料，价值与轨迹长度无关；
        # 原实现在此处无差别短路，导致 1 步任务里的纠偏被整条丢弃（真机验证发现）。
        # 现改为：仅有纠偏待入库时不短路；无纠偏仍保持原短路语义（避免短任务噪声）。
        _has_corrections = bool(
            (run_record.get("user_corrections") or []) and self.corrective_source)
        if steps < self.min_steps_for_skill and not _has_corrections:
            return None

        success = bool(run_record.get("success", False))
        objective = (run_record.get("objective") or "").strip()
        retry_count = int(run_record.get("retry_count", 0) or 0)
        reason = (run_record.get("reason") or "").strip()
        task_id = self.task_id

        facts: List[str] = []
        lessons: List[str] = []
        corrections: List[str] = []

        if success:
            try:
                wm = WorldModel(task_id=task_id)
                wm.load()
                facts = [f for f in (wm.facts or []) if f and f != "_(暂无)_"]
            except Exception:
                facts = []
        else:
            if reason:
                lessons.append(f"[失败] {objective} —— {reason}")

        # 高重试（无论成败）均记录经验，避免重复踩坑
        if retry_count >= self.high_retry_threshold and retry_count > 0:
            lessons.append(f"[高重试({retry_count})] {objective} —— {reason or '未记录原因'}")

        # K4：第三来源——session 用户纠偏（Curator 级红线②：corrective_source 关时不入库）
        _corr = list(run_record.get("user_corrections") or [])
        if _corr and self.corrective_source:
            corrections = [f"- {c}" for c in _corr if c and c.strip()]

        # K5：去重命中率 = 新 facts 中已被既有 MEMORY.md 覆盖比例（蒸馏前快照比对）
        self._last_dedup_hit_rate = self._compute_dedup_hit_rate(facts)

        if not facts and not lessons and not corrections:
            return None

        path = memory_rollout_file(task_id)
        now = datetime.now(timezone.utc).isoformat()
        traj = f"tasks/{task_id}/trajectory.jsonl"
        lines = [
            f"# rollout: {task_id}",
            f"- task_id: {task_id} / objective: {objective} / success: {success} "
            f"/ steps: {steps} / distilled_at(UTC iso): {now} / trajectory: {traj}",
            "",
            "## facts",
        ]
        if facts:
            lines.extend(f"- {f}" for f in facts)
        else:
            lines.append("- (暂无)")
        lines.append("")
        lines.append("## lessons")
        if lessons:
            lines.extend(f"- {l}" for l in lessons)
        else:
            lines.append("- (暂无)")
        lines.append("")
        lines.append("## user_corrections")
        if corrections:
            lines.extend(corrections)
        else:
            lines.append("- (暂无)")
        lines.append("")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines), encoding="utf-8")
            return path
        except Exception:
            return None

    # ------------------------------------------------------------------
    # K5 辅助：去重命中率 + 指标持久化 + 稳态降频判定
    # ------------------------------------------------------------------
    def _log_steady(self, msg: str) -> None:
        """K5 稳态事件日志（stdout，后端可观测）。"""
        try:
            print(f"[Curator][SteadyState] {msg}", flush=True)
        except Exception:
            pass

    def _compute_dedup_hit_rate(self, facts: List[str]) -> float:
        """新蒸馏 facts 中已被既有 MEMORY.md 覆盖的比例（K5 蒸馏去重命中率）。"""
        if not facts:
            return 0.0
        try:
            existing = memory_master().read_text(encoding="utf-8").lower() if memory_master().exists() else ""
        except Exception:
            existing = ""
        if not existing:
            return 0.0
        hit = sum(1 for f in facts if f.strip().lower() in existing)
        return round(hit / len(facts), 4)

    def _persist_curator_metric(self, dedup_hit_rate: float,
                                promoted: int, created: int) -> None:
        """滚动写 ``memory/curator_metrics.json``（最近 50 条），供 K5 稳态读取。"""
        try:
            p = global_memory() / "curator_metrics.json"
            data = {"metrics": []}
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8")) or data
                except Exception:
                    data = {"metrics": []}
            data.setdefault("metrics", [])
            data["metrics"].append({
                "ts": datetime.now(timezone.utc).isoformat(),
                "task_id": self.task_id,
                "dedup_hit_rate": dedup_hit_rate,
                "skills_promoted": promoted,
                "skills_created": created,
            })
            data["metrics"] = data["metrics"][-50:]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def maybe_merge_rollouts(self, threshold: int = 5) -> int:
        """第五件维护·合并：未合并 rollout 批量合并进 MEMORY.md + 再生 summary。

        双触发规则（§2.2）：本期落地「未合并数量 ≥ threshold」这一条（默认 5，可配置）。
        已合并 task_id 记入 ``memory/merged.json`` 保证幂等去重。

        Returns:
            本次追加进 MEMORY.md 的条目数（facts + lessons）。
        """
        rollouts_dir = memory_rollouts()
        if not rollouts_dir.exists():
            return 0

        merged_file = global_memory() / "merged.json"
        merged_ids: set = set()
        if merged_file.exists():
            try:
                merged_ids = set(
                    (json.loads(merged_file.read_text(encoding="utf-8")) or {}).get("merged_task_ids", [])
                )
            except Exception:
                merged_ids = set()

        pending = [p for p in sorted(rollouts_dir.glob("*.md")) if p.stem not in merged_ids]
        if len(pending) < threshold:
            return 0

        new_facts: List[str] = []
        new_lessons: List[str] = []
        for p in pending:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            f_sec, l_sec = _parse_rollout_sections(text)
            new_facts.extend(f_sec)
            new_lessons.extend(l_sec)

        # 更新合并清单（幂等：已合并 ∪ 本次待合并）
        merged_ids |= {p.stem for p in pending}
        _write_merged_ids(merged_file, merged_ids)

        if not new_facts and not new_lessons:
            return 0

        master = memory_master()
        existing = master.read_text(encoding="utf-8") if master.exists() else ""
        new_text, added_f, added_l = _merge_memory_sections(existing, new_facts, new_lessons)
        try:
            master.write_text(new_text, encoding="utf-8")
        except Exception:
            return 0
        # 再生注入视图（截断 SUMMARY_TRUNCATE 字符，保证注入上下文体量可控）
        try:
            memory_summary().write_text(new_text[:SUMMARY_TRUNCATE], encoding="utf-8")
        except Exception:
            pass
        return added_f + added_l

    # ==================================================================
    # 辅助：批量扫描历史 run（可选，供离线分析用）
    # ==================================================================

    def scan_all_runs(self) -> List[Dict[str, Any]]:
        """扫描某 task 下所有 *.run.json，返回 RunRecord dict 列表（供批量分析）。"""
        store_dir = task_dir(self.task_id)
        if not store_dir.exists():
            return []
        runs: List[Dict[str, Any]] = []
        for p in sorted(store_dir.glob("*.run.json")):
            try:
                runs.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        return runs

    def flag_all_low_quality(self) -> List[str]:
        """批量扫描所有历史 run，标记 low_quality（离线维护用，非每次触发必跑）。"""
        all_flagged: List[str] = []
        for rec in self.scan_all_runs():
            all_flagged.extend(self.flag_low_quality(rec))
        return all_flagged


# ---------------------------------------------------------------------------
# 蒸馏 / 合并辅助（模块级，便于单测与无 Curator 实例复用）
# ---------------------------------------------------------------------------

def _parse_rollout_sections(text: str):
    """从 rollout md 解析 ``## facts`` / ``## lessons`` 段落的有效条目。"""
    facts: List[str] = []
    lessons: List[str] = []
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("## facts"):
            cur = "facts"
            continue
        if s.startswith("## lessons"):
            cur = "lessons"
            continue
        if s.startswith("# ") and not s.startswith("## "):
            cur = None
            continue
        if cur == "facts" and s.startswith("- "):
            v = s[2:].strip()
            if v and v != "(暂无)":
                facts.append(v)
        elif cur == "lessons" and s.startswith("- "):
            v = s[2:].strip()
            if v and v != "(暂无)":
                lessons.append(v)
    return facts, lessons


def _merge_memory_sections(existing: str, new_facts: List[str], new_lessons: List[str]):
    """把新 facts/lessons 去重追加进 MEMORY.md 双分区。

    返回 ``(新文本, 追加facts数, 追加lessons数)``。

    去重规则（§3.1）：条目全文 strip + 小写比对，杜绝重复沉淀；
    仅追加不覆写，保护人工编辑内容；首次自动创建固定头部。
    """
    fact_sec, lesson_sec = "## 长期事实", "## 教训"
    sections = {fact_sec: [], lesson_sec: []}
    cur = None
    for line in existing.splitlines():
        s = line.strip()
        if s.startswith("# ") and not s.startswith("## "):
            cur = None
            continue
        if s.startswith("## "):
            cur = s
            if cur not in sections:
                sections[cur] = []
            continue
        if cur in sections and s.startswith("- "):
            sections[cur].append(s[2:].strip())

    def _add(items: List[str], new: List[str]) -> int:
        seen = {x.strip().lower() for x in items if x}
        added = 0
        for n in new:
            key = n.strip().lower()
            if key and key not in seen:
                seen.add(key)
                items.append(n.strip())
                added += 1
        return added

    added_f = _add(sections[fact_sec], new_facts)
    added_l = _add(sections[lesson_sec], new_lessons)

    out = ["# OmniAgent 全局长期记忆", ""]
    for marker in (fact_sec, lesson_sec):
        out.append(marker)
        if sections[marker]:
            out.extend(f"- {it}" for it in sections[marker])
        else:
            out.append("- (暂无)")
        out.append("")
    return "\n".join(out).rstrip() + "\n", added_f, added_l


def _write_merged_ids(path: Path, ids: set) -> None:
    """幂等写 ``merged.json``（已合并 task_id 清单）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"merged_task_ids": sorted(ids)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def regenerate_summary() -> int:
    """K1 · 服务端重生成注入视图 ``memory_summary.md``（截断管控）。

    用户经 ``PUT /memory`` 改写 MEMORY.md 后调用，使下一轮注入视图与人工
    编辑内容同步；返回 summary 字符数。复用唯一常量 ``SUMMARY_TRUNCATE``，
    不复制魔法数。MEMORY.md 不存在时清空 summary 并返回 0。
    """
    master = memory_master()
    summary = memory_summary()
    if not master.exists():
        try:
            summary.write_text("", encoding="utf-8")
        except Exception:
            pass
        return 0
    try:
        text = master.read_text(encoding="utf-8")
    except Exception:
        return 0
    try:
        summary.write_text(text[:SUMMARY_TRUNCATE], encoding="utf-8")
    except Exception:
        return 0
    return min(len(text), SUMMARY_TRUNCATE)
