"""M4d：协议层改由 OpenAI Agents SDK 承载 —— 传输层单测 + 端到端验证。

两级验证：
1) 单元：注入脚本化的 SDK Model，验证 OpenAI chat 消息 <-> SDK input items 的映射，
   以及 ModelResponse -> BrainReply 的读取。
2) 端到端：真起一个本地 OpenAI 兼容 HTTP server + 真实 OpenAIChatCompletionsModel，
   跑一次 ToolLoop.run_task，证明「换掉手搓 httpx 协议层」后主链路仍然通。

不依赖真实模型 / GPU / 模拟器。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agents import Model
from agents.usage import Usage
from agents.models.interface import ModelResponse
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from omni_core.brain.llm import (
    BrainReply,
    LLMClient,
    ToolCall,
    _to_input_items,
    _to_sdk_tools,
    _system_instructions,
)
from omni_core.brain import sdk_model


# --- ---------------------------------------------------------------- 脚本化 Model
class _ScriptedModel(Model):
    """按脚本产出 ModelResponse，并记录每次收到的调用参数。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

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
        self.calls.append(
            {
                "system_instructions": system_instructions,
                "input": input,
                "tools": tools,
                "tool_choice": getattr(model_settings, "tool_choice", None),
                "temperature": getattr(model_settings, "temperature", None),
            }
        )
        return self.script.pop(0) if self.script else _resp()

    async def stream_response(self, *args, **kwargs):  # 本测试不走流式
        raise NotImplementedError


def _resp(*items):
    return ModelResponse(output=list(items), usage=Usage(), response_id="resp_test")


def _msg(text):
    return ResponseOutputMessage(
        id="m1",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )


def _call(name, args, call_id="c1"):
    return ResponseFunctionToolCall(
        id="fc1",
        arguments=json.dumps(args, ensure_ascii=False),
        call_id=call_id,
        name=name,
        type="function_call",
    )


def _client_with_model(monkeypatch, script, cfg=None):
    fake = _ScriptedModel(script)
    monkeypatch.setattr(sdk_model, "build_sdk_model", lambda c, timeout=None: fake)
    cfg = cfg or {"base_url": "http://127.0.0.1:1/v1", "model": "stub", "api_key": "k"}
    return LLMClient(cfg), fake


BASE_MSG = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]


# --- ---------------------------------------------------------------- 消息映射
def test_system_message_goes_to_system_instructions(monkeypatch):
    cli, fake = _client_with_model(monkeypatch, [_resp(_msg("ok"))])
    assert cli.chat(BASE_MSG) == BrainReply(content="ok", tool_calls=[], finish_reason="stop")
    assert fake.calls[0]["system_instructions"] == "SYS"
    assert {"role": "user", "content": "hi"} in fake.calls[0]["input"]


def test_assistant_toolcall_and_tool_result_mapping():
    msgs = BASE_MSG + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "c9", "type": "function", "function": {"name": "click", "arguments": '{"x":1}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "c9", "name": "click", "content": '{"ok":true}'},
    ]
    items = _to_input_items(msgs)
    assert {"type": "function_call", "call_id": "c9", "name": "click", "arguments": '{"x":1}'} in items
    assert {"type": "function_call_output", "call_id": "c9", "output": '{"ok":true}'} in items
    assert not any(i.get("role") == "system" for i in items)


def test_tool_schema_converted_to_sdk_tool():
    from agents import FunctionTool

    tools = _to_sdk_tools(
        [{"type": "function", "function": {"name": "click", "description": "点击", "parameters": {"p": 1}}}]
    )
    assert len(tools) == 1
    t = tools[0]
    assert isinstance(t, FunctionTool)
    assert (t.name, t.description, t.params_json_schema, t.strict_json_schema) == (
        "click", "点击", {"p": 1}, False,
    )


def test_tool_choice_only_when_tools_present(monkeypatch):
    cli, fake = _client_with_model(
        monkeypatch,
        [_resp(), _resp()],
    )
    cli.chat(BASE_MSG, tools=[{"type": "function", "function": {"name": "plan"}}],
             tool_choice={"type": "function", "function": {"name": "plan"}})
    cli.chat(BASE_MSG)  # 无工具
    assert fake.calls[0]["tool_choice"] == {"type": "function", "function": {"name": "plan"}}
    assert fake.calls[1]["tool_choice"] is None


