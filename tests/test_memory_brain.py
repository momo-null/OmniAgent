"""brain.db（LTM 单脑）单测：schema/两段判/状态机/融合排序/persona 过滤。

假 LLM 按调用顺序消费脚本队列（extract → value → [consistency]）；
brain 库落在 tmp_path，不碰真实数据；巩固调度桩掉（后台线程会抢脚本）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain  # noqa: E402


class _FakeJudge:
    """按调用顺序弹出脚本的假 LLM。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_json(self, brain_cfg, system, user, timeout=60.0):
        self.calls.append(system[:12])
        if not self.script:
            raise AssertionError(f"脚本耗尽: {system[:30]}")
        return self.script.pop(0)


def _extract(text):
    return {"facts": [{"text": text, "scope": "global"}]}


def _value(kind="fact", importance=0.8):
    return {"items": [{"idx": 0, "keep": True, "kind": kind, "importance": importance}]}


def _decide(action, target_id=None, merged=None):
    d = {"idx": 0, "action": action, "target_id": target_id}
    if merged:
        d["merged_content"] = merged
    return {"decisions": [d]}


@pytest.fixture
def brain(tmp_path, monkeypatch):
    """隔离 brain 库 + 启用开关 + 假 LLM 脚本队列。"""
    db = tmp_path / "brain.db"
    monkeypatch.setattr(memory_brain, "_BRAIN_PATH", str(db))
    # 巩固 worker 会把库备份进 _ARCHIVE_DIR——不隔离就会往真实数据目录写垃圾
    monkeypatch.setattr(memory_brain, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    # 隔离环境下 runtime.knowledge.memory.enabled 不存在 → capture 会空转，这里强制开
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    fake = _FakeJudge([])
    monkeypatch.setattr(memory_brain.llm_judge, "chat_json", fake.chat_json)
    # L2 维护是外部副作用（另写文件/开 LLM），单测桩掉；场景层有独立测试
    from omni_core.local import scene_executor
    monkeypatch.setattr(scene_executor, "maybe_maintain", lambda *a, **k: None)
    # 巩固在后台线程跑，会抢占本测试的假 LLM 脚本 → 桩掉线程体（保留派发闸语义）
    def _noop_run(cfg, persona):
        with memory_brain._dispatch_lock:
            memory_brain._dispatch_state["running"] = False

    monkeypatch.setattr(memory_brain, "_run_consolidation", _noop_run)
    # persona 固定，避免依赖真实 config
    monkeypatch.setattr(memory_brain, "_cfg",
                        lambda key, default=None: {"persona": "tangtang"}.get(
                            key, default if default is not None
                            else memory_brain._LTM_DEFAULTS.get(key)))
    return fake


def test_flush_writes_novel_atom_with_value_fields(brain):
    brain.script = [_extract("用户偏好先聊后动的工作方式"), _value("preference", 0.8)]
    memory_brain.capture("p_test", "user", "以后改我代码前先跟我说方案")
    memory_brain.capture("p_test", "assistant", "好的记住了")
    stats = memory_brain.flush_brain("p_test", {"base_url": "http://x", "api_key": "k"},
                                     source_task="t1", persona="tangtang")
    assert stats["extracted"] == 1 and stats["stored"] == 1
    conn = memory_brain.open_brain()
    row = conn.execute("SELECT content, kind, importance, persona, origin, state "
                       "FROM atoms").fetchone()
    conn.close()
    assert "先聊后动" in row[0]
    assert row[1] == "preference" and abs(row[2] - 0.8) < 1e-6
    assert row[3] == "tangtang" and row[4] == "p_test" and row[5] == "active"


def test_duplicate_strengthens_instead_of_new_row(brain):
    brain.script = [_extract("用户偏好先聊后动的工作方式"), _value("preference", 0.8)]
    memory_brain.capture("p", "user", "以后改我代码前先跟我说方案")
    memory_brain.capture("p", "assistant", "好")
    memory_brain.flush_brain("p", {"base_url": "http://x"}, persona="tangtang")
    conn = memory_brain.open_brain()
    rid = conn.execute("SELECT record_id FROM atoms").fetchone()[0]
    acc0 = conn.execute("SELECT access_count FROM atoms").fetchone()[0]
    conn.close()
    # 第二轮：同一事实 → 判 duplicate → 旧条强化而非新增
    brain.script = [_extract("用户偏好先聊后动的工作方式"), _value("preference", 0.8),
                    _decide("duplicate", target_id=rid)]
    memory_brain.capture("p", "user", "再次强调：以后改我代码前先跟我说方案")
    memory_brain.capture("p", "assistant", "嗯")
    stats = memory_brain.flush_brain("p", {"base_url": "http://x"}, persona="tangtang")
    assert stats["duplicated"] == 1
    conn = memory_brain.open_brain()
    n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    acc1 = conn.execute("SELECT access_count FROM atoms WHERE record_id=?",
                        (rid,)).fetchone()[0]
    conn.close()
    assert n == 1
    assert acc1 == acc0 + 1


def test_conflict_tombstones_old_row(brain):
    brain.script = [_extract("用户所在地是广州"), _value("fact", 0.7)]
    memory_brain.capture("p", "user", "用户所在地是广州")
    memory_brain.capture("p", "assistant", "好的")
    memory_brain.flush_brain("p", {"base_url": "http://x"}, persona="tangtang")
    conn = memory_brain.open_brain()
    rid = conn.execute("SELECT record_id FROM atoms").fetchone()[0]
    conn.close()
    brain.script = [_extract("用户所在地是上海"), _value("fact", 0.8),
                    _decide("conflict", target_id=rid)]
    memory_brain.capture("p", "user", "我搬家了，现在所在地是上海")
    memory_brain.capture("p", "assistant", "记下了")
    memory_brain.flush_brain("p", {"base_url": "http://x"}, persona="tangtang")
    conn = memory_brain.open_brain()
    states = dict(conn.execute("SELECT record_id, state FROM atoms"))
    conn.close()
    assert states[rid] == "tombstone"
    assert sum(1 for v in states.values() if v == "active") == 1


def test_search_persona_filter_and_reinforce(brain):
    rid = memory_brain.remember("糖糖的新闻简报来自四个来源", kind="fact",
                                importance=0.8, persona="tangtang")
    memory_brain.remember("中性记忆：项目在 D 盘", kind="fact", importance=0.6, persona="")
    hits = memory_brain.search_brain("新闻简报来源", persona="tangtang")
    assert len(hits) == 1 and hits[0]["record_id"] == rid
    conn = memory_brain.open_brain()
    acc = conn.execute("SELECT access_count FROM atoms WHERE record_id=?",
                       (rid,)).fetchone()[0]
    conn.close()
    assert acc == 1  # 召回即强化


def test_update_and_forget_tombstone(brain):
    rid = memory_brain.remember("旧的所在地是广州", kind="fact", importance=0.7)
    new_rid = memory_brain.update_memory(rid, "新的所在地是上海")
    assert new_rid
    assert memory_brain.forget(new_rid, reason="用户要求")
    conn = memory_brain.open_brain()
    rows = dict(conn.execute("SELECT record_id, state FROM atoms"))
    conn.close()
    assert rows[rid] == "tombstone" and rows[new_rid] == "tombstone"
    assert memory_brain.search_brain("所在地", persona="tangtang") == []


def test_low_importance_exact_match_survives_floor(brain):
    """相关性按排名归一：importance 低的精确命中不再被 inject_floor 误杀。"""
    rid = memory_brain.remember("便签只记在手机备忘录里", kind="fact", importance=0.2)
    hits = memory_brain.search_brain("便签 备忘录", persona="tangtang", reinforce=False)
    assert [h["record_id"] for h in hits] == [rid]
    assert hits[0]["score"] >= 0.30


def test_consolidation_due_reads_atom_count(brain):
    """巩固触发：空库不触发，有原子即触发（回归：conn 先 close 后 execute 曾恒 False）。"""
    assert memory_brain._consolidation_due() is False
    memory_brain.remember("用户偏好把待办写在便签上", kind="fact", importance=0.8)
    assert memory_brain._consolidation_due() is True


def test_trigram_two_char_query_falls_back_to_like(brain, monkeypatch):
    """trigram 下纯 2 字 query 无 MATCH 也必须走 LIKE 兜底，且不越过 persona 隔离。"""
    monkeypatch.setattr(memory_brain, "_fts_tokenizer", lambda: "trigram")
    rid = memory_brain.remember("用户偏好把待办写在便签上", kind="fact", importance=0.8,
                                persona="tangtang")
    other = memory_brain.remember("别人的便签习惯", kind="fact", importance=0.8,
                                  persona="other")
    ids = [h["record_id"] for h in
           memory_brain.search_brain("便签", persona="tangtang", reinforce=False)]
    assert rid in ids and other not in ids


def test_consolidation_worker_runs_and_rescores(brain):
    """巩固 worker 全链路（备份→重算→写 meta），确保触发后真的能跑完不留 last_consolidation 空。"""
    rid = memory_brain.remember("用户偏好把待办写在便签上", kind="fact", importance=0.8)
    memory_brain._consolidation_worker({"base_url": "http://x"}, "tangtang")
    conn = memory_brain.open_brain()
    last = conn.execute("SELECT v FROM meta WHERE k='last_consolidation'").fetchone()
    sal = conn.execute("SELECT salience FROM atoms WHERE record_id=?", (rid,)).fetchone()[0]
    stats = conn.execute("SELECT v FROM meta WHERE k='last_consolidation_stats'").fetchone()
    conn.close()
    assert last and last[0]
    assert "error" not in (stats[0] if stats else "")
    assert 0.0 < float(sal) <= 1.0


def test_consolidation_dispatch_not_deadlocked(brain, monkeypatch):
    """派发不能先占 worker 的锁：同一把非可重入锁被双重 acquire 会让巩固永卡死。"""
    import time as _t
    calls = []

    def fake_worker(cfg, persona):
        calls.append(1)

    def fake_run(cfg, persona):
        try:
            memory_brain._consolidation_worker(cfg, persona)
        finally:
            with memory_brain._dispatch_lock:
                memory_brain._dispatch_state["running"] = False

    # fixture 把线程体桩成 noop，这里换回「真实派发 + 假 worker」链路
    monkeypatch.setattr(memory_brain, "_run_consolidation", fake_run)
    monkeypatch.setattr(memory_brain, "_consolidation_worker", fake_worker)
    memory_brain.remember("用户偏好把待办写在便签上", kind="fact", importance=0.8)
    memory_brain.maybe_consolidate({"base_url": "http://x"}, "tangtang")
    for _ in range(60):
        if calls:
            break
        _t.sleep(0.05)
    assert calls == [1], "worker 必须真的被派发执行（不是被锁卡死）"
    assert memory_brain._consolidation_lock.locked() is False
    assert memory_brain._dispatch_state["running"] is False


def test_disabled_brain_is_noop(brain, monkeypatch):
    monkeypatch.setattr(memory_brain, "enabled", lambda: False)
    memory_brain.capture("p", "user", "hello world 测试内容")
    memory_brain.capture("p", "assistant", "ok")
    stats = memory_brain.flush_brain("p", {"base_url": "http://x"})
    assert stats["extracted"] == 0
    assert memory_brain.remember("x") == ""
