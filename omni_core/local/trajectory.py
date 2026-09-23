"""M4a.1 Trajectory 富 schema + 落盘。

每步动作写成结构化记录并持久化，供 Evaluation（M4a.2）/ skill 录制（M4b.2）/
未来 SFT 数据集过滤 / Debug。

设计红线：
- 零场景硬编码：资产跟 task_id 走，本模块不含任何场景 / 业务字眼。
- 不重复存全量世界模型（WorldModel 仍只存「当前屏摘要」供组消息）。
- 落盘路径 ``tasks/<task_id>/trajectory.jsonl`` + run 级 ``RunRecord``。
"""
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.local.runtime_paths import task_trajectory


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class StepRecord:
    """单步结构化记录（逐行写入 jsonl）。"""

    def __init__(
        self,
        run_id: str,
        step: int,
        state: str,
        observation: Dict[str, Any],
        action: Optional[Dict[str, Any]],
        result: Any,
        metrics: Dict[str, Any],
        verified: bool,
    ):
        self.run_id = run_id
        self.step = step
        self.state = state
        self.observation = observation
        self.action = action
        self.result = result
        self.metrics = metrics
        self.verified = verified
        self.ts = _now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step": self.step,
            "ts": self.ts,
            "state": self.state,
            "observation": self.observation,
            "action": self.action,
            "result": self.result,
            "metrics": self.metrics,
            "verified": self.verified,
        }


class RunRecord:
    """一次任务运行的总记录（run 结束写盘）。"""

    def __init__(
        self,
        run_id: str,
        objective: str,
        done_when: str,
        expected: Optional[str],
        backend: str,
        brain_model: str,
        exec_model: str,
        start_ts: str,
        end_ts: str,
        success: bool,
        steps: int,
        brain_calls: int,
        decision_steps: int,
        action_count: int,
        retry_count: int,
        failures: int,
        recoveries: int,
        trajectory_file: str,
        report_file: Optional[str] = None,
        reason: str = "",
    ):
        self.run_id = run_id
        self.objective = objective
        self.done_when = done_when
        self.expected = expected
        self.backend = backend
        self.brain_model = brain_model
        self.exec_model = exec_model
        self.start_ts = start_ts
        self.end_ts = end_ts
        self.success = success
        self.steps = steps
        self.brain_calls = brain_calls
        self.decision_steps = decision_steps
        self.action_count = action_count
        self.retry_count = retry_count
        self.failures = failures
        self.recoveries = recoveries
        self.trajectory_file = trajectory_file
        self.report_file = report_file
        # F1.3：终止/失败原因（成功运行保持空，不写盘）
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        d = self.__dict__.copy()
        # F1.3：成功运行不写入 reason 字段（仅失败/终止 run 携带原因，便于排查）
        if not self.reason:
            d.pop("reason", None)
        return d


class TrajectoryStore:
    """单 run 的轨迹写入器；run 结束写 ``RunRecord`` + 失败单写 ``failures/``。"""

    def __init__(self, task_id: str, run_id: Optional[str] = None):
        self.run_id = run_id or _new_run_id()
        self.task_id = task_id
        self.dir = task_trajectory(task_id).parent
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{_today()}_{self.run_id}.jsonl"
        self._fh = open(self.path, "a", encoding="utf-8")
        self.step = 0
        self.start_ts = _now_iso()

    # --- 写入 ---------------------------------------------------------------
    def log_step(
        self,
        state: str,
        observation: Dict[str, Any],
        action: Optional[Dict[str, Any]],
        result: Any,
        metrics: Optional[Dict[str, Any]] = None,
        verified: bool = False,
    ) -> StepRecord:
        self.step += 1
        rec = StepRecord(
            run_id=self.run_id,
            step=self.step,
            state=state,
            observation=observation,
            action=action,
            result=result,
            metrics=metrics,
            verified=verified,
        )
        self._fh.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
        self._fh.flush()
        return rec

    def log_think(self, content: str, role: str = "", model: str = "") -> None:
        """写一条「思考（推理链）」轨迹，供历史复盘。

        与 action/observation 标准 step 解耦：思考是决策过程，不计入 step 计数，
        单独以 kind="think" 落盘，便于复盘工具按类型筛选。
        """
        if not content:
            return
        self._fh.write(json.dumps({
            "run_id": self.run_id,
            "step": self.step,
            "ts": _now_iso(),
            "kind": "think",
            "role": role,
            "model": model,
            "content": content,
        }, ensure_ascii=False) + "\n")
        self._fh.flush()

    def finish_run(
        self,
        objective: str,
        done_when: str,
        expected: Optional[str],
        backend: str,
        brain_model: str,
        exec_model: str,
        success: bool,
        steps: int,
        brain_calls: int,
        decision_steps: int,
        action_count: int,
        retry_count: int,
        failures: int,
        recoveries: int,
        report_file: Optional[str] = None,
        reason: str = "",
    ) -> RunRecord:
        self._fh.close()
        end_ts = _now_iso()
        rec = RunRecord(
            run_id=self.run_id,
            objective=objective,
            done_when=done_when,
            expected=expected,
            backend=backend,
            brain_model=brain_model,
            exec_model=exec_model,
            start_ts=self.start_ts,
            end_ts=end_ts,
            success=success,
            steps=steps,
            brain_calls=brain_calls,
            decision_steps=decision_steps,
            action_count=action_count,
            retry_count=retry_count,
            failures=failures,
            recoveries=recoveries,
            trajectory_file=str(self.path),
            report_file=report_file,
            reason=reason,
        )
        # 主记录
        with open(self.dir / f"{self.run_id}.run.json", "w", encoding="utf-8") as f:
            json.dump(rec.to_dict(), f, ensure_ascii=False, indent=2)
        # 失败单写 failures/
        if not success:
            fdir = self.dir / "failures"
            fdir.mkdir(exist_ok=True)
            with open(fdir / f"{self.run_id}.json", "w", encoding="utf-8") as f:
                json.dump(rec.to_dict(), f, ensure_ascii=False, indent=2)
        return rec

    # --- 留存 prune（Master Spec §5 / §12.6：raw 轨迹滚动 30 天） ----------
    def prune(self, max_age_days: int = 30) -> int:
        """删除超过 max_age_days 天的 jsonl / run 记录（failures 14 天）。"""
        import shutil

        cutoff = time.time() - max_age_days * 86400
        fail_cutoff = time.time() - 14 * 86400
        removed = 0
        for p in self.dir.glob("*.jsonl"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        for p in self.dir.glob("*.run.json"):
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        fdir = self.dir / "failures"
        if fdir.exists():
            for p in fdir.glob("*.json"):
                if p.stat().st_mtime < fail_cutoff:
                    p.unlink()
                    removed += 1
        return removed

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
