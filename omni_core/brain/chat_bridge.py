"""把「只实现 `chat()` 的大脑」包成 SDK `Model`（适配层，非循环）。

用途：让 Agents SDK 的 `Runner` 能驱动任何 OpenAI 兼容大脑，包括
- 真实大脑：`omni_core.brain.llm.LLMClient`（它本身 `sdk_model()` 就是真 SDK Model，
  走不到这里）；
- 只暴露 `chat(messages, tools, tool_choice) -> BrainReply` 的同步大脑/脚本化假大脑。

它只做**类型适配**（SDK `ModelRequest` <-> OpenAI chat 消息），
不实现任何循环/协议——循环归 Runner，协议归 SDK Model。
"""
from typing import Any, Dict, List, Optional

from agents import ModelSettings, ModelTracing
from agents.models.interface import ModelResponse
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from omni_core.brain.llm import _to_chat_messages, BrainReply


def build_chat_bridge_model(chat_obj: Any):
    """返回一个 SDK Model：把请求转给 `chat_obj.chat(...)`，把回复转回 ModelResponse。

    Args:
        chat_obj: 任何提供 `chat(messages, tools, tool_choice) -> BrainReply` 的对象。
    """
    from agents import Model

    class _ChatBridgeModel(Model):
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ) -> ModelResponse:
            messages = _to_chat_messages(system_instructions, input)
            chat_tools = _to_chat_tools(tools)
            tool_choice = "auto"
            if chat_tools:
                tc = getattr(model_settings, "tool_choice", None)
                tool_choice = tc if isinstance(tc, (str, dict)) else "auto"
            reply: BrainReply = _call_chat(
                self.inner, messages, chat_tools or None, tool_choice
            )
            return _reply_to_model_response(reply)

        async def stream_response(self, *args, **kwargs):  # 本适配不走流式
            raise NotImplementedError

    return _ChatBridgeModel(chat_obj)


def _call_chat(inner: Any, messages: List[Dict[str, Any]], tools: Any, tool_choice: Any) -> BrainReply:
    """调用 `.chat()`：按被调方的形参个数自适应（兼容 1/2/3 参的既有大脑与假大脑）。"""
    last_err: Optional[BaseException] = None
    for args in ((messages, tools, tool_choice), (messages, tools), (messages,)):
        try:
            return inner.chat(*args)
        except TypeError as e:
            last_err = e
    raise last_err if last_err else RuntimeError("chat() 调用失败")


def _to_chat_tools(sdk_tools: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """SDK Tool（FunctionTool / dict）-> OpenAI function schema。"""
    out: List[Dict[str, Any]] = []
    for t in sdk_tools or []:
        if isinstance(t, dict):
            out.append(t)
            continue
        out.append(
            {
                "type": "function",
                "function": {
                    "name": getattr(t, "name", ""),
                    "description": getattr(t, "description", "") or "",
                    "parameters": getattr(t, "params_json_schema", None) or {},
                },
            }
        )
    return out


_call_seq = None


def _next_call_id(index: int, explicit: str) -> str:
    """生成**唯一** call_id。

    call_id 重复会让 SDK 把后续同名工具调用判为重复而跳过执行，
    因此即使调用方每次都给同一个 id，这里也要保证全局唯一。
    """
    global _call_seq
    if _call_seq is None:
        import itertools

        _call_seq = itertools.count(1)
    return explicit or f"call_{next(_call_seq)}_{index}"


def _reply_to_model_response(reply: BrainReply, usage: Any = None) -> ModelResponse:
    """BrainReply -> SDK ModelResponse（供 Runner 消费）。"""
    output: List[Any] = []
    if (reply.content or "").strip():
        output.append(
            ResponseOutputMessage(
                id="msg_bridge",
                content=[ResponseOutputText(annotations=[], text=reply.content or "", type="output_text")],
                role="assistant",
                status="completed",
                type="message",
            )
        )
    for i, tc in enumerate(reply.tool_calls or []):
        import json

        raw = tc.args if isinstance(tc.args, str) else json.dumps(tc.args or {}, ensure_ascii=False)
        call_id = _next_call_id(i, tc.id)
        output.append(
            ResponseFunctionToolCall(
                id=f"fc_{call_id}",
                arguments=raw,
                call_id=call_id,
                name=tc.name,
                type="function_call",
            )
        )
    if usage is None:
        from agents.usage import Usage

        usage = Usage()
    from uuid import uuid4

    return ModelResponse(output=output, usage=usage, response_id=f"resp_{uuid4().hex[:12]}")
