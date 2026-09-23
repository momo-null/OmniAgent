"""大模型调用入口 `LLMClient` —— 走 OpenAI Agents SDK 的 Model 抽象。

M4d：`omni_core/brain/client.py` 中手搓的 OpenAI 协议层（httpx 请求、payload 组装、
tool_calls JSON 解析、HTTP 错误处理）**已删除**，改由 SDK 的 `Model` 实现负责：

    手搓态（已删）                     框架态（当前）
    -------------------------         ------------------------------------------
    httpx.Client.post(...)            Model.get_response(...)   # SDK 负责 request
    payload["tools"] = schemas        tools=[Tool]              # SDK 负责转换
    payload["tool_choice"]            ModelSettings.tool_choice # SDK 负责下发
    json.loads(fn["arguments"])       ToolCall 直接来自 SDK      # SDK 负责解析
    resp.json() -> choices[0]         ModelResponse.output      # SDK 负责解析

本模块只剩两件框架集成必需的事（不是协议实现）：
1. OpenAI chat 消息 <-> SDK Responses input items 的**类型映射**（用 SDK 原生 item 形）；
2. 同步调用方 <-> SDK 异步 Model 的**线程桥**（常驻后台 loop）。

模型无关红线（§5.1）：Model 实例由 `sdk_model.build_sdk_model()` 产出，
底层是 openai-compatible 端点（在线网关 / 本地 llama-server 均可），不锁厂商。
"""
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from agents import FunctionTool, ModelSettings, ModelTracing

from omni_core.async_bridge import run_async  # 共享常驻 loop（与 tool 插件层同一个）
from omni_core.brain import sdk_model


def model_health_ok(base_url: Optional[str], timeout: float = 2.0) -> Optional[bool]:
    """探活本地模型端点（llama.cpp 等 OpenAI 兼容 server 的 ``/health``）。

    返回：
      ``True``  : 可达（HTTP 200）
      ``False`` : 不可达（连接失败 / 超时 / 非 200）
      ``None``  : 跳过预检（base_url 为空，或非本机端点——在线 provider 不做预检以免误判）
    """
    if not base_url:
        return None
    bu = str(base_url).strip()
    if "127.0.0.1" not in bu and "localhost" not in bu:
        return None
    # 先探 /health，缺失时回退 /v1/models：两者任一 200 即视为端点可达。
    # 覆盖「server 在跑但模型未就绪 / 未暴露 /health」的情况，使 executor
    # 不可达时能更可靠地回退主模型（而非误判健康、静默烧步数）。
    for path in ("/health", "/v1/models"):
        try:
            import urllib.request

            req = urllib.request.Request(bu.rstrip("/") + path, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any]
    id: str = ""


@dataclass
class BrainReply:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    reasoning: str = ""  # 推理模型真实思考链（reasoning item 的 summary / reasoning_content）


# --- OpenAI chat 消息 -> SDK input items（框架原生类型） ----------------------
def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def _system_instructions(messages: List[Dict[str, Any]]) -> Optional[str]:
    parts = [_text_of(m.get("content")) for m in messages or [] if m.get("role") == "system"]
    joined = "\n\n".join(p for p in parts if p)
    return joined or None


