"""env 概念退场（v4）回归：零痕迹 / 插件化 / state_text 约定 / 视觉主链路 / verify LLM 判定。

覆盖 plan §7 验收的可自动化子集：
- 内核源码零 env 痕迹（activate_environment / ExecutionModule / env_kind / requires_env /
  text_of / verify_done / self.exec.）；
- 删掉插件目录（plugin_dirs 指空）后内核装载不挂、插件清单含 host/emulator/ocr 且默认停用；
- runtime.backend 残留键无任何消费方；
- state_text 通用约定（observe → 世界模型）；
- verify 走 LLM 判定（mock llm_judge），退化语义；
- capability registry 语义（未注册返回 None）；
- 截图附件通路（image_to_data_url 压缩 / _HumanInjection 一次性注入）。
"""
from __future__ import annotations

import base64
import inspect
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PIL import Image

from omni_core.tools import capability
from omni_core.tools.base import TOOL_REGISTRY, call_tool


_KERNEL_MODULES__NO_ENV_TRACES = (
    "omni_core.local.loop.core",
    "omni_core.local.loop.pool",
    "omni_core.local.loop.graph_runner",
    "omni_core.brain.prompt",
    "omni_core.tools.loader",
    "omni_core.local.world_model",
)
_BAD_TOKENS = (
    "activate_environment", "ExecutionModule", "env_kind",
    "requires_env", "text_of", "verify_done", "self.exec.",
)


# === ① 零痕迹：内核源码 + 空插件目录装载 =======================================
@pytest.mark.parametrize("modname", _KERNEL_MODULES__NO_ENV_TRACES)
def test_kernel_source_has_no_env_traces(modname):
    import importlib

    src = inspect.getsource(importlib.import_module(modname))
    for bad in _BAD_TOKENS:
        assert bad not in src, f"{modname} 残留 env 痕迹: {bad}"


def test_kernel_loads_with_empty_plugin_dirs(tmp_path):
    """「删掉插件目录」的最强模拟：plugin_dirs 指空目录 → 装载零挂、零装载。"""
    from omni_core.tools.loader import PluginContext, load_plugins

    cfg = {"runtime": {"tools": {"plugin_dirs": [str(tmp_path)]}}}
    report = load_plugins(cfg, ctx=PluginContext(config=cfg, wired=False))
    assert report.loaded == [] and report.failed == []


def test_env_plugins_listed_and_disabled_by_default():
    """host / emulator / ocr 与 web/filesystem 同级出现在插件清单，且默认停用。"""
    import config as app_config
    from omni_core.tools.loader import PluginContext, list_plugins, load_plugins

    cfg = app_config.load_config() or {}
    names = {p["name"]: p for p in list_plugins(cfg)}
    for name in ("host", "emulator", "ocr"):
        assert name in names, f"{name} 应出现在插件清单"
        assert names[name]["enabled"] is False, f"{name} 默认停用"
    report = load_plugins(cfg, ctx=PluginContext(config=cfg, wired=False))
    assert not ({"host", "emulator", "ocr"} & set(report.loaded)), "默认停用 = 不装载"


# === ② config 兼容：runtime.backend 残留键无消费方 =============================
def test_runtime_backend_key_unused_in_kernel():
    import importlib

    for modname in ("omni_core.local.loop.core", "omni_core.local.loop.pool",
                    "omni_core.local.loop.graph_runner"):
        src = inspect.getsource(importlib.import_module(modname))
        assert '"backend"' not in src and "'backend'" not in src, f"{modname} 仍读 runtime.backend"


# === ③ state_text 通用约定 =====================================================
def test_state_text_convention_feeds_world_model(monkeypatch):
    """observe 结果自带 state_text → 世界模型存文本；内核不解读感知字段。"""
    from omni_core.local.world_model import WorldModel
    from tests._env import install_fake_env

    class _Fb:
        def __init__(self):
            self.ocr = ["登录成功"]

        def observe(self):
            return {"active_window": "com.fake", "ocr_text": list(self.ocr)}

        def execute_mouse_action(self, *a, **k):
            return True

        def execute_keyboard_action(self, *a, **k):
            return True

    fb = install_fake_env(monkeypatch, _Fb())
    res = call_tool("observe", {})
    assert res["ok"] is True
    assert "登录成功" in res["state_text"]
    assert "com.fake" in res["state_text"]

    # 内核侧：世界模型按约定透传（与 sdk_bridge._on_step 同一调用形状）
    world = WorldModel()
    world.update(res, state_text=str(res.get("state_text") or ""))
    assert "登录成功" in world.current_state_text()
    assert world.current_percept.get("ocr_text") == ["登录成功"]


# === ④ verify 通用 LLM 判定 ====================================================
def _bare_loop():
    from omni_core.local.loop.core import ToolLoop

    loop = ToolLoop.__new__(ToolLoop)  # 只需 brain_cfg，不跑装配
    loop.brain_cfg = {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}
    return loop


