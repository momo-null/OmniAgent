"""M2.5 测试：能力动态注入 + 视觉 SoM（som_marks）+ tap_by_mark 双路。

不依赖 GPU / 真实模型 / 模拟器：视觉 VLM 用假 client 注入，执行后端用假对象。
"""
import os

import pytest

from omni_core.tools.base import call_tool
from omni_core.tools.loader import load_plugins, plugin_module
from omni_core.brain.prompt import (
    capability_block,
    tool_catalog_block,
    build_system_prompt,
)
from plugins.vision.runtime import VisionRuntime


# --- 能力动态注入 ----------------------------------------------------------
from tests._env import install_fake_env  # noqa: E402


@pytest.fixture(autouse=True)
def _load_vision_plugin(enable_plugin):
    """本文件直接用 vision 插件的运行时 / 工具：先**显式打开**它再装载。

    vision 默认 `enabled: false`（治"每次都用 vision"），且装载是进程级幂等——
    必须显式打开并主动装载，不能依赖别的测试文件先跑过（旧版正是这么偶然通过的）。
    """
    enable_plugin("vision")
    load_plugins({})


def test_capability_block_text_only():
    block = capability_block({"vision": False})
    assert "无原生视觉" in block
    assert "视觉" in block  # 引导走视觉工具


def test_capability_block_text_vision():
    block = capability_block({"vision": True})
    assert "文本 + 视觉" in block
    assert "可直接理解" in block  # 说明可原生看


def test_build_system_prompt_lists_tools():
    schemas = [
        {"type": "function", "function": {"name": "vision_describe", "description": "理解截图。其它"}},
        {"type": "function", "function": {"name": "tap_by_mark", "description": "按标记点击。其它"}},
    ]
    prompt = build_system_prompt({"vision": False}, schemas)
    assert "vision_describe" in prompt
    assert "tap_by_mark" in prompt
    assert "无原生视觉" in prompt


# --- som_marks 派发 ---------------------------------------------------------
class _FakeVision:
    def __init__(self):
        self.calls = []

    def visual_so_m(self, ask=""):
        self.calls.append(ask)
        return {"ok": True, "marks": [{"id": 1, "center": [0.5, 0.6], "ref": "", "label": "x"}]}


def test_som_marks_dispatch_routes_to_vision():
    """M3：派发已迁至 agent 外层插件层（call_tool），视觉工具经运行时注入。"""
    fake = _FakeVision()
    plugin_module("vision").bind_vision_runtime(fake)
    res = call_tool("som_marks", {"ask": "列出所有按钮"})
    assert res["ok"] is True
    assert fake.calls == ["列出所有按钮"]


# --- 视觉 SoM 解析 + 点击双路 ----------------------------------------------
class _FakeExec:
    def __init__(self):
        self.mouse_actions = []  # 记录 (action_type, params)
        self.tapped_id = None

    def screenshot(self):
        return {"path": self._path}

    def execute_mouse_action(self, action_type, params):
        # 修复后 tap_by_mark 视觉路径走这里（而非不存在的 click()）
        self.mouse_actions.append((action_type, params))
        return {"ok": True}

    def tap_by_id(self, rid):
        self.tapped_id = rid
        return {"ok": True}


class _FakeVLClient:
    """伪装成 httpx.Client，让 LocalVision.describe 直接返回给定文本。"""

    def __init__(self, text):
        self._text = text

    def post(self, url, json=None):
        class _Resp:
            status_code = 200

            def json(self_inner):
                return {"choices": [{"message": {"content": self._text}}]}

        return _Resp()


def _make_vr(tmp_path, vl_text, marks_ref=None):
    from PIL import Image

    img = Image.new("RGB", (100, 100), (255, 255, 255))
    p = str(tmp_path / "shot.png")
    img.save(p)
    exec_mod = _FakeExec()
    exec_mod._path = p
    cfg = {"enabled": True, "model": "m", "base_url": "http://fake"}
    vr = VisionRuntime(exec_mod, cfg, client=_FakeVLClient(vl_text))
    return vr, exec_mod


def test_visual_so_m_parses_and_tap_by_coord(tmp_path):
    vr, exec_mod = _make_vr(
        tmp_path, '[{"id":1,"x":0.5,"y":0.6,"label":"开始按钮"}]'
    )
    res = vr.visual_so_m()
    assert res["ok"] is True
    assert len(res["marks"]) == 1
    m = res["marks"][0]
    assert m["center"] == [0.5, 0.6]
    # 视觉 mark -> 归一化坐标点击，修复后应走 execute_mouse_action("click", ...)
    tap = vr.tap_mark(res["marks"][0])
    assert tap["ok"] is True
    assert exec_mod.mouse_actions == [("click", {"x": 0.5, "y": 0.6})]


