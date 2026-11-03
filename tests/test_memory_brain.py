"""brain.db（LTM 单脑）单测：schema/两段判/状态机/融合排序/persona 过滤。

假 LLM 按调用顺序消费脚本队列（extract → value → [consistency]）；
brain 库落在 tmp_path，不碰真实数据；巩固调度桩掉（后台线程会抢脚本）。
"""
import os
import sys

import json
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain  # noqa: E402


class _FakeJudge:
    """按调用顺序弹出脚本的假 LLM。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_json(self, brain_cfg, system, user, timeout=60.0):
        self.calls.append(f"{system}\n{user}")     # 拼接记录：断言可同时查 system 锚与 user 素材
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


def test_blank_extraction_still_counts_flush(brain):
    """白卷轮也要计入 flush 计数并尝试巩固，否则闲聊为主的会话永远攒不到巩固。"""
    brain.script = [{"facts": []}]
    memory_brain.capture("p", "user", "你好啊")
    memory_brain.capture("p", "assistant", "在的")
    stats = memory_brain.flush_brain("p", {"base_url": "http://x"}, persona="tangtang")
    assert stats["extracted"] == 0
    conn = memory_brain.open_brain()
    cnt = conn.execute("SELECT v FROM meta WHERE k='flush_count'").fetchone()
    conn.close()
    assert cnt and int(cnt[0]) == 1


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


def test_overlong_merge_is_rejected_not_truncated(brain):
    """merge 结果超线 → 旧条不动、新条独立入库（stats.merge_rejected），**不截成半句话**。

    成因见 ``_MAX_ATOM_CHARS`` 注释：静默截断把每次合并的尾巴砍掉，同一条 rule
    于是越并越宽（2026-11-03 实测最宽一条 117 字，仍在往 200 线走）。
    """
    brain.script = [_extract("改我代码前先给方案"), _value("rule", 0.9)]
    memory_brain.capture("p", "user", "改我代码前先给方案")
    memory_brain.capture("p", "assistant", "好")
    memory_brain.flush_brain("p", {"base_url": "http://x"}, persona="tangtang")
    conn = memory_brain.open_brain()
    rid, old = conn.execute("SELECT record_id, content FROM atoms").fetchone()
    conn.close()
    brain.script = [_extract("改我代码前先给方案，并且要等我点头"), _value("rule", 0.9),
                    _decide("merge", target_id=rid, merged="约" * 250)]
    memory_brain.capture("p", "user", "再补一句：改我代码前先给方案，并且要等我点头")
    memory_brain.capture("p", "assistant", "嗯")
    stats = memory_brain.flush_brain("p", {"base_url": "http://x"}, persona="tangtang")
    assert stats["merged"] == 0 and stats["merge_rejected"] == 1
    conn = memory_brain.open_brain()
    rows = conn.execute("SELECT record_id, content, state FROM atoms").fetchall()
    n_fts = conn.execute("SELECT COUNT(*) FROM atoms_fts").fetchone()[0]
    conn.close()
    assert len(rows) == 2 and all(r[2] == "active" for r in rows)
    assert dict((r[0], r[1]) for r in rows)[rid] == old     # 旧条一字未动
    assert n_fts == len(rows)                               # 拒并路径同样一条一行


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
    """P0 绝对分：低 importance 的**精确**命中仍要活过 inject_floor——
    relevance 由 bm25 绝对分给出（多原子语料下量级真实），不再候选集内排名归一。"""
    rid = memory_brain.remember("便签只记在手机备忘录里", kind="fact", importance=0.2)
    for i in range(6):   # 语料太小 bm25 会退化（idf≈0），补无关条目还原真实量级
        memory_brain.remember(f"无关事实第{i}条：项目在D盘根目录", kind="fact",
                              importance=0.5)
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


# === M3 peer 归属维度 + M2.5 月记步（plan persona-social-memory-2026-10-31） ===

def test_flush_tags_peer_from_judge(brain):
    """写链打标：judge 输出 peer → atoms.peer 落库（「名片(uid)」格式）。"""
    brain.script = [
        _extract("成员甲 用语音嘲笑我"),
        {"items": [{"idx": 0, "keep": True, "kind": "episode", "importance": 0.7,
                    "peer": "成员甲(999999)"}]},
        {"decisions": []},
    ]
    memory_brain.capture("p_peer", "user", "「成员甲」(999999): 你这话根本站不住脚")
    memory_brain.capture("p_peer", "assistant", "嫌难听还点，下次不叫了")
    memory_brain.flush_brain("p_peer", {"base_url": "http://x"}, persona="tangtang")
    conn = memory_brain.open_brain()
    row = conn.execute("SELECT peer FROM atoms WHERE content LIKE '%嘲笑%'").fetchone()
    conn.close()
    assert row is not None and row[0] == "成员甲(999999)"


def test_search_peer_boost_not_filter(brain):
    conn = memory_brain.open_brain()
    now = memory_brain._now()
    memory_brain._insert_atom(conn, "rid-a", {"text": "成员甲 踩我语音", "kind": "episode",
                                              "importance": 0.6, "peer": "成员甲(999999)"},
                              "", "qq", "t", now)
    memory_brain._insert_atom(conn, "rid-b", {"text": "成员甲 踩我语音", "kind": "episode",
                                              "importance": 0.6, "peer": ""},
                              "", "qq", "t", now)
    conn.commit()
    hits = memory_brain.search_brain("成员甲 踩我语音", peer="成员甲(999999)", conn=conn)
    ids = [h["record_id"] for h in hits]
    conn.close()
    assert ids[0] == "rid-a"                                 # peer 加权排前
    assert set(ids) == {"rid-a", "rid-b"}                    # 加权不是过滤


def test_inject_prefix_and_extract_peer(brain):
    conn = memory_brain.open_brain()
    memory_brain._insert_atom(conn, "rid-p", {"text": "成员甲 爱踩我语音", "kind": "rule",
                                              "importance": 0.9, "peer": "成员甲(999999)"},
                              "", "qq", "t", memory_brain._now())
    conn.commit()
    conn.close()
    q = "「成员甲」(999999) 又踩我，怎么回"
    text, meta = memory_brain.inject_text_brain(query=q)
    assert "关于 成员甲(999999)：" in text
    assert meta["peer"] == "成员甲(999999)"                 # 从 query 自动解析
    assert memory_brain.extract_peer("先说「阿伟」(222333) 的事") == "阿伟(222333)"
    assert memory_brain.extract_peer("没有标注的普通句子") == ""


def test_consolidation_groups_by_peer(brain, monkeypatch):
    """peer 分组：同一人的 3 条 episode 抽象为 1 条 rule 且 peer 继承；跨 peer 不混账。"""
    conn = memory_brain.open_brain()
    now = memory_brain._now()
    for i in range(3):
        memory_brain._insert_atom(conn, f"ep-a{i}",
                                  {"text": f"成员甲第{i}次踩我语音", "kind": "episode",
                                   "importance": 0.6, "peer": "成员甲(999999)"},
                                  "", "qq", "t", now)
    memory_brain._insert_atom(conn, "ep-b0",
                              {"text": "成员甲第9次被我怼", "kind": "episode",
                               "importance": 0.6, "peer": ""},
                              "", "qq", "t", now)
    conn.commit()
    conn.close()

    async_steps = ("maintain_profile", "maintain_diary",
                   "maintain_month_diary", "maintain_relations")
    for fn in async_steps:
        monkeypatch.setattr(memory_brain, fn, lambda *a, **k: 0)

    # P5 后去重循环真的会发 LLM（全文 FTS 近邻有候选）→ 假 LLM 按 system 路由，
    # 不依赖调用次数与顺序
    def _router(cfg, system, user, timeout=60.0):
        if "一致性评审器" in system:
            return {"decisions": []}          # 四态判 novel：不干扰分组断言
        return {"rule": "成员甲 爱用语音踩我"}  # 抽象器

    monkeypatch.setattr(memory_brain.llm_judge, "chat_json", _router)
    memory_brain._consolidate_impl({"base_url": "http://x"}, "tangtang")
    conn = memory_brain.open_brain()
    rules = conn.execute("SELECT content, peer FROM atoms WHERE kind='rule'").fetchall()
    conn.close()
    assert any(r[1] == "成员甲(999999)" and "踩" in r[0] for r in rules)
    assert not any(r[1] == "" and "踩" in r[0] for r in rules)   # 无对象那条不混入


def _seed_sessions(sessions):
    """造会话记录（maintain_diary 的 L0 素材；_isolated_home 保证落 tmp）。"""
    from omni_core.local import runtime_paths as P
    from omni_core.local.task_store import ProjectStore

    for pid, records in sessions.items():
        ProjectStore.ensure(pid)
        f = P.project_dir(pid) / "s_test.jsonl"
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                     encoding="utf-8")


def _diary_day_iso():
    """maintain_diary 写昨日（D+1 语义）：素材与断言都用昨天的日期。"""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=1))


def test_maintain_diary_writes_and_idempotent(brain):
    day = _diary_day_iso().strftime("%Y-%m-%d")
    day_iso = _diary_day_iso().isoformat()
    _seed_sessions({"qq": [
        {"ts": day_iso, "role": "user",
         "content": "「成员甲」(999999): 你这话根本站不住脚，还是先管好你自己吧"},
        {"ts": day_iso, "role": "assistant",
         "content": "嫌难听还点，下次不叫了，反正另一条鱼的声音也就一般般"},
        {"ts": day_iso, "role": "user",
         "content": "「成员甲」(999999): 你这是偏袒呢，那以后我该怎么喊你"},
        {"ts": day_iso, "role": "assistant",
         "content": "叫错了就改呗，喊对就行，别动不动就摆脸色给人看"},
    ]})
    brain.script = [{"diary": "今天成员甲(999999)挑我毛病，我怼了回去。"}]
    assert memory_brain.maintain_diary({"base_url": "http://x"}) == 1
    from omni_core.local import scene_executor

    raw = scene_executor.read_scene_block("qq", f"diary-{day}.md")
    assert "挑我毛病" in raw          # 日记正文来自本轮 LLM 产出（不是素材原文）
    brain.script = [{"diary": "不该被写"}]
    assert memory_brain.maintain_diary({"base_url": "http://x"}) == 0   # 幂等


def test_peer_lines_group_by_uid_across_projects(monkeypatch, tmp_path):
    """关系素材按 **uid** 跨项目/跨会话合并（人是跨场景的，不分群/不分项目）。"""
    import json as _json

    proj = tmp_path / "projects"
    for pid, rows in {
        "qq": [{"ts": memory_brain._now(), "role": "user",
                "content": "「成员甲」(999999): 你这话站不住脚"},
               {"ts": memory_brain._now(), "role": "assistant", "content": "嫌弃就别叫"}],
        "wechat": [{"ts": memory_brain._now(), "role": "user",
                    "content": "「成员甲」(999999): 又来问你一遍"}],
    }.items():
        d = proj / pid
        d.mkdir(parents=True)
        (d / "s_x.jsonl").write_text(
            "\n".join(_json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")
    monkeypatch.setattr(memory_brain.rp, "projects_root", lambda: proj)

    peers = memory_brain._peer_lines(limit_days=7)
    assert "999999" in peers
    assert len(peers["999999"]["lines"]) == 2          # 两个项目的账并到一起
    assert peers["999999"]["name"] == "成员甲"


def test_maintain_relations_writes_global_card(monkeypatch, tmp_path):
    """关系卡步：按 uid 写全局卡（一份），正文三行；信息不足不写。"""
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    rel = tmp_path / "relations"
    monkeypatch.setattr(memory_brain.rp, "global_relations", lambda: rel)
    monkeypatch.setattr(memory_brain, "_peer_lines", lambda limit_days=7: {
        "999999": {"name": "成员甲", "lines": ["a", "b", "c"]},
        "888888": {"name": "太短的", "lines": ["只有一条"]},
    })
    seen = []

    def fake(cfg, system, user, timeout=0):
        seen.append(system)
        return {"card": "最近交手：连问三次\n他对我的态度：试探\n我对他的态度：警惕"}

    monkeypatch.setattr(memory_brain.llm_judge, "chat_json", fake)
    assert memory_brain.maintain_relations({"model": "m"}) == 1
    p = rel / "999999.md"
    assert p.is_file() and not (rel / "888888.md").exists()   # 往来太少不写卡
    txt = p.read_text(encoding="utf-8")
    assert "成员甲" in txt and "警惕" in txt
    assert "写你对这个人的印象" in seen[0]      # 走了关系卡 prompt（同名人消歧在 prompt 里）


def test_maintain_diary_blank_roll(brain):
    _seed_sessions({"qq": [{"ts": _diary_day_iso().isoformat(), "role": "user",
                            "content": "「张三」(7): 在吗"}]})
    brain.script = [{"diary": ""}]                            # 白卷是正确答案
    assert memory_brain.maintain_diary({"base_url": "http://x"}) == 0


def test_maintain_diary_merges_session_files(brain):
    """同项目多会话素材：跨文件合并 + 每会话保底（小会话不被全局尾窗截掉——
    机械层只保证「被看见」，写不写仍由意义判决定）。"""
    from omni_core.local import runtime_paths as P
    from omni_core.local.task_store import ProjectStore
    from datetime import timedelta

    ProjectStore.ensure("qq")
    base = _diary_day_iso().replace(hour=0, minute=0, second=0, microsecond=0)
    # s_a：200 条大会话（占满全局尾窗）
    rows_a = []
    for i in range(200):
        ts = (base + timedelta(seconds=i * 60)).isoformat()
        rows_a.append(json.dumps({"ts": ts, "role": "user",
                                  "content": f"「甲」(1): 群聊灌水第{i}句"}, ensure_ascii=False))
    (P.project_dir("qq") / "s_a.jsonl").write_text("\n".join(rows_a) + "\n", encoding="utf-8")
    # s_b：2 条小私聊，时间戳排在最前（全局尾窗外，只能靠会话保底被看见）
    rows_b = []
    for i, text in enumerate(("早上我们聊了鱼粮的口味问题", "中午你还答应给我加饭")):
        rows_b.append(json.dumps({"ts": (base + timedelta(minutes=i)).isoformat(),
                                  "role": "user", "content": f"「乙」(2): {text}"},
                                 ensure_ascii=False))
    (P.project_dir("qq") / "s_b.jsonl").write_text("\n".join(rows_b) + "\n", encoding="utf-8")

    brain.script = [{"diary": "早晚都有故事。"}]
    assert memory_brain.maintain_diary({"base_url": "http://x"}) == 1
    assert "投喂" not in brain.calls[-1]           # s_a 的灌水在尾窗内但早段被截
    assert "灌水第199句" in brain.calls[-1]         # 大会话尾部在场
    assert "鱼粮的口味" in brain.calls[-1] and "加饭" in brain.calls[-1]   # 小会话保底被看见


def test_diary_self_names_anchor(brain):
    """身份锚：渠道登记的名字（note_self_names）进 diary system prompt——core 零写死，
    换账号/换渠道随登记自愈。"""
    from omni_core.local import scene_executor

    day_iso = _diary_day_iso().isoformat()
    _seed_sessions({"qq": [
        {"ts": day_iso, "role": "user",
         "content": "「成员甲」(999999): [引用] 测试名片A: 随便说说而已你又不会当真 "
                    "| 你再说一遍，刚才那句我没听清，你重讲"},
        {"ts": day_iso, "role": "assistant",
         "content": "行，那我再讲一遍：鱼离开水就这声，你将就着听，反正你也学不会"},
        {"ts": day_iso, "role": "user",
         "content": "「成员甲」(999999): 就这？我看你今天也没多能说，说完就算了吧"},
        {"ts": day_iso, "role": "assistant",
         "content": "嫌难听还点，下次不叫了，反正另一条鱼的声音也就一般般"},
    ]})
    memory_brain.note_self_names(["测试名片A", "100000001"])
    try:
        brain.script = [{"diary": "有人引用我的话来着。"}]
        assert memory_brain.maintain_diary({"base_url": "http://x"}) == 1
        assert "测试名片A" in brain.calls[-1] and "100000001" in brain.calls[-1]
        assert scene_executor.read_scene_block("qq", f"diary-{_diary_day_iso():%Y-%m-%d}.md")
    finally:
        memory_brain._SELF_NAMES.clear()


def test_month_diary_compress_and_idempotent(brain):
    for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
        from omni_core.local import scene_executor

        scene_executor.write_diary_block(
            "qq", d, f"九月{d[-1]}日，成员甲又挑我毛病说我这也不行那也不行，我怼回去说它自己也没高明到哪去，晚上主人还夸了我今天的任务做得利索")
    brain.script = [{"summary": "九月：与成员甲语音互踩，主人夸我",
                     "month_diary": "九月成员甲总踩我语音，我没输。"}]
    assert memory_brain.maintain_month_diary({"base_url": "http://x"}) == 1
    from omni_core.local import scene_executor

    raw = scene_executor.read_scene_block("qq", "diary-month-2026-09.md")
    assert "九月：与成员甲语音互踩" in raw                 # 摘要行在块首（地图）
    brain.script = [{"summary": "s", "month_diary": "x"}]
    assert memory_brain.maintain_month_diary({"base_url": "http://x"}) == 0  # 幂等


def test_month_diary_skips_current_and_thin(brain):
    today = memory_brain._now()[:10]
    from omni_core.local import scene_executor

    scene_executor.write_diary_block("qq", today, f"今天{today}的事")   # 当月：不压
    scene_executor.write_diary_block("qq", "2026-09-21", "九月廿一日单篇")  # <3 篇：不压
    assert memory_brain.maintain_month_diary({"base_url": "http://x"}) == 0
    assert not scene_executor.read_scene_block("qq", f"diary-month-{today[:7]}.md")
    assert not scene_executor.read_scene_block("qq", "diary-month-2026-09.md")


def test_nav_includes_month_beyond_daily_window(brain):
    from omni_core.local import scene_executor

    for d in range(1, 9):
        scene_executor.write_diary_block("qq", f"2026-09-{d:02d}", f"九月{d}日")
    scene_executor.write_diary_block("qq", "month-2026-09", "九月地图行")
    names = [e["filename"] for e in scene_executor.load_scene_nav("qq")]
    assert "diary-month-2026-09.md" in names                  # 月记全露（不挤 7 篇窗口）
    assert len([n for n in names if n.startswith("diary-2")]) == 7