def test_model_response_parsed_into_brain_reply(monkeypatch):
    cli, _ = _client_with_model(
        monkeypatch, [_resp(_msg("思考"), _call("observe", {}, "z1"))]
    )
    reply = cli.chat(BASE_MSG)
    assert reply.content == "思考"
    assert reply.tool_calls == [ToolCall(name="observe", args={}, id="z1")]


def test_bad_arguments_json_degrades_to_raw(monkeypatch):
    bad = ResponseFunctionToolCall(id="x", arguments="{not json", call_id="z2", name="f", type="function_call")
    cli, _ = _client_with_model(monkeypatch, [_resp(bad)])
    reply = cli.chat(BASE_MSG)
    assert reply.tool_calls[0].args == {"_raw": "{not json"}


def test_missing_api_key_raises():
    with pytest.raises(RuntimeError):
        LLMClient({"base_url": "http://x/v1", "model": "m"})


def test_default_request_settings_forwarded(monkeypatch):
    cli, fake = _client_with_model(
        monkeypatch,
        [_resp()],
        cfg={"base_url": "http://x/v1", "model": "m", "api_key": "k",
             "request": {"temperature": 0.7, "max_tokens": 512}},
    )
    cli.chat(BASE_MSG)
    assert fake.calls[0]["temperature"] == 0.7


# --- ---------------------------------------------------------------- 端到端
def _start_stub_server(responses, requests):
    """本地 OpenAI 兼容 stub：按脚本返回 chat.completions 响应，并记录请求体。"""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            requests.append(body)
            n = len(requests) - 1
            payload = responses[n] if n < len(responses) else {"choices": [{"message": {"content": ""}}]}
            out = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _chat_reply(message, finish="stop"):
    return {
        "id": "1",
        "object": "chat.completion",
        "created": 0,
        "model": "stub",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def test_end_to_end_via_local_openai_compatible_server(monkeypatch):
    """真 transport 跑一次 ToolLoop.run_task：observe -> task_done。"""
    from omni_core.local.tool_loop import ToolLoop, TaskSpec

    requests = []
    responses = [
        _chat_reply(
            {"content": "", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "observe", "arguments": "{}"}}
            ]},
            finish="tool_calls",
        ),
        _chat_reply(
            {"content": "", "tool_calls": [
                {"id": "t2", "type": "function", "function": {"name": "task_done", "arguments": '{"reason":"ok"}'}}
            ]},
            finish="tool_calls",
        ),
    ]
    srv = _start_stub_server(responses, requests)
    try:
        port = srv.server_address[1]

        class _FakeBackend:
            def __init__(self, *a, **k):
                self.backend = self
                self.texted = []
                self.backend_kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

            def observe(self):
                return {"ok": True, "percept": "p"}

            def text_of(self, percept):
                return str(percept)

            def verify_done(self, cond, percept):
                return True, "ok"

        monkeypatch.setattr("omni_core.local.loop.core.ExecutionModule", _FakeBackend)
        loop = ToolLoop(
            {"model": "stub", "base_url": f"http://127.0.0.1:{port}/v1",
             "capabilities": {}, "api_key": "stub-key"},
            verbose=False,
        )
        res = loop.run_task(TaskSpec(objective="o", max_steps=3))
        assert res["success"] is True, res

        # 请求确实走到了 /chat/completions，且工具 schema 已由 SDK 下发
        assert len(requests) == 2
        assert requests[0]["model"] == "stub"
        names = {t["function"]["name"] for t in requests[0].get("tools", [])}
        assert "observe" in names           # 能力工具：来自 tool 插件层
        assert "task_done" in names         # 内核元工具：必须下发，否则模型不会收尾
        assert "record" in names
        # 第二轮应带上 observe 的 assistant tool_call + tool 结果转换后的消息
        roles = [m.get("role") for m in requests[1]["messages"]]
        assert roles.count("tool") >= 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_system_instructions_concatenates_multiple_system_messages():
    msgs = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "u"},
        {"role": "system", "content": "B"},
    ]
    assert _system_instructions(msgs) == "A\n\nB"
