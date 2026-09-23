"""agent 外层 tool 插件层（L1）。

设计定位（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §3.5 / §5.0）：
- 工具=插件，完全平级，由 LLM 直接调用；内核零持有、零派发。
- 这里提供隔离「定义 / 注册 / 派发 / 回填」四跳的轻量基础设施，
  作为接入点；M1 接入 OpenAI Agents SDK 时，`function_tool` 直接替换本装饰器，
  插件函数体（业务实现）无需改动。

插件分组（config.runtime.tools.groups 驱动启停，不写死）：
- ``vision``  视觉/SoM（M0）
- ``device``  设备能力：Host/Emulator 键鼠/感知/设备原语 + 通用件（M3）
- ``python``  Python 执行（§5.5）
- ``mcp``     外部 MCP server 接入的工具（M3，与自研平级）
- ``local_model`` 本地模型作为工具（M9，边界由 llm.local_as_tool.boundary 配置）
"""
from omni_core.tools.base import (
    ToolPlugin,
    PluginRegistry,
    function_tool,
    tool_registry,
    register_tool,
    register_external,
    unregister_tool,
    call_tool,
    schemas,
    build_plugin_registry,
)
from omni_core.tools.vision_tool import (
    bind_vision_runtime,
    vision_describe,
    som_ground,
    som_marks,
    som_last_result,
    tap_by_mark,
)
from omni_core.tools.device_tool import (
    bind_execution_module,
    press,
    hotkey,
    input_text,
    wait,
    click,
    drag,
    observe,
    read_screen_text,
    ocr_screenshot,
    screenshot,
    get_ui_tree,
    tap_by_id,
    tap_text,
    launch_app,
    press_keycode,
    collect_list,
    template_match,
    wait_for,
)
from omni_core.tools.python_tool import run_python, configure as configure_python
from omni_core.tools.shell_tool import shell_exec, configure as configure_shell
from omni_core.tools.filesystem_tool import (
    read_file,
    write_file,
    edit_file,
    list_dir,
    search_content,
    mkdir,
    generate_report,
    configure as configure_filesystem,
)
from omni_core.tools.web_tool import (
    web_fetch,
    web_search,
    configure as configure_web,
)
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
    GROUP as LOCAL_MODEL_GROUP,
)


__all__ = [
    "ToolPlugin",
    "PluginRegistry",
    "function_tool",
    "tool_registry",
    "register_tool",
    "register_external",
    "unregister_tool",
    "call_tool",
    "schemas",
    "build_plugin_registry",
    "bind_vision_runtime",
    "vision_describe",
    "som_ground",
    "som_marks",
    "som_last_result",
    "tap_by_mark",
    "bind_execution_module",
    "press",
    "hotkey",
    "input_text",
    "wait",
    "click",
    "drag",
    "observe",
    "read_screen_text",
    "ocr_screenshot",
    "screenshot",
    "get_ui_tree",
    "tap_by_id",
    "tap_text",
    "launch_app",
    "press_keycode",
    "collect_list",
    "template_match",
    "wait_for",
    "run_python",
    "configure_python",
    "build_mcp_servers",
    "load_skill",
    "set_skill_task_context",
    "skill_load_limit",
    "configure_local_model",
    "configure_local_model_from_config",
    "render_local_model_description",
    "local_model_registered",
    "LOCAL_INFER_TOOL",
    "LOCAL_MODEL_GROUP",
    "shell_exec",
    "configure_shell",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "search_content",
    "mkdir",
    "generate_report",
    "configure_filesystem",
    "web_fetch",
    "web_search",
    "configure_web",
]
