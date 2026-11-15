"""思考开关 + 标签残片消毒（2026-11-15 事故族）单测。

三层防线契约：
- 推导层：ToolLoop 按 mode 显式下发 reasoning_effort（chat=none / task-auto=high），
  仅对声明 supports_reasoning_effort 的 provider 生效；
- 消毒层：strip_tag_junk / is_pure_tag_fragment（brain/sanitize.py 单一实现），
  输入侧 / 记录侧共用；
- 收尾层：残片签名拦截 + 纠正重试（_plain_finish_judge，chat 也覆盖），
  见 test_memory_alignment / test_mode_lane 既有用例。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core.brain.sanitize import (  # noqa: E402
    strip_tag_junk, is_pure_tag_fragment, SANITIZE_STATS,
)
from omni_core.brain import router as model_router  # noqa: E402


# === ① 剥壳规则（生产实测形态逐一覆盖） =====================================

def test_strip_empty_thinking_wrappers():
    """空包裹（单对/连排）剥净，正文保留——life 56% 污染块的主形态。"""
    assert strip_tag_junk("<thinking> </thinking>我给你做了一个身体") == "我给你做了一个身体"
    assert strip_tag_junk("<thinking> </thinking>\n<thinking> </thinking>\n\n出现在最前面") == "出现在最前面"
    assert strip_tag_junk("正文原样保留") == "正文原样保留"


def test_strip_leading_snake_tag_keeps_html():
    """前导 snake_case 标签剥壳取正文；HTML 标签（无下划线）零误伤。"""
    assert strip_tag_junk("<memory_recall > 正文") == "正文"
    assert strip_tag_junk('<memory_recall">') == ""
    # 代码回复零误伤
    assert strip_tag_junk('<div class="x">hi</div>') == '<div class="x">hi</div>'
    assert strip_tag_junk("<div>hi") == "<div>hi"


def test_is_pure_tag_fragment_signatures():
    """残片签名：剥完为空 / 整体畸形开标签；正常回复不命中。"""
    assert is_pure_tag_fragment('<memory_recall">') is True
    assert is_pure_tag_fragment("<thinking> </thinking>") is True
    assert is_pure_tag_fragment("<butong>") is True
    assert is_pure_tag_fragment("<butong>\n有正文") is False     # 剥壳留正文，非纯残片
    assert is_pure_tag_fragment("正常回复") is False
    assert is_pure_tag_fragment("<3 但这不是标签") is False
    assert is_pure_tag_fragment("") is False


def test_strip_idempotent_and_safe():
    """幂等；多包裹嵌套收敛；干净文本逐字节不变。"""
    s = "<a> </a>嵌套<b> </b>正文"
    assert strip_tag_junk(strip_tag_junk(s)) == strip_tag_junk(s)
    assert strip_tag_junk("<x> </x><y> </y>正文") == "正文"
    plain = "「测试昵称」(10001): [图片]我给你做了一个身体"
    assert strip_tag_junk(plain) == plain.strip()


# === ② 思考开关推导（ToolLoop 单点，Web/网关两链共用） ======================

def _catalog_with_flag(flag: bool):
    return {
        "providers": {"testprov": {"label": "t", "base_url": "http://127.0.0.1:9",
                                   "api_key": "test",
                                   "supports_reasoning_effort": flag,
                                   "models": [{"id": "m1", "label": "m1", "vision": True}]}},
        "defaults": {"main": "testprov/m1"},
    }


def test_router_flag_propagates(monkeypatch):
    """provider 旗标 → 端点 dict：声明才透传，OpenAI 官方等严格端点默认不下发。"""
    monkeypatch.setattr(model_router, "_catalog", lambda: _catalog_with_flag(True))
    ep = model_router.resolve_slot("main")
    assert ep.get("supports_reasoning_effort") is True
    monkeypatch.setattr(model_router, "_catalog", lambda: _catalog_with_flag(False))
    ep = model_router.resolve_slot("main")
    assert ep.get("supports_reasoning_effort") is False


def test_thinking_effort_derived_from_mode(monkeypatch):
    """ToolLoop 单点推导：chat=none（对话轮显式关）/ auto-task=high（任务恒有思考块）。"""
    from tests._env import install_fake_env

    class _B:
        def text_of(self, percept):
            return ""

    install_fake_env(monkeypatch, _B)
    from omni_core.local.loop import ToolLoop

    monkeypatch.setattr(model_router, "_catalog", lambda: _catalog_with_flag(True))
    cfg = model_router.build_brain_cfg(engine_src={"reasoning_mode": "native"})
    assert cfg.get("supports_reasoning_effort") is True

    loop_chat = ToolLoop(dict(cfg), mode="chat", verbose=False)
    assert loop_chat.brain_cfg["request"]["reasoning_effort"] == "none"
    loop_auto = ToolLoop(dict(cfg), mode="auto", verbose=False)
    assert loop_auto.brain_cfg["request"]["reasoning_effort"] == "high"
    loop_task = ToolLoop(dict(cfg), mode="task", verbose=False)
    assert loop_task.brain_cfg["request"]["reasoning_effort"] == "high"


def test_thinking_effort_absent_without_flag(monkeypatch):
    """未声明旗标的 provider 不下发参数——严格端点（OpenAI 官方）不会 400。"""
    from tests._env import install_fake_env

    class _B:
        def text_of(self, percept):
            return ""

    install_fake_env(monkeypatch, _B)
    from omni_core.local.loop import ToolLoop

    monkeypatch.setattr(model_router, "_catalog", lambda: _catalog_with_flag(False))
    cfg = model_router.build_brain_cfg(engine_src={})
    loop = ToolLoop(dict(cfg), mode="auto", verbose=False)
    assert "reasoning_effort" not in (loop.brain_cfg.get("request") or {})


def test_build_brain_cfg_unifies_web_and_gateway(monkeypatch):
    """装配收口：Web _make_brain_cfg 与网关 build_brain_cfg 同一构造，等价产出。"""
    import config as cfg_mod
    from backend.api.routers.helpers import _make_brain_cfg

    monkeypatch.setattr(cfg_mod, "load_config", lambda: {"brain": {"reasoning_mode": "native"}})
    monkeypatch.setattr(model_router, "_catalog", lambda: _catalog_with_flag(True))

    web_cfg, _exec = _make_brain_cfg()
    gw_cfg = model_router.build_brain_cfg(model=None,
                                          engine_src={"reasoning_mode": "native"})
    for k in ("base_url", "model", "reasoning_mode", "supports_reasoning_effort"):
        assert web_cfg.get(k) == gw_cfg.get(k), f"两条派发链装配漂移: {k}"
    # 显式选择仍走白名单校验（非法选择被忽略）
    web_sel, _ = _make_brain_cfg(model="testprov/m1")
    assert web_sel.get("model") == "m1"
