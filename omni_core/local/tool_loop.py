"""兼容 shim：历史 `from omni_core.local.tool_loop import ...` 全部照常可用。

实现已迁至 omni_core/local/loop/ 包，本文件仅做转发，零逻辑改动。
"""
from omni_core.local.loop import (
    ToolLoop,
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
# 透传原模块级导入名（LLMClient / WorldModel / ExecutionModule / TrajectoryStore / ...），
# 以兼容既有 monkeypatch 写法与模块属性访问。
from omni_core.local.loop.core import *  # noqa: F401,F403

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