def test_visual_so_m_robust_to_surrounding_text(tmp_path):
    vr, _ = _make_vr(
        tmp_path,
        '好的，这是识别结果：\n[{"id":1,"x":0.1,"y":0.2,"label":"a"},'
        '{"id":2,"x":0.9,"y":0.8,"label":"b"}]\n结束。',
    )
    res = vr.visual_so_m()
    assert res["ok"] is True
    assert len(res["marks"]) == 2
    assert res["marks"][1]["center"] == [0.9, 0.8]


def test_tap_by_mark_structured_uses_resource_id(tmp_path):
    """结构化 mark（带 ref）走 tap_by_id，不走坐标。"""
    vr, exec_mod = _make_vr(tmp_path, "[]")
    tap = vr.tap_mark({"id": 3, "ref": "com.x:id/btn", "label": "btn", "center": [0.0, 0.0]})
    assert tap["ok"] is True
    assert exec_mod.tapped_id == "com.x:id/btn"
    assert exec_mod.mouse_actions == []  # 结构化 mark 不应走坐标点击


def test_tap_by_mark_unknown_id():
    """未知 id 在插件侧被拒（运行时不持有 marks 状态）。"""
    vision_tool = plugin_module("vision")

    assert vision_tool.tap_by_mark(99)["ok"] is False
    assert "未知" in vision_tool.tap_by_mark(99)["error"]


# --- tool_loop observe 路由到后端（Bug 3 回归） -------------------------------
def test_tool_loop_observe_routes_to_backend(monkeypatch):
    """observe 必须走当前执行后端，不能硬编码宿主机 observer（否则模拟器场景
    会读到宿主机桌面，与截图/vision 矛盾）。"""
    from omni_core.brain.llm import BrainReply, ToolCall
    from omni_core.local.loop import ToolLoop, TaskSpec

    class _FakeBackend:
        # 注：M4 起后端不再声明 tool_schemas（能力清单由 tool 插件层提供）

        def __init__(self):
            self.observe_calls = 0
            self.backend = _FakeBackendInner()
            self.kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

        def observe(self):
            self.observe_calls += 1
            return {"active_window": "com.fake.game", "ocr_text": ["x"]}

        def screenshot(self):
            return {"path": "none"}


    class _FakeBackendInner:
        """模拟真实执行后端（self.exec.backend 调用点）。"""

        def verify_done(self, cond: str, percept: dict):
            ocr = (percept or {}).get("ocr_text") or []
            ok = any(cond and cond in str(o) for o in ocr)
            return ok, "ok" if ok else "no"

        def text_of(self, percept: dict) -> str:
            p = percept or {}
            win = p.get("active_window")
            ocr = " ".join(p.get("ocr_text") or [])
            return " ".join([w for w in [win, ocr] if w])

    class _FakeBrain:
        def __init__(self, *a, **k):
            self._step = 0

        def chat(self, messages, tools=None):
            captured.append(messages)
            self._step += 1
            if self._step == 1:
                # 先调 observe 获取屏幕状态，再 task_done
                return BrainReply(
                    content="",
                    tool_calls=[ToolCall(name="observe", args={}, id="c0")],
                    finish_reason="stop",
                )
            return BrainReply(
                content="",
                tool_calls=[ToolCall(name="task_done", args={"reason": "ok"}, id="c1")],
                finish_reason="stop",
            )

        def close(self):
            pass

    captured = []
    fb = _FakeBackend()
    install_fake_env(monkeypatch, lambda *a, **k: fb)
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _FakeBrain)

    loop = ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "capabilities": {}}, verbose=False)
    loop.run_task(TaskSpec(objective="o", max_steps=2))

    assert fb.observe_calls >= 1
    assert captured, "大脑应至少被调用一次"
    # 框架态下每步结果以 tool 消息回填（不再每步新造 user 消息），
    # 因此改从 tool 消息里确认 observe 的结果确实来自后端。
    tool_texts = [
        str(m.get("content") or "")
        for msgs in captured
        for m in msgs
        if m.get("role") == "tool"
    ]
    assert any("com.fake.game" in t for t in tool_texts)  # 来自后端 observe，而非宿主机 observer
