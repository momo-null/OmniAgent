"""agent 外层 tool 插件层 —— 注册中心（基于 OpenAI Agents SDK）。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §3.5 / §5.0）：
工具=插件，完全平级，由 LLM 直接调用；内核零持有、零派发。

**M5：四跳全部交给 SDK**
- [1] 定义：用 SDK `agents.tool.function_tool`（schema 由 SDK 从签名+docstring 产出，
      内核不再自研 _build_schema）。
- [2] 注册：SDK 的 `FunctionTool` 直接登记进本注册表；外部 MCP 工具由 SDK 的
      `MCPServer` 在 Runner 里加载，与自研工具同层平级（本表只是"枚举/直接调用"的视图）。
- [3] 派发：主链路走 SDK `Runner.run`（原生 dispatch）；本模块的 `call_tool` 只在
      同步场景（测试 / 后端只读调用）直接 invoke 同一个 `FunctionTool`。
- [4] 回填：由 SDK 完成（assistant/tool 消息、function-call 序列化全框架接管）。

为什么还保留一张进程内表：
- 供 `GET /api/runtime/tools` 做只读枚举；
- 供同步调用方（单测、脚本）按名 invoke；
- 能力分组（config.runtime.tools.groups）需要一张可按 group 过滤的清单。
它不是"派发器"——派发是 SDK Runner 的事。
"""
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from agents.tool import FunctionTool, function_tool as sdk_function_tool
from agents.tool_context import ToolContext

from omni_core.async_bridge import run_async


