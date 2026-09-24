"""agent 外层 tool 插件层（L1）。

设计定位（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §3.5 / §5.0）：
- 工具=插件，完全平级，由 LLM 直接调用；内核零持有、零派发。
- 这里提供隔离「定义 / 注册 / 派发 / 回填」四跳的轻量基础设施。

工具来源（`ToolPlugin.source`）：
- ``core``   内核内置（shell 执行 / skill / local_model / 环境适配）
- ``env``    环境自带（host / emulator 的工具面，见 ``environments/``）
- ``plugin`` ``plugins/`` 下的自包含插件（自持 ``enabled`` 开关）
- ``mcp``    外部 MCP server（与自研平级）
"""
from omni_core.tools.base import (
    ToolPlugin,
    PluginRegistry,
    function_tool,
    register_tool,
    register_external,
    unregister_tool,
    call_tool,
    sdk_tools,
    schemas,
    build_plugin_registry,
)
from omni_core.tools.shell_tool import shell_exec, configure as configure_shell
from omni_core.tools.env_loader import activate_environment, active_kind
from omni_core.tools.mcp_servers import build_mcp_servers
from omni_core.tools.skill_tool import (
    load_skill,
    set_skill_task_context,
    skill_load_limit,
)
from omni_core.tools.local_model_tool import (
    configure as configure_local_model,
    configure_from_app_config as configure_local_model_from_config,
    render_description as render_local_model_description,
    is_registered as local_model_registered,
    TOOL_NAME as LOCAL_INFER_TOOL,
    UNIT as LOCAL_MODEL_UNIT,
)
from omni_core.tools.loader import (
    PluginContext,
    LoadReport,
    load_plugins,
    last_report,
    shutdown_plugins,
)


__all__ = [
    "ToolPlugin",
    "PluginRegistry",
    "function_tool",
    "register_tool",
    "register_external",
    "unregister_tool",
    "call_tool",
    "sdk_tools",
    "schemas",
    "build_plugin_registry",
    "shell_exec",
    "configure_shell",
    "activate_environment",
    "active_kind",
    "build_mcp_servers",
    "load_skill",
    "set_skill_task_context",
    "skill_load_limit",
    "configure_local_model",
    "configure_local_model_from_config",
    "render_local_model_description",
    "local_model_registered",
    "LOCAL_INFER_TOOL",
    "LOCAL_MODEL_UNIT",
    "PluginContext",
    "LoadReport",
    "load_plugins",
    "last_report",
    "shutdown_plugins",
]
