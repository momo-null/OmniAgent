"""M3 测试锁（plan personal-agent-memory §8.5 M3 行 / N-13）：

- circle_note 不产生 atoms、不读叙事正文（INV-5 / INV-10）；
- 每场合每巩固周期 reflection ≤1 次；全局单轮 ≤5 次（N-13）；
- buffer 只存聚合信号，不存他人原话（INV-6）；
- 重写不追加、保留上一版；失败保留旧版（§5.6）；
- <current-context> 带 tone 字段（§6.4）。

不拉真实模型（假 LLM 脚本队列）；brain.db 落 tmp_path。
"""
import json

import pytest

from omni_core import memory_brain as mb
from omni_core.local.loop import context_signal as cs


class _FakeJudge:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_json(self, brain_cfg, system, user, timeout=60.0):
        self.calls.append((system, user))
        if not self.script:
            raise AssertionError(f"脚本耗尽: {system[:40]}")
        return self.script.pop(0)


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(mb, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "maybe_consolidate", lambda *a, **k: None)
    from omni_core.local import scene_executor
    monkeypatch.setattr(scene_executor, "maybe_maintain", lambda *a, **k: None)
    fake = _FakeJudge([])
    monkeypatch.setattr(mb.llm_judge, "chat_json", fake.chat_json)
    yield fake
    cs._CURRENT.clear()


BRAIN_CFG = {"base_url": "http://x", "model": "m"}


def test_buffer_stores_signals_not_verbatim(brain):
    """INV-6：buffer 只存聚合信号（词面/计数），不存他人原话。"""
    texts = ["今天群里为白米饭吵了一整个上午，下午又聊起剑网3"] * 6
    buf = mb.update_context_buffer("group:1", texts, source_kind="chat")
    assert buf and buf["msg_count"] == 6
    raw = json.dumps(buf, ensure_ascii=False)
    assert "吵了一整个上午" not in raw          # 原话不进 buffer（只有词面）
    assert buf["topics"]                        # 词面在场
    assert buf["source_kinds"] == ["chat"]      # 来源标注（INV-5）


def test_reflection_writes_note_no_atoms(brain):
    """reflection 产物只进 circle_note，绝不产生 atoms（INV-5 / N-13）。"""
    mb.update_context_buffer("group:1", ["白米饭" * 3] * 20)
    brain.script = [{"summary": "这个群里常为白米饭起哄", "topics": ["白米饭", "起哄"],
                     "tone": "起哄为主，别太当真"}]
    assert mb.reflect_circle_note(BRAIN_CFG, "group:1") is True
    note = mb.get_circle_note("group:1")
    assert note["summary"].startswith("这个群里常为白米饭起哄")
    assert note["topics"] == ["白米饭", "起哄"] and note["tone"]
    assert note["version"] == 1 and note["source_count"] == 20
    # 没有 atoms 被写
    conn = mb.open_brain()
    try:
        n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    finally:
        conn.close()
    assert n == 0
    # 重写保留上一版（version 递增、prev_summary 留底）
    brain.script = [{"summary": "第二版归纳", "topics": ["白米饭"], "tone": ""}]
    assert mb.reflect_circle_note(BRAIN_CFG, "group:1") is True
    note2 = mb.get_circle_note("group:1")
    assert note2["version"] == 2 and note2["prev_summary"].startswith("这个群里常为白米饭起哄")


def test_reflection_failure_keeps_previous(brain):
    """§5.6：reflection 失败保留上一版，静默不阻断。"""
    mb.update_context_buffer("group:2", ["素材素材素材"] * 20)
    brain.script = [{"summary": "第一版", "topics": ["素材"], "tone": ""}]
    assert mb.reflect_circle_note(BRAIN_CFG, "group:2") is True
    brain.script = [None]                        # chat_json 返回 None（失败）
    assert mb.reflect_circle_note(BRAIN_CFG, "group:2") is False
    note = mb.get_circle_note("group:2")
    assert note["summary"] == "第一版" and note["version"] == 1