@dataclass
class ToolPlugin:
    """一个平级 tool 插件：SDK FunctionTool + 能力分组元数据。"""

    name: str
    tool: FunctionTool
    group: str = "generic"        # 能力分组（config 驱动启停，不写死）
    source: str = "builtin"       # builtin | mcp（仅可观测性，二者平级）
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def description(self) -> str:
        return self.tool.description or ""

    @property
    def parameters(self) -> Dict[str, Any]:
        return self.tool.params_json_schema or {}

    @property
    def schema(self) -> Dict[str, Any]:
        """OpenAI function schema 视图（供枚举/日志/只读接口；派发不依赖它）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


#: 进程内注册表：name -> ToolPlugin
TOOL_REGISTRY: Dict[str, ToolPlugin] = {}


def _tool_error_result(ctx: Any, error: Exception) -> str:
    """工具异常 -> 结构化错误串回给 agent（设计 §5.0「错误传播」）。

    框架原生行为：异常不中断循环，而是作为 tool result 回到模型，
    由模型自主重试 / 换工具。这里用 JSON 形状，使直接调用方也能识别。
    """
    import json

    return json.dumps(
        {"ok": False, "error": f"{type(error).__name__}: {error}"},
        ensure_ascii=False,
    )


def function_tool(
    name: Optional[str] = None,
    description: Optional[str] = "",
    group: str = "generic",
    timeout: Optional[float] = None,
) -> Callable[[Callable], Callable]:
    """把业务函数注册为平级 tool 插件（**用 SDK 的 function_tool**）。

    schema 由 SDK 从函数签名 + Google 风格 docstring 产出；参数说明请写在
    docstring 的 `Args:` 段（SDK 原生解析）。

    Args:
        name: 对外工具名（缺省用函数名）。
        description: 工具描述（缺省用 docstring 首段）。
        group: 能力分组，供 config.runtime.tools.groups 过滤。
        timeout: 单次工具调用超时（秒）。

    Usage::
        @function_tool(description="点击归一化坐标", group="device")
        def click(x: float, y: float) -> dict:
            \"\"\"点击屏幕。

            Args:
                x: 横坐标 0~1
                y: 纵坐标 0~1
            \"\"\"
    """
    def deco(func: Callable) -> Callable:
        # strict_mode=False：部分工具有默认参数（如 wait(ms=500)），
        # 严格模式要求所有属性必填且 additionalProperties=false，不适用。
        # failure_error_function：工具异常回成结构化错误串给 agent 自主重试，
        # 不抛穿框架循环（设计 §5.0 错误传播）。
        tool = sdk_function_tool(
            func,
            name_override=name,
            description_override=description or None,
            strict_mode=False,
            use_docstring_info=True,
            timeout=timeout,
            failure_error_function=_tool_error_result,
        )
        register_tool(ToolPlugin(name=tool.name, tool=tool, group=group))
        return func
    return deco


def register_tool(plugin: ToolPlugin) -> None:
    """登记一个插件（自研扩展点）。"""
    TOOL_REGISTRY[plugin.name] = plugin


def register_external(plugin: ToolPlugin) -> None:
    """登记外部工具（MCP）：与自研工具进同一张表，完全平级。"""
    register_tool(plugin)


def unregister_tool(name: str) -> None:
    TOOL_REGISTRY.pop(name, None)


def _plugins(groups: Optional[List[str]] = None,
             excluded: Optional[List[str]] = None) -> List[ToolPlugin]:
    """按能力组过滤的插件视图（只读枚举用）。

    excluded：视图级禁用名单（config.runtime.tools.disabled），在分组过滤
    **之后**执行——高权限放行的工具（如 full_access 放行的 shell）仍可被单工具禁用。
    空/None = 不过滤，默认行为不变。
    """
    if not groups:
        result = list(TOOL_REGISTRY.values())
    else:
        wanted = set(groups)
        result = [p for p in TOOL_REGISTRY.values() if p.group in wanted]
    if excluded:
        ex = set(excluded)
        result = [p for p in result if p.name not in ex]
    return result


def sdk_tools(groups: Optional[List[str]] = None) -> List[FunctionTool]:
    """给 SDK Agent 用的工具清单（派发由 Runner 原生接管）。"""
    return [p.tool for p in _plugins(groups)]


def schemas(groups: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """OpenAI function schema 视图列表（只读枚举用）。"""
    return [p.schema for p in _plugins(groups)]


def _ctx(tool_name: str, arguments: str) -> ToolContext:
    """构造直接 invoke 所需的最小 ToolContext（无 run 上下文，context=None）。"""
    return ToolContext(
        context=None,
        tool_name=tool_name,
        tool_call_id="direct",
        tool_arguments=arguments,
    )


def call_tool(name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """同步场景按名 invoke 同一个 SDK FunctionTool（主链路不走这里，走 Runner）。"""
    import json

    plugin = TOOL_REGISTRY.get(name)
    if plugin is None:
        return {"ok": False, "error": f"unknown tool: {name}"}
    payload = json.dumps(args or {}, ensure_ascii=False)
    try:
        raw = run_async(plugin.tool.on_invoke_tool(_ctx(name, payload), payload))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return _normalize(raw)


def _normalize(raw: Any) -> Dict[str, Any]:
    """工具返回值统一成可序列化 dict。

    dict 原样透传（感知结果等本身带形状的 dict 不被污染）；其余包成 output。
    刻意**不**给 dict 注入 ok：各插件自己声明 ok（设计 [4] 回填），
    内核只按 no_confidence / ok 字段做升级判断。
    """
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {"ok": True}
    if isinstance(raw, str):
        # failure_error_function 回的是结构化错误串：还原成错误 dict，
        # 让直接调用方（测试/同步入口）与框架内 agent 看到同一个形状。
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {"ok": True, "output": raw}
        if isinstance(parsed, dict):
            return parsed
        return {"ok": True, "output": raw}
    return {"ok": True, "output": str(raw)}


class PluginRegistry:
    """agent 外层 tool 插件注册表视图（内核持有的唯一句柄）。

    只暴露：按能力分组过滤的清单（`sdk_tools` 给 Runner / `schemas` 给枚举），
    以及同步场景的按名 invoke（`dispatch`）。派发本身由 SDK Runner 负责。
    """

    def __init__(self, groups: Optional[List[str]] = None,
                 excluded: Optional[List[str]] = None):
        self.groups = list(groups) if groups else None
        # 视图级禁用名单（config.runtime.tools.disabled），默认空=不过滤
        self.excluded = list(excluded) if excluded else None

    @property
    def plugins(self) -> List[ToolPlugin]:
        return _plugins(self.groups, self.excluded)

    @property
    def schemas(self) -> List[Dict[str, Any]]:
        return [p.schema for p in self.plugins]

    def sdk_tools(self) -> List[FunctionTool]:
        return [p.tool for p in self.plugins]

    def names(self) -> List[str]:
        return [p.name for p in self.plugins]

    def has(self, name: str) -> bool:
        return name in TOOL_REGISTRY

    def dispatch(self, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return call_tool(name, args)


def build_plugin_registry(groups: Optional[List[str]] = None,
                           excluded: Optional[List[str]] = None) -> PluginRegistry:
    """构造插件注册表视图（groups=None 表示不过滤，全部启用）。

    excluded：视图级禁用名单（config.runtime.tools.disabled），在分组过滤之后执行。
    """
    return PluginRegistry(groups, excluded)


# 便捷别名（与旧 ToolRegistry 语义对齐）
tool_registry = TOOL_REGISTRY
