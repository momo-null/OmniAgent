"""能力门（tools_enabled）回归：外部可达通道的消息必须是纯对话。

三层守住：
1. 内核装配层：tools_enabled=False 时 Agent 的 tools 为空、元工具不注入、MCP 不连接；
2. 网关派发层：通道的 allow_tools 透传到 dispatch，最终进入 ToolLoop；
3. 通道层：QQ / 飞书缺省 allow_tools=False（fail-closed）。
"""
import types

import pytest

from gateway.channels.base import InboundMessage
from omni_core.brain import sdk_loop as sl
from omni_core.local.loop import ToolLoop


class _FakeBrain:
    """LLMClient 替身：构造即用，不连网。"""

    def __init__(self, *a, **k):
        self.model = "m"


def _loop(monkeypatch, **kw):
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _FakeBrain)
    return ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
                    verbose=False, **kw)


# === 1. 内核装配层 ===

def test_tool_loop_defaults_to_tools_enabled(monkeypatch):
    assert _loop(monkeypatch).tools_enabled is True


def test_chat_only_loop_drops_mcp(monkeypatch):
    loop = _loop(monkeypatch, tools_enabled=False)
    assert loop.tools_enabled is False
    assert loop.mcp_servers == []


def _assemble(monkeypatch, captured, mcp_calls=None, **kw):
    """调用 _assemble_agent_stack，拦截 Agent 构造与 MCP 连接。"""
    class _FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def _mcp(servers):
        if mcp_calls is not None:
            mcp_calls.append(list(servers))
        return []

    monkeypatch.setattr(sl, "Agent", _FakeAgent)
    monkeypatch.setattr(sl, "_ensure_mcp_connected", _mcp)
    sl._assemble_agent_stack(
        brain=types.SimpleNamespace(sdk_model=lambda: object()),
        name="x",
        instructions="i",
        tools=[object()],
        gate=types.SimpleNamespace(),
        mcp_servers=[object()],
        max_steps=5,
        allow_dispatch=True,
        todo_store=None,
        budget_hint_ratio=0.0,
        max_input_tokens=0,
        compress_threshold=0.0,
        hard_ceiling=0.0,
        retain_ratio=0.0,
        summarize=None,
        max_output_tokens=0,
        on_truncate=None,
        model_settings=None,
        on_state=None,
        **kw,
    )


def test_assemble_chat_only_has_no_tools_and_no_stop_behavior(monkeypatch):
    captured, mcp_calls = {}, []
    _assemble(monkeypatch, captured, mcp_calls, tools_enabled=False)
    # 能力插件 + 元工具（task_done/verify/record/dispatch）全部不装配
    assert captured["tools"] == []
    assert captured["mcp_servers"] == []
    # 连 MCP 都不去连（不是"连上不给用"）
    assert mcp_calls == []
    # 无工具时不挂 StopAtTools（否则 SDK 侧多一条无意义的收尾规则）
    assert "tool_use_behavior" not in captured


def test_assemble_with_tools_still_injects_meta_tools(monkeypatch):
    captured = {}
    _assemble(monkeypatch, captured, tools_enabled=True)
    names = [getattr(t, "name", "") for t in captured["tools"]]
    assert "task_done" in names
    assert "verify" in names
    assert "tool_use_behavior" in captured


class _FakeBackend:
    tool_schemas = []

    def __init__(self, *a, **k):
        self.kind = "host"

    def observe(self):
        return {"active_window": "fake", "ocr_text": []}

    def screenshot(self):
        return {"ok": True, "path": "none"}

    def read_screen_text(self):
        return {"ok": True, "ocr_text": []}

    def execute_mouse_action(self, action, args):
        return True

    def execute_keyboard_action(self, action, args):
        return True


def test_chat_only_run_finishes_with_plain_text(monkeypatch):
    """空工具下循环仍能正常收口（纯文本收尾），且全程没有工具调用。"""
    from omni_core.brain.llm import BrainReply
    from omni_core.local.loop import TaskSpec
    from tests._env import install_fake_env

    calls = {"chat": 0}

    class _FB:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.model = cfg.get("model", "")

        def chat(self, messages, tools=None, tool_choice="auto"):
            calls["chat"] += 1
            # 纯对话：只回文本，绝不产生 tool_call
            assert not tools, f"纯对话运行不应下发任何工具: {tools}"
            return BrainReply(content="你好呀，我在。", tool_calls=[], finish_reason="stop")

        def close(self):
            pass

    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _FB)
    install_fake_env(monkeypatch, _FakeBackend)
    loop = ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "capabilities": {}},
                    verbose=False, tools_enabled=False)
    # 收尾意图门自身也是一次 LLM 调用，这里放行（本用例只验证无工具收口）
    monkeypatch.setattr(loop, "_plain_finish_judge", lambda traj: (lambda text: None))

    res = loop.run_task(TaskSpec(objective="你好", task_id="tg_chat", max_steps=3))
    assert res.get("success") is True, res
    assert calls["chat"] >= 1


def test_build_user_content_is_plain_without_images():
    from omni_core.local.loop.sdk_bridge import build_user_content

    assert build_user_content("你好") == "你好"


def test_build_user_content_multimodal_with_attachments():
    from omni_core.local.loop.sdk_bridge import build_user_content

    content = build_user_content(
        "看看这个", [{"type": "image", "data_url": "data:image/png;base64,AAA"}])
    assert content[0] == {"type": "input_text", "text": "看看这个"}
    assert content[1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}


def test_build_user_content_ignores_unknown_attachment_types():
    """未知 type 忽略且不破坏文本：全部未知时退化成纯文本（fail-open）。"""
    from omni_core.local.loop.sdk_bridge import build_user_content

    assert build_user_content("hi", [{"type": "video", "path": "x"}]) == "hi"


# === 2. 网关派发层 ===

def test_dispatch_submit_propagates_tools_enabled(monkeypatch):
    from gateway import dispatch

    items = []
    monkeypatch.setattr(dispatch, "ensure_worker", lambda: None)
    monkeypatch.setattr(dispatch._QUEUE, "put", lambda item: items.append(item))

    dispatch.submit("hi", tools_enabled=False)
    dispatch.submit("hi")
    assert items[0]["tools_enabled"] is False
    assert items[1]["tools_enabled"] is True


def test_gateway_on_message_passes_channel_policy(monkeypatch):
    from gateway import channel_gateway as gw

    seen = {}
    monkeypatch.setattr(gw.dispatch, "ensure_worker", lambda: None)
    monkeypatch.setattr(gw.dispatch, "submit", lambda *a, **kw: seen.update(kw) or "t1")

    gw._on_message(InboundMessage(channel="napcat", chat_id="private:2", user_id="2",
                                  text="hi", reply=None))
    assert seen["tools_enabled"] is False

    gw._on_message(InboundMessage(channel="napcat", chat_id="private:2", user_id="2",
                                  text="hi", reply=None, allow_tools=True))
    assert seen["tools_enabled"] is True


# === 3. 通道层 fail-closed ===

def test_inbound_message_defaults_to_no_tools():
    assert InboundMessage(channel="x", chat_id="c", user_id="u", text="t").allow_tools is False


def test_napcat_allow_tools_defaults_false(monkeypatch):
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    from gateway.channels.napcat import NapcatAdapter

    assert NapcatAdapter({"enabled": True}).allow_tools is False
    assert NapcatAdapter({"enabled": True, "allow_tools": True}).allow_tools is True
