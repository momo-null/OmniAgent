"""M1 测试锁（plan personal-agent-memory §8.5 M1 行 / N-12）：

- ``context_signal`` 单点构造、run 内只读、``<current-context>`` ≤200 字符、每 run 一次；
- ``recent_topics`` 不触发额外 LLM；``circle_note.topics`` 优先、L0 词面兜底；
- 稳定段只出现一次 ``<current-context>``；空信号零注入；
- napcat 通道只给结构：场合自述不再带「群里刚才的消息」原文块（F-4）。

不拉真实模型；brain.db 落 tmp_path。
"""
import json
import time

import pytest

from omni_core import memory_brain as mb
from omni_core.local.loop import context_signal as cs
from omni_core.local.loop.instructions import InstructionMixin


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    return db


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch):
    """N-12 红线：话题词面构造绝不触发 LLM——碰到即失败。"""
    def _boom(*a, **k):
        raise AssertionError("context_signal 不得触发 LLM 调用（recent_topics 词面兜底）")
    monkeypatch.setattr(mb.llm_judge, "chat_json", _boom)
    yield


@pytest.fixture(autouse=True)
def _clean_signal():
    cs._CURRENT.clear()
    yield
    cs._CURRENT.clear()


def test_build_run_reads_structure_and_peer(brain, monkeypatch):
    """结构来自通道句柄 / gateway 上下文；peer 沿用 extract_peer 口径。"""
    from gateway.channels import context as ch

    class _Reply:
        chat_id = "group:42"

    monkeypatch.setattr(ch, "_CURRENT_REPLY", _Reply(), raising=False)
    monkeypatch.setattr(ch, "_SOURCE_KIND", "chat", raising=False)
    sig = cs.build_run(task_id="t1", objective="「张三」(10086) @ 了你：在吗", run_key="rk_x")
    assert sig["chat_id"] == "group:42"
    assert sig["source_kind"] == "chat"
    assert sig["peer"] == "张三(10086)"
    assert sig["run_id"] == "t1:rk_x"
    assert cs.run_id() == "t1:rk_x"
    # 无句柄（cron）→ task
    monkeypatch.setattr(ch, "_CURRENT_REPLY", None, raising=False)
    monkeypatch.setattr(ch, "_SOURCE_KIND", "", raising=False)
    sig2 = cs.build_run(task_id="t2", objective="跑个任务", run_key="rk_y")
    assert sig2["source_kind"] == "task" and sig2["chat_id"] == ""
    assert cs.run_id() == "t2:rk_y"


def test_render_budget_and_shape(brain, monkeypatch):
    cs._CURRENT.update({"chat_id": "group:7", "source_kind": "chat",
                        "peer": "成员甲(9999)", "project_id": "qq",
                        "recent_topics": ["白米饭", "剑网3", "抬杠", "复读机"],
                        "run_id": "t:rk"})
    block = cs.render()
    assert block.startswith("<current-context>") and block.endswith("</current-context>")
    assert len(block) <= 200                                   # ≤200 字符（N-12）
    assert "chat=group:7" in block and "topics=白米饭、剑网3、抬杠、复读机" in block
    # 超预算 → 逐级丢 topics 而不是硬塞
    monkeypatch.setattr(mb, "get_circle_note",
                        lambda chat_id: {"topics": ["话题" + str(i) for i in range(4)]})
    cs._CURRENT["peer"] = "这是一个非常非常长的名片名字啊(1234567890)"
    block2 = cs.render(max_chars=60)
    assert len(block2) <= 60
    # 空信号 → 零注入
    cs._CURRENT.clear()
    assert cs.render() == ""


def test_recent_topics_no_llm_circle_note_first(brain, monkeypatch):
    """circle_note.topics 优先；缺笔记时走 L0 词面兜底且零 LLM（N-12）。"""
    monkeypatch.setattr(mb, "get_circle_note",
                        lambda chat_id: {"topics": ["白米饭"]})
    assert cs._recent_topics("group:1", "") == ["白米饭"]
    # 兜底：写几条 L0 消息（一个高频词 + 噪声），词面来自频次、不调 LLM
    monkeypatch.setattr(mb, "get_circle_note", lambda chat_id: {})
    for i in range(5):
        mb.index_l0_message(f"s{i}", "user", f"今天白米饭吵起来了，剑网3也有人聊",
                            chat_id="group:9", recorded_at=f"2026-11-05T0{i}:00:00+00:00")
    topics = cs._recent_topics("group:9", "")
    assert "白米饭" in topics or "米饭" in topics
    assert all(len(t) <= 12 for t in topics) and len(topics) <= 4