def test_verify_uses_llm_judge(monkeypatch):
    from omni_core.local.loop.core import _VERIFY_JUDGE_SYSTEM, ToolLoop
    from omni_core.local.loop.parts import TaskSpec
    from omni_core.local.world_model import WorldModel

    loop = _bare_loop()
    world = WorldModel()
    spec = TaskSpec(objective="o", done_when="已保存")

    # 退化 1：无完成条件 → 显式失败（不调模型）
    assert ToolLoop._verify(loop, TaskSpec(objective="o"), world) == (False, "无可校验条件")

    # 退化 2：无状态文本 → 失败并提示先观测
    passed, why = ToolLoop._verify(loop, spec, world)
    assert passed is False and "无当前状态" in why

    world.update({"ok": True, "state_text": "文件已保存到磁盘"}, state_text="文件已保存到磁盘")

    # 达成 / 未达成 / 判定器不可用，三种判定都来自 llm_judge（mock）
    calls = {}

    def fake_judge(cfg, system, user, timeout=0):
        calls["system"], calls["user"] = system, user
        return {"passed": True, "reason": "状态文本含已保存"}

    monkeypatch.setattr("omni_core.local.llm_judge.chat_json", fake_judge)
    passed, why = ToolLoop._verify(loop, spec, world)
    assert passed is True and "已保存" in why
    assert calls["system"] == _VERIFY_JUDGE_SYSTEM
    assert "已保存" in calls["user"] and "文件已保存到磁盘" in calls["user"]

    monkeypatch.setattr("omni_core.local.llm_judge.chat_json",
                        lambda *a, **k: {"passed": False, "reason": "未见证据"})
    passed, why = ToolLoop._verify(loop, spec, world)
    assert passed is False and "未见证据" in why

    monkeypatch.setattr("omni_core.local.llm_judge.chat_json", lambda *a, **k: None)
    passed, why = ToolLoop._verify(loop, spec, world)
    assert passed is False and "判定器不可用" in why


def test_verify_no_substring_matching(monkeypatch):
    """写死匹配的语义不再存在：状态里含条件词但判定器说未达成 → 未达成。"""
    from omni_core.local.loop.core import ToolLoop
    from omni_core.local.loop.parts import TaskSpec
    from omni_core.local.world_model import WorldModel

    loop = _bare_loop()
    world = WorldModel()
    world.update({"ok": True, "state_text": "保存失败：磁盘已满"}, state_text="保存失败：磁盘已满")
    monkeypatch.setattr("omni_core.local.llm_judge.chat_json",
                        lambda *a, **k: {"passed": False, "reason": "保存失败"})
    passed, why = ToolLoop._verify(
        loop, TaskSpec(objective="o", done_when="已保存|保存完成"), world)
    assert passed is False  # 旧子串匹配会因"保存失败"含"保存"而误判达成


# === ⑤ capability registry =====================================================
def test_capability_registry_semantics():
    assert capability.get_capability("never_registered_xyz") is None  # 未注册 → None 不抛
    capability.register_capability("t_cap", lambda: 1)
    try:
        assert callable(capability.get_capability("t_cap"))
    finally:
        capability.unregister_capability("t_cap")
    assert capability.get_capability("t_cap") is None


def test_ocr_plugin_import_does_not_load_ort():
    """OCR 插件模块可安全 import 且不触发 ORT（懒加载到首次识别）。"""
    before = set(sys.modules)
    import plugins.ocr.plugin  # noqa: F401

    assert "utils.ocr" not in sys.modules or "utils.ocr" in before  # 不新增 ORT 引入


# === ⑥ 视觉主链路：压缩 + 一次性注入 ==========================================
def test_image_to_data_url_compresses_to_vision_friendly_size(tmp_path):
    from utils.image_utils import image_to_data_url

    p = tmp_path / "big.png"
    Image.new("RGB", (3120, 2160), (10, 120, 30)).save(p)
    url = image_to_data_url(str(p))
    assert url.startswith("data:image/jpeg;base64,")
    img = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
    assert max(img.size) <= 1568
    assert len(url) < 400_000, "压缩后 data_url 不应是数 MB 级"


def test_attachment_channel_one_shot_injection():
    """S9：工具结果附件 → 通道 → 下一请求边界注入 user 消息，一次性（投后即清）。"""
    from omni_core.brain.sdk_loop import _HumanInjection, _HumanInjectionModel
    from omni_core.local.loop.sdk_bridge import build_user_content

    ch = _HumanInjection()
    ch.push_message({
        "role": "user",
        "content": build_user_content(
            "（最新截图已附加，可直接查看）",
            [{"type": "image", "data_url": "data:image/jpeg;base64,xxx"}],
        ),
    })
    msgs = ch.take_messages()
    assert len(msgs) == 1
    blocks = msgs[0]["content"]
    assert isinstance(blocks, list)
    assert any(b.get("type") == "input_image" for b in blocks)
    assert ch.take_messages() == []  # 一次性：取走即空，不进会话历史、不重复投递


def test_screenshot_tool_returns_attachments(tmp_path, monkeypatch):
    """host screenshot：落盘 + 压缩附件（mss 截屏打桩为写一张小图）。"""
    from plugins.host.backend import HostBackend

    shot = tmp_path / "shot.png"
    Image.new("RGB", (800, 600), (5, 5, 5)).save(shot)

    class _FakeMss:
        def __init__(self):
            self.monitors = [{"left": 0, "top": 0, "width": 800, "height": 600}]

        def shot(self, output=None):
            Image.new("RGB", (800, 600), (5, 5, 5)).save(output)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    import plugins.host.backend as hb

    monkeypatch.setattr(hb, "_MSS_STUB", None, raising=False)
    real_mss = hb.mss if hasattr(hb, "mss") else None

    class _MssFactory:
        @staticmethod
        def mss():
            return _FakeMss()

    monkeypatch.setitem(sys.modules, "mss", _MssFactory)
    be = HostBackend({})
    monkeypatch.setattr("omni_core.local.artifacts.record_artifact", lambda *a, **k: None)
    res = be.screenshot(str(shot))
    assert res["ok"] is True
    atts = res.get("attachments") or []
    assert atts and atts[0]["type"] == "image" and atts[0]["data_url"].startswith("data:image/jpeg")
