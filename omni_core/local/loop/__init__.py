"""OmniAgent 主控制循环（ToolLoop）的分模块实现。

由 tool_loop.py 拆分而来；对外 API 不变，详见各子模块。
"""
from omni_core.local.loop.core import ToolLoop
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

__all__ = [
    "ToolLoop",
    "VERIFY_TOOL_SCHEMA",
    "ESCALATE_TOOL_SCHEMA",
    "TASK_DONE_TOOL_SCHEMA",
    "RECORD_TOOL_SCHEMA",
    "_WORKER_EXTRA_SCHEMAS",
    "_strip_after_think",
    "_SubtaskGate",
    "_ARTIFACT_MAX",
    "_ARTIFACT_SHOW_MAX",
    "_ARTIFACT_ITEM_CHARS",
    "_WORLD_SUMMARY_CHARS",
    "_subtask_artifacts",
    "_request_fingerprint",
    "_recheck_spec",
    "_failure_point",
    "_format_prev_results",
    "_DEFAULT_ESCALATION",
    "TaskSpec",
    "_safe_str",
    "_compact_for_brain",
    "_TodoStore",
]