def test_due_thresholds_and_once_per_cycle(brain):
    """触发判定：条数阈值 / 间隔阈值；每巩固周期 ≤1 次（N-13）。"""
    buf = mb.update_context_buffer("group:3", ["词面"] * 11)   # 11 < 12 → 未到条数
    assert mb._circle_note_due("group:3", buf) is False
    buf = mb.update_context_buffer("group:3", ["词面"])          # 累计 12 → 到条数
    assert mb._circle_note_due("group:3", buf) is True
    # 反思一次后，未过巩固周期边界 → 不再触发（周期内 ≤1 次）
    brain.script = [{"summary": "v1", "topics": ["词面"], "tone": ""}]
    assert mb.reflect_circle_note(BRAIN_CFG, "group:3") is True
    # buffer msg_count 清零 → 条数不触发；间隔路径被「周期内已反思」挡住
    buf2 = mb.update_context_buffer("group:3", ["词面"] * 20)
    assert mb._circle_note_due("group:3", buf2) is False


def test_flush_tail_accumulates_and_reflects(brain):
    """flush 尾部接线：带 chat_id 的消息按场合累积；到阈值触发一次 reflection。"""
    msgs = [{"role": "user", "content": f"群友第{i}句在聊白米饭", "chat_id": "group:9"}
            for i in range(12)]
    brain.script = [{"summary": "他们在聊白米饭", "topics": ["白米饭"], "tone": ""}]
    mb._flush_tail("p", BRAIN_CFG, {}, "", True, msgs=msgs)
    note = mb.get_circle_note("group:9")
    assert note and note["topics"] == ["白米饭"]
    assert len(brain.calls) == 1                 # 一次 reflection，一次 LLM
    # 无 chat_id 的消息不触发任何 LLM（Web/任务输入缺场合键 → 语境链静默跳过）
    n_calls = len(brain.calls)
    mb._flush_tail("p", BRAIN_CFG, {}, "", True,
                   msgs=[{"role": "user", "content": "没有场合键的一轮"}])
    assert len(brain.calls) == n_calls


def test_coldstart_and_global_budget(brain, monkeypatch):
    """巩固期冷启动：无 note 的老场合各跑首版；全局 ≤5 次/周期（N-13）。"""
    for i in range(7):
        cid = f"group:{i}"
        for j in range(15):
            mb.index_l0_message(f"s{i}_{j}", "user", f"第{i}个群的白米饭第{j}句",
                                chat_id=cid, recorded_at=f"2026-11-05T0{j % 9}:00:00+00:00")
    monkeypatch.setattr(mb, "_circle_cfg",
                        lambda key, default=None: {"max_per_cycle": 3}.get(key, default))
    for i in range(3):
        brain.script.append({"summary": f"第{i}个群首版", "topics": ["白米饭"], "tone": ""})
    n = mb.run_context_reflections(BRAIN_CFG, "", coldstart=True)
    assert n == 3                                # 7 个候选场合，预算 3 → 只跑 3 个
    have = []
    for i in range(7):
        if mb.get_circle_note(f"group:{i}"):
            have.append(i)
    assert len(have) == 3


def test_context_signal_includes_tone(brain):
    """§6.4：circle_note 只影响 <current-context> 的话题/语气字段。"""
    mb.update_context_buffer("group:5", ["词面"] * 20)
    brain.script = [{"summary": "氛围归纳", "topics": ["词面"], "tone": "互相起哄的分寸"}]
    assert mb.reflect_circle_note(BRAIN_CFG, "group:5") is True
    cs.build_run(task_id="", objective="", run_key="", source_kind="chat")
    cs._CURRENT["chat_id"] = "group:5"
    cs._CURRENT["tone"] = cs._circle_tone("group:5")
    block = cs.render()
    assert "tone=互相起哄的分寸" in block
    assert len(block) <= 200
    # 语境摘要原文（summary）不进渲染——只有 tone/topics 字段（INV-5）
    assert "氛围归纳" not in block
