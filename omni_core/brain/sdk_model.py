"""把 LLMClient 的 OpenAI 兼容配置接入 OpenAI Agents SDK 的 Model 抽象。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.1）：
- 大脑（在线大模型 / 本地模型）统一走 openai-compatible 端点。
- SDK 原生 ``OpenAIChatCompletionsModel`` 直接消费 AsyncOpenAI 客户端，
  因此「BrainClient 降级为 model provider」= 用 cfg 构造 AsyncOpenAI 喂给 SDK。
- 上层（tool_loop / Agent）不再持有手搓 chat，只拿到一个 ``Model`` 实例。

注意：本模块不引入任何业务/场景逻辑，仅做配置到 SDK Model 的映射。
"""
from openai import AsyncOpenAI

from agents import Model, OpenAIChatCompletionsModel
from agents.exceptions import ModelBehaviorError

import httpx


class _MalformedResponseGuard(Model):
    """协议层兜底：上游网关返回非标准响应体时给清晰错误，而不是崩成 AttributeError。

    场景（通用协议问题，非厂商特有）：部分 openai-compatible 网关在软拦截
    （如内容过滤）时返回 HTTP 200 + 非标准体（裸字符串等）。openai 客户端
    原样透传，SDK ``_fetch_response`` 的 ``isinstance(ret, ChatCompletion)``
    不成立即按流式分支返回 ``(Response, stream)`` 元组，随后 ``response.choices``
    抛 ``AttributeError: 'tuple' object has no attribute 'choices'``——
    既难排障又会被上层当成「runner 异常」升级。此处按 SDK 通用错误形态
    （AttributeError + choices 属性名）识别并转成 ModelBehaviorError，
    交由 errors.classify_provider_error 走「provider 拒绝、暂停」的既定降级链路。
    """

    def __init__(self, inner: Model) -> None:
        self._inner = inner

    @staticmethod
    def _is_sdk_tuple_response(e: BaseException) -> bool:
        return isinstance(e, AttributeError) and "has no attribute 'choices'" in str(e)

    async def get_response(self, *args, **kwargs):
        try:
            return await self._inner.get_response(*args, **kwargs)
        except AttributeError as e:
            if self._is_sdk_tuple_response(e):
                raise ModelBehaviorError(
                    "模型服务返回非标准响应体（疑似网关/内容过滤软拦截，HTTP 200 但无 choices 结构），"
                    "任务已暂停；请调整输入内容、缩小上下文或更换模型端点后重试。"
                ) from e
            raise

    async def stream_response(self, *args, **kwargs):
        async for chunk in self._inner.stream_response(*args, **kwargs):
            yield chunk


def build_sdk_model(brain_cfg: dict, timeout: float | None = None) -> Model:
    """由 LLMClient 风格的 cfg 构造 SDK Model。

    Args:
        brain_cfg: 含 base_url / model / api_key（或 api_key_env）/ proxy 的 dict。
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
    # 代理：每个提供方可独立配置；留空/缺失 = 直连。鉴权按通用写法嵌进 URL
    # （http://user:pass@host:port），由 httpx 原生解析，无需单独字段。
    proxy = str(brain_cfg.get("proxy") or "").strip()
    if proxy:
        kwargs["http_client"] = httpx.AsyncClient(proxy=proxy)
    client = AsyncOpenAI(**kwargs)
    return _MalformedResponseGuard(OpenAIChatCompletionsModel(model=model, openai_client=client))