def _to_input_items(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI chat 消息 -> SDK Responses input items（system 走 system_instructions）。"""
    items: List[Dict[str, Any]] = []
    for msg in messages or []:
        role = msg.get("role")
        content = _text_of(msg.get("content"))

        if role == "system":
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id") or "",
                    "output": content,
                }
            )
            continue
        if role == "assistant":
            calls = msg.get("tool_calls") or []
            for tc in calls:
                fn = tc.get("function") or {}
                args = fn.get("arguments", "{}")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id") or "",
                        "name": fn.get("name", "") or tc.get("name", ""),
                        "arguments": args,
                    }
                )
            if content or not calls:
                items.append({"role": "assistant", "content": content})
            continue
        items.append({"role": "user", "content": content})
    return items


async def _not_invoked_here(ctx, arguments: str) -> str:
    """占位：本模式只向 model 询问「下一步调哪个工具」，执行由 tool_loop 侧负责。

    kernel 侧的循环自己派发工具（见 `tool_loop` + `omni_core.tools.call_tool`），
    因此 SDK 永远不会被要求真正 invoke 这里的 FunctionTool。
    """
    raise RuntimeError(
        "该 FunctionTool 仅用于向模型声明工具；执行请走 omni_core.tools.call_tool"
    )


def _field(item: Any, key: str, default: Any = None) -> Any:
    """同时兼容 dict 形态与 SDK 对象形态的 input item。"""
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _to_chat_messages(
    system_instructions: Optional[str],
    input_items: Any,
) -> List[Dict[str, Any]]:
    """SDK input items -> OpenAI chat 消息（供 `.chat()` 形态的大脑消费）。

    主链路走 SDK Runner，不需要这一步；它是给「只实现 chat() 的同步大脑」
    （含测试里的脚本化假大脑）提供的适配，使它们也能被 Runner 驱动。
    """
    msgs: List[Dict[str, Any]] = []
    if system_instructions:
        msgs.append({"role": "system", "content": system_instructions})

    pending_calls: List[Dict[str, Any]] = []

    def _flush() -> None:
        if pending_calls:
            msgs.append({"role": "assistant", "content": "", "tool_calls": list(pending_calls)})
            pending_calls.clear()

    for item in input_items or []:
        itype = _field(item, "type")
        if itype == "function_call":
            pending_calls.append(
                {
                    "id": _field(item, "call_id", "") or "",
                    "type": "function",
                    "function": {
                        "name": _field(item, "name", "") or "",
                        "arguments": _field(item, "arguments", "{}") or "{}",
                    },
                }
            )
            continue
        if itype == "function_call_output":
            _flush()
            out = _field(item, "output", "")
            if not isinstance(out, str):
                try:
                    out = json.dumps(out, ensure_ascii=False)
                except TypeError:
                    out = str(out)
            msgs.append({"role": "tool", "tool_call_id": _field(item, "call_id", "") or "", "content": out})
            continue
        role = _field(item, "role")
        if role in ("user", "assistant", "system", "developer"):
            _flush()
            content = _field(item, "content", "") or ""
            if not isinstance(content, str):
                content = _text_of(content)
            msgs.append({"role": "system" if role == "developer" else role, "content": content})
    _flush()
    return msgs


def _to_sdk_tools(schemas: Optional[List[Dict[str, Any]]]) -> List[FunctionTool]:
    """OpenAI function schema -> SDK FunctionTool（Chat Completions 只认 FunctionTool）。"""
    tools: List[FunctionTool] = []
    for s in schemas or []:
        fn = s.get("function") if isinstance(s, dict) else None
        if fn is None and isinstance(s, dict) and s.get("name"):
            fn = s
        if not isinstance(fn, dict):
            continue
        tools.append(
            FunctionTool(
                name=fn.get("name", ""),
                description=fn.get("description", "") or "",
                params_json_schema=fn.get("parameters") or {"type": "object", "properties": {}},
                on_invoke_tool=_not_invoked_here,
                strict_json_schema=False,
            )
        )
    return tools


def _parse_model_response(resp: Any) -> BrainReply:
    """SDK ModelResponse -> BrainReply（解析已由 SDK 完成，此处只做读取）。"""
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[ToolCall] = []
    for item in getattr(resp, "output", None) or []:
        itype = getattr(item, "type", "")
        if itype == "reasoning":
            # 推理模型真实思考链：summary 文本 或 reasoning_content（与口播分离）
            for s in getattr(item, "summary", None) or []:
                t = getattr(s, "text", None)
                if t:
                    reasoning_parts.append(t)
            rc = getattr(item, "reasoning_content", None)
            if rc:
                reasoning_parts.append(rc)
        elif itype == "message":
            for chunk in getattr(item, "content", None) or []:
                text = getattr(chunk, "text", None)
                if text:
                    content_parts.append(text)
                elif getattr(chunk, "refusal", None):
                    content_parts.append(getattr(chunk, "refusal"))
        elif itype == "function_call":
            raw = getattr(item, "arguments", "") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": raw}
            tool_calls.append(
                ToolCall(
                    name=getattr(item, "name", "") or "",
                    args=args,
                    id=getattr(item, "call_id", "") or "",
                )
            )
    return BrainReply(
        content="".join(content_parts),
        tool_calls=tool_calls,
        finish_reason="stop",
        reasoning="\n".join(reasoning_parts).strip(),
    )


class LLMClient:
    """大脑/执行器统一入口：配置 -> SDK Model -> BrainReply。

    对外契约与旧 client 完全一致（`chat(messages, tools, tool_choice) -> BrainReply`），
    因此上层（tool_loop）零改动；差异只在协议由谁实现（SDK 而非手搓 httpx）。
    """

    def __init__(self, cfg: dict, timeout: float = 180.0, on_debug=None):
        self.on_debug = on_debug
        self.base_url = str(cfg["base_url"]).rstrip("/")
        self.model = cfg["model"]
        env_key = cfg.get("api_key_env", "OMNI_BRAIN_API_KEY")
        # 优先 config.api_key 明文（本机调试），否则回退环境变量
        self.api_key = (cfg.get("api_key") or "").strip() or os.environ.get(env_key, "")
        if not self.api_key:
            raise RuntimeError(
                f"[LLMClient] 缺少大模型 api key：请在 config 设 brain.api_key，"
                f"或设置环境变量 {env_key!r} 后再运行。"
            )
        self.capabilities = cfg.get("capabilities", {}) or {}
        req = cfg.get("request", {}) or {}
        self.temperature = float(req.get("temperature", 0.3))
        self.max_tokens = int(req.get("max_tokens", 2048))
        self.timeout = timeout
        # 协议层：交给 SDK（openai-compatible 端点 -> AsyncOpenAI -> Model）
        self._model = sdk_model.build_sdk_model(cfg, timeout=timeout)

    def sdk_model(self):
        """暴露底层 SDK Model（供 Agents SDK 的 Agent/Runner 直接使用）。

        这是「BrainClient 降级为 model provider」（设计 §5.1）的落点：
        内核不再自己跑循环，只提供一个 Model 给框架。
        """
        return self._model

    def _emit_debug(self, kind: str, payload: Dict[str, Any]) -> None:
        """旁路调试埋点：把模型调用的 prompt / response 推给上层（前端详细日志窗口）。"""
        if not self.on_debug:
            return
        try:
            self.on_debug(kind, dict(payload))
        except Exception:
            pass

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> BrainReply:
        """发一次聊天请求（单次 model 调用，多步循环由上层/tool_loop 驱动）。"""
        sdk_tools = _to_sdk_tools(tools)
        settings = ModelSettings(temperature=self.temperature, max_tokens=self.max_tokens)
        if sdk_tools:
            settings.tool_choice = tool_choice or "auto"

        # 埋点：发送给 LLM 的完整 prompt（全量，前端可折叠）
        self._emit_debug("llm_request", {
            "model": self.model,
            "base_url": self.base_url,
            "prompt": _format_messages(messages),
            "tools": [t.get("function", {}).get("name")
                      for t in (tools or []) if isinstance(t, dict)],
        })

        coro = self._model.get_response(
            system_instructions=_system_instructions(messages),
            input=_to_input_items(messages),
            model_settings=settings,
            tools=sdk_tools,
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
        try:
            resp = run_async(coro, self.timeout)
        except Exception as e:  # 统一成 RuntimeError，保持旧契约（上层已有兜底）
            self._emit_debug("llm_error", {"model": self.model, "error": f"{type(e).__name__}: {e}"})
            raise RuntimeError(f"[LLMClient] 调用失败: {type(e).__name__}: {e}") from e
        reply = _parse_model_response(resp)
        # 埋点：LLM 返回的 thought + 决策（tool_calls）
        self._emit_debug("llm_response", {
            "model": self.model,
            "content": reply.content or "",
            "tool_calls": [
                {"name": getattr(tc, "name", "?"),
                 "arguments": getattr(tc, "arguments", getattr(tc, "args", ""))}
                for tc in (reply.tool_calls or [])
            ],
            "finish_reason": reply.finish_reason,
        })
        return reply

    def close(self):
        try:
            run_async(self._model.close(), 5.0)
        except Exception:
            pass


def _format_messages(messages: List[Dict[str, Any]]) -> str:
    """把 OpenAI 格式 messages 拼成可读全文（含 tool_calls），供详细日志展示。"""
    parts: List[str] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        content = m.get("content")
        if isinstance(content, list):  # 多模态 content（文本段）
            content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        tcs = m.get("tool_calls") or []
        if tcs:
            for tc in tcs:
                fn = tc.get("function") if isinstance(tc, dict) else None
                name = fn.get("name") if isinstance(fn, dict) else getattr(tc, "name", "?")
                args = fn.get("arguments") if isinstance(fn, dict) else getattr(tc, "arguments", "")
                parts.append(f"[{role}] ▶ 调用工具 {name}:\n{args}")
        else:
            parts.append(f"[{role}] {content or ''}")
    return "\n\n".join(parts)
