"""M1：OpenAI Agents SDK 接管单步 ReAct（POC）。

把「工具=插件、平级外置」范式接到 SDK 运行时：
- Agent 用 SDK 原生 ``Agent`` 表达；工具用 SDK 原生 ``function_tool`` 包装
  ``omni_core.tools`` 里的业务函数（函数体不变，仅换装饰器）。
- 循环（ReAct / function-call / 回填）由 ``Runner.run`` 接管，内核零手搓。
- 护城河（WorldModel/Curator/Trajectory）作为 SDK ``RunHooks`` 后处理钩子挂载，
  与循环解耦（设计 §5.3）。

本模块不写任何场景/业务逻辑，只做「SDK 运行时装配 + 钩子挂载」。
"""
from typing import Any, Callable, List, Optional

from agents import Agent, Runner, RunHooks, function_tool
from agents.run_context import RunContextWrapper

from omni_core.brain import sdk_model
from omni_core.tools.base import sdk_tools

#: 默认工具清单由注册表按 group 统一提供（不再按名硬编码具体插件，内核零反向依赖）。


class OmniRunHooks(RunHooks):
    """护城河后处理钩子：每步工具结果经回调落盘（Trajectory/Curator）。

    挂载点（设计 §5.3）：在 SDK 一步完成（含 tool 执行）后回调，与循环解耦。
    本类不写死护城河内部 schema，只把 (tool_name, result) 交给注入的回调。
    """

    def __init__(self, on_tool_result: Optional[Callable[[str, Any], None]] = None):
        self._on_tool_result = on_tool_result

    async def on_tool_end(
        self,
        context: RunContextWrapper,
        agent: Agent,
        tool: Any,
        result: Any,
    ) -> None:
        if self._on_tool_result is not None:
            try:
                name = getattr(tool, "name", "?")
                self._on_tool_result(name, result)
            except Exception:
                pass


def sdk_function_tool(func):
    """把 omni_core.tools 的业务函数包成 SDK FunctionTool（自动产出 schema）。

    函数体来自 omni_core.tools（平级插件）；此处仅加 SDK 装饰器。M0 的轻量
    function_tool 在 M1 起不再作为主路径。
    """
    return function_tool(func)


def build_omni_agent(
    model,
    tools: Optional[List[Any]] = None,
    instructions: str = "",
) -> Agent:
    """装配 M1 单 agent（框架接管 ReAct + 工具路由）。

    Args:
        model: SDK Model 实例（由 sdk_model.build_sdk_model 产出）。
        tools: SDK FunctionTool 列表（用 sdk_function_tool 包装业务函数得到）。
        instructions: 系统指令（由 prompt.build_system_prompt 产出，本模块不持有）。
    """
    tools = tools or sdk_tools()
    return Agent(
        name="omni_agent",
        model=model,
        instructions=instructions or "你是 OmniAgent 的执行 agent，按目标调用可用工具完成步骤。",
        tools=tools,
    )


async def run_task_sdk(
    agent: Agent,
    user_input: str,
    hooks: Optional[RunHooks] = None,
    max_turns: int = 10,
) -> Any:
    """跑一次 SDK 单 agent 循环（ReAct + function-call + 回流），返回 RunResult。"""
    return await Runner.run(agent, user_input, hooks=hooks, max_turns=max_turns)
