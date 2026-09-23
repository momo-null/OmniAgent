"""M1 验证：OpenAI Agents SDK 接管单步 ReAct POC。

不依赖 GPU；网络调用（真实 LLM 端点）用 _ONLINE 开关，离线仅验证 SDK 装配。
验证点：
- LLMClient cfg -> SDK Model 装配（§5.1 BrainClient 降级为 model provider）
- omni_core.tools 业务函数被 SDK function_tool 包成平级插件（tools 暴露 schema）
- Agent 装配正确、工具 schema 可被 SDK 读取
- 护城河钩子（OmniRunHooks.on_tool_end）在工具执行后触发
"""
import os
import pytest

from omni_core.brain import sdk_model, sdk_agent
from omni_core.tools.vision_tool import bind_vision_runtime, vision_describe


# 用 offline stub 的 AsyncOpenAI 客户端测装配（不触网）
def _stub_cfg():
    return {
        "base_url": "http://127.0.0.1:9999/v1",
        "model": "stub-model",
        "api_key": "stub-key",
    }


def test_sdk_model_builds_from_cfg():
    # LLMClient 风格 cfg -> SDK Model（不触网，仅构造）
    model = sdk_model.build_sdk_model(_stub_cfg())
    assert model is not None
    # 模型名透传
    assert model.model == "stub-model"


def test_agent_exposes_tool_schemas():
    model = sdk_model.build_sdk_model(_stub_cfg())
    agent = sdk_agent.build_omni_agent(model)
    # 4 个视觉工具作为平级插件被 SDK Agent 持有
    names = {t.name for t in agent.tools}
    assert {"vision_describe", "som_ground", "som_marks", "tap_by_mark"}.issubset(names)
    # schema 由 SDK 自动从函数签名产出（含参数）
    vd = next(t for t in agent.tools if t.name == "vision_describe")
    assert "prompt" in vd.params_json_schema["properties"]


def test_sdk_function_tool_wraps_business_fn():
    # 业务函数体来自 omni_core.tools（平级插件），仅加 SDK 装饰器
    ft = sdk_agent.sdk_function_tool(vision_describe)
    assert ft.name == "vision_describe"
    assert callable(ft.on_invoke_tool)


def test_hooks_fire_on_tool_end():
    # 验证护城河钩子：SDK 在工具执行后回调 on_tool_end，把 (tool_name, result)
    # 交给注入的回调（接 Trajectory/Curator 落盘），与循环解耦。
    #
    # 注：本仓库未安装 pytest-asyncio（pyproject 里的 asyncio_mode 只是声明，
    # pytest 会报 "Unknown config option"），async 用例会被判为「不支持的 async def」。
    # 因此这里用 asyncio.run 直接驱动，不依赖任何 pytest 异步插件。
    import asyncio

    fired = []
    hooks = sdk_agent.OmniRunHooks(on_tool_result=lambda n, r: fired.append((n, r)))

    class _FakeTool:
        name = "vision_describe"

    # 直接驱动 hooks 的回调路径（与 SDK 内部调用同形，无需真实 LLM 端点）
    asyncio.run(hooks.on_tool_end(None, None, _FakeTool(), '{"ok": true, "description": "x"}'))
    assert fired and fired[0] == ("vision_describe", '{"ok": true, "description": "x"}')