def test_stable_block_injects_context_once(brain):
    """稳定段含 <current-context> 且只出现一次（M1 验收：每 run 只注入一次）。"""
    cs._CURRENT.update({"chat_id": "group:7", "source_kind": "chat",
                        "peer": "甲(1)", "project_id": "qq",
                        "recent_topics": ["白米饭"], "run_id": "t:rk"})

    class _FakeLoop(InstructionMixin):
        def __init__(self):
            self.knowledge_cfg = {"memory": True}

    block, layers = _FakeLoop()._memory_stable_block(False, task_id="t1")
    assert block.count("<current-context>") == 1
    assert block.count("</current-context>") == 1
    assert "context_signal" in layers
    # 子任务零注入
    block_sub, layers_sub = _FakeLoop()._memory_stable_block(True, task_id="t1")
    assert block_sub == "" and layers_sub == []


def test_napcat_group_objective_has_no_verbatim_history(monkeypatch):
    """F-4：通道只给结构——被 @ 的场合自述不再带「群里刚才的消息」原文块；
    「刚才聊了什么」由模型自调 msg_group_history（工具仍在）。"""
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    from gateway.channels.napcat import NapcatAdapter

    a = NapcatAdapter({"enabled": True,
                       "group_presence": {"enabled_groups": ["333"], "timers": False}})
    # 群历史有货（旧实现会把它整块塞进场合自述）
    monkeypatch.setattr(a, "group_history",
                        lambda gid, n=8: [{"who": "群友", "text": "刚才有人说了一长串话",
                                           "message_id": "1"}])
    monkeypatch.setattr(a, "group_name", lambda gid: "测试群")
    got = []
    ev = {"post_type": "message", "message_type": "group", "self_id": 111,
          "user_id": 222, "group_id": 333, "message_id": 900, "time": time.time(),
          "sender": {"nickname": "张三"},
          "message": [{"type": "at", "data": {"qq": 111}},
                      {"type": "text", "data": {"text": "在吗"}}]}
    a._handle_event(ev, got.append)
    assert got
    text = got[0].text
    assert "群里刚才的消息" not in text
    assert not hasattr(NapcatAdapter, "_group_context_block")   # 物理删除，零痕迹
    # 机制性内容保留（发送规则/身份锚属结构，不属于记忆内容）
    assert "msg_send_text" in text


def test_napcat_no_relation_card_injection(monkeypatch):
    """NP-2（D-2 / N-3）：关系卡自动注入物理删除——群聊与私聊场合自述都无印象卡正文；
    「因为记得这个人所以说话不同」改由 narrative_search 按需自取。"""
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    from gateway.channels.napcat import NapcatAdapter
    from omni_core.local import runtime_paths as rp

    a = NapcatAdapter({"enabled": True,
                       "group_presence": {"enabled_groups": ["333"], "timers": False}})
    monkeypatch.setattr(a, "group_name", lambda gid: "测试群")
    monkeypatch.setattr(a, "_member_names", lambda gid: {})
    # 该 uid 的关系卡在场（旧实现会整块注入）
    rel = rp.global_relations()
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "999999.md").write_text(
        "-----META-START-----\nname: 成员甲\nuid: 999999\nupdated: x\n"
        "-----META-END-----\n最近交手：他总拿大肥鱼起哄。", encoding="utf-8")
    got = []
    ev = {"post_type": "message", "message_type": "group", "self_id": 111,
          "user_id": 999999, "group_id": 333, "message_id": 901, "time": time.time(),
          "sender": {"card": "成员甲"},
          "message": [{"type": "at", "data": {"qq": 111}},
                      {"type": "text", "data": {"text": "在吗"}}]}
    a._handle_event(ev, got.append)
    assert got
    assert "关于这个人的印象" not in got[0].text
    assert "起哄" not in got[0].text            # 印象卡正文零外泄（N-3）
    assert not hasattr(NapcatAdapter, "_peer_note")   # 物理删除，零痕迹
    # 私聊同口径
    got2 = []
    ev2 = {"post_type": "message", "message_type": "private", "self_id": 111,
           "user_id": 999999, "message_id": 902, "time": time.time(),
           "sender": {"card": "成员甲"},
           "message": [{"type": "text", "data": {"text": "单独聊聊"}}]}
    a._handle_event(ev2, got2.append)
    assert got2 and "关于这个人的印象" not in got2[0].text
