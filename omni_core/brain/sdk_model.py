"""把 LLMClient 的 OpenAI 兼容配置接入 OpenAI Agents SDK 的 Model 抽象。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.1）：
- 大脑（在线大模型 / 本地模型）统一走 openai-compatible 端点。
- SDK 原生 ``OpenAIChatCompletionsModel`` 直接消费 AsyncOpenAI 客户端，
  因此「BrainClient 降级为 model provider」= 用 cfg 构造 AsyncOpenAI 喂给 SDK。
- 上层（tool_loop / Agent）不再持有手搓 chat，只拿到一个 ``Model`` 实例。

注意：本模块不引入任何业务/场景逻辑，仅做配置到 SDK Model 的映射。
"""
from openai import AsyncOpenAI

from agents import OpenAIChatCompletionsModel


def build_sdk_model(brain_cfg: dict, timeout: float | None = None) -> OpenAIChatCompletionsModel:
    """由 LLMClient 风格的 cfg 构造 SDK Model。

    Args:
        brain_cfg: 含 base_url / model / api_key（或 api_key_env）/ request 的 dict。
        timeout: 单次请求超时（秒）；None 则用 openai 默认。
    """
    base_url = str(brain_cfg["base_url"]).rstrip("/")
    model = brain_cfg["model"]
    api_key = (brain_cfg.get("api_key") or "").strip()
    env_key = brain_cfg.get("api_key_env", "OMNI_BRAIN_API_KEY")
    if not api_key:
        import os
        api_key = os.environ.get(env_key, "")
    if not api_key:
        raise RuntimeError(
            f"[sdk_model] 缺少大模型 api key：请在 cfg 设 api_key，"
            f"或设置环境变量 {env_key!r}。"
        )

    kwargs = {"base_url": base_url, "api_key": api_key}
    if timeout:
        kwargs["timeout"] = timeout
    client = AsyncOpenAI(**kwargs)
    return OpenAIChatCompletionsModel(model=model, openai_client=client)
