"""X3 运行控制台 · 后端运行时路由聚合壳（由 router_runtime.py 拆分而来）。

对外只暴露 `router`（等价于原 APIRouter(prefix="/api/runtime")）及原模块全部模块级名；
子路由实现下沉到 backend/api/routers/ 各域模块，server.py / tests 零感知。
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.api.routers import (
    helpers, chat_runtime, tools_api, tasks_api, profile_api,
    snapshot_api, approvals_api, diagnostics_api, models_api,
)

router = APIRouter(prefix="/api/runtime", tags=["runtime"])
router.include_router(chat_runtime.router)
router.include_router(models_api.router)
router.include_router(tools_api.router)
router.include_router(profile_api.router)
router.include_router(tasks_api.router)
router.include_router(snapshot_api.router)
router.include_router(approvals_api.router)
router.include_router(diagnostics_api.router)

# 兼容 re-export：保留原模块暴露的全部模块级名（外部零改动）
from backend.api.routers.helpers import (  # noqa: F401
    ROOT, CONFIG_PATH, manager, AGENT_MAIN, _traj_cursor, _cursor_lock,
    _EXEC_TOOLS, _append_task_name, _call_skill,
    _config, _config_hash, _ensure_outbox, _finish_task,
    _is_any_running, _is_task_running, _make_brain_cfg,
    _merge_message_step, _merge_thinking_step, _paths, _project_store,
    _read_latest_collected, _read_skills,
    _read_trajectory, _read_world, _running_task_id,
    _session_records_to_model_messages, _snapshot, _steps_to_text, _task_store,
    _trajectory_dir, _trim, _try_start_task, _upsert, clear_live_snapshot, config,
    live_snapshot, push_chat,
    push_message_stream, push_thinking, push_thinking_stream, push_tool_call,
    TOOL_REGISTRY,
)
from backend.api.routers.chat_runtime import (  # noqa: F401
    _dispatch_chat, _sse, _stream_gen, api_chat, api_inject, api_stop,
    api_wake, live, stream,
)
from backend.api.routers.tools_api import (  # noqa: F401
    list_tools, set_environment, set_plugin_enabled,
)
from backend.api.routers.tasks_api import (  # noqa: F401
    api_skill_delete, api_skill_run, create_task, delete_task, get_task,
    get_task_history, list_project_sessions, list_projects, list_projects_meta,
    list_task_skills, list_tasks, read_project_session, update_task_state,
)
from backend.api.routers.snapshot_api import (  # noqa: F401
    get_skills, snapshot,
)
from backend.api.routers.diagnostics_api import (  # noqa: F401
    memory_diagnostics,
)
from backend.api.routers.models_api import (  # noqa: F401
    list_models, put_models, set_model_default,
)
