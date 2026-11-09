"""memory_observe 工具：观察进料道的 agent 写入面（agent→工具→内核代理）。

红线形态（memory-architecture §2.6）：她的见闻经工具进 capture_observation 统一入口，
不经脚本直写（2026-11-10 起 life 的 publish_diary.py 因此退役）；出处必填（H-7），
空出处不入库。
"""
import pytest

from omni_core.tools import memory_tool as mt


@pytest.fixture()
def observe_env(monkeypatch):
    from omni_core import memory_brain as mb

    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mt, "_current_conversation", lambda: ("life:test", ""))
    captured: dict = {}

    def _fake_capture(context_key, text, provenance=None,
                      observed_at="", source_kind="observation"):
        captured.update({"context_key": context_key, "text": text,
                         "provenance": provenance, "source_kind": source_kind})
        return "m_abc123"

    monkeypatch.setattr(mb, "capture_observation", _fake_capture)
    return captured


def test_observe_routes_to_capture_lane(observe_env):
    r = mt.memory_observe(text="看到一篇讲 JSK 保值率的帖子", source="小红书 某帖 748 赞")
    assert r["ok"] and r["record_id"] == "m_abc123"
    c = observe_env
    assert c["context_key"] == "life:test"
    assert "JSK" in c["text"]
    assert c["source_kind"] == "observation"
    assert c["provenance"]["surface"] == "observe"
    assert c["provenance"]["source"].startswith("小红书")


def test_observe_requires_source(observe_env):
    """H-7：缺出处不入库——工具在边界就拒，不触达内核。"""
    r = mt.memory_observe(text="一条见闻", source="")
    assert not r["ok"] and "source" in r["error"]
    assert not observe_env


def test_observe_requires_text(observe_env):
    r = mt.memory_observe(text="   ", source="x")
    assert not r["ok"] and "text" in r["error"]
    assert not observe_env


def test_observe_respects_memory_switch(monkeypatch):
    from omni_core import memory_brain as mb

    monkeypatch.setattr(mb, "enabled", lambda: False)
    r = mt.memory_observe(text="x", source="y")
    assert not r["ok"] and "未启用" in r["error"]
