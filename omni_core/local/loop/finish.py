"""FinishMixin：ToolLoop 的职责切片（逐字搬移，零逻辑改动）。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
import warnings
import re
from datetime import datetime, timezone
import config
from pathlib import Path
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from omni_core.brain.llm import LLMClient, model_health_ok
from omni_core.brain.prompt import build_system_prompt
from omni_core.brain import tools as brain_tools
from omni_core.local.world_model import WorldModel
from omni_core.local.states import AgentState
from omni_core.local.trajectory import TrajectoryStore
from omni_core.local import telemetry
from omni_core.local.curator import Curator
from omni_core.local.runtime_paths import (
    task_trajectory, task_collected, auto_project_id,
)
from omni_core.local.task_store import TaskStore, ProjectStore

from omni_core.local.loop.parts import (
    VERIFY_TOOL_SCHEMA,
    ESCALATE_TOOL_SCHEMA,
    TASK_DONE_TOOL_SCHEMA,
    RECORD_TOOL_SCHEMA,
    _WORKER_EXTRA_SCHEMAS,
    _strip_after_think,
    _SubtaskGate,
    _ARTIFACT_MAX,
    _ARTIFACT_SHOW_MAX,
    _ARTIFACT_ITEM_CHARS,
    _WORLD_SUMMARY_CHARS,
    _subtask_artifacts,
    _request_fingerprint,
    _recheck_spec,
    _failure_point,
    _format_prev_results,
    _DEFAULT_ESCALATION,
    TaskSpec,
    _safe_str,
    _compact_for_brain,
    _TodoStore,
)


class FinishMixin:
    def _finish(
        self,
        success: bool,
        reason: str,
        steps: int,
        store: Optional[TrajectoryStore] = None,
        spec: Optional[TaskSpec] = None,
    ) -> Dict[str, Any]:
        for client in (self.brain, getattr(self, "executor", None)):
            try:
                client.close()
            except Exception:
                pass

        # 运行级失败兜底（单大脑失败无 escalate 计数）
        failures = self._failures
        if not success and failures == 0:
            failures = 1
        recoveries = self._recoveries

        backend = (self._cfg.get("runtime") or {}).get("backend", "?")

        run_meta = {
            "objective": spec.objective if spec else "",
            "done_when": spec.done_when if spec else "",
            "expected": spec.expected if spec else None,
            "backend": backend,
            "brain_model": self.brain_model,
            "exec_model": self.exec_model,
            "success": success,
            "steps": steps,
            "brain_calls": self._brain_calls,
            "decision_steps": self._decision_steps,
            "action_count": self._action_count,
            "retry_count": self._retry_count,
            "failures": failures,
            "recoveries": recoveries,
        }

        report_file = None
        if store is not None:
            run_rec = store.finish_run(
                objective=run_meta["objective"], done_when=run_meta["done_when"], expected=run_meta["expected"],
                backend=backend, brain_model=self.brain_model, exec_model=self.exec_model,
                success=success, steps=steps, brain_calls=self._brain_calls,
                decision_steps=self._decision_steps, action_count=self._action_count,
                retry_count=self._retry_count, failures=failures, recoveries=recoveries, reason=reason,
            )
            report_dir = str(task_trajectory(spec.task_id).parent / "reports")
            report_file = telemetry.emit_run_report(run_rec.to_dict(), report_dir)
            run_meta["run_id"] = run_rec.run_id
            run_meta["trajectory_file"] = run_rec.trajectory_file

        result: Dict[str, Any] = {"success": success, "reason": reason, "steps": steps}
        if store is not None:
            result["run_id"] = run_meta.get("run_id")
            result["trajectory_file"] = run_meta.get("trajectory_file")
            result["report_file"] = report_file
            result["metrics"] = telemetry.compute(run_meta)

        # M4b.3 Curator：任务完成后触发一次（触发式，不挂定时器）
        if self.curator_enabled and store is not None and spec is not None:
            try:
                curator = Curator(
                    task_id=spec.task_id,
                    config=self._cfg.get("runtime", {}).get("curator"),
                )
                run_record_for_curator = dict(run_meta)
                run_record_for_curator["reason"] = reason
                run_record_for_curator["run_id"] = run_meta.get("run_id", "")
                # K4：用户纠偏消息透传给蒸馏（第三来源）；默认空，由 config 控制是否启用
                run_record_for_curator["user_corrections"] = list(spec.corrections or [])
                curator_report = curator.run_once(run_record_for_curator)
                result["curator_report"] = curator_report.to_dict()
                if self.verbose and curator_report.flagged_low_quality:
                    self._log(f"[Curator] 标记 low_quality: {curator_report.flagged_low_quality}")
                if self.verbose and curator_report.errors:
                    self._log(f"[Curator] 维护完成（含 {len(curator_report.errors)} 个错误）")
            except Exception as e:
                if self.verbose:
                    self._log(f"[Curator] 触发失败（不影响主流程）: {type(e).__name__}: {e}")

        return result

