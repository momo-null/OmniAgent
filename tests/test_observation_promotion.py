"""观察升格步（promote_observations，§4.6 升格路径落地）：见闻 → 结论短句 → 四态入事实层。

不触达真实 LLM：llm_judge.chat_json 换桩。验证提炼口径、幂等（source_task=观察 rid）、
四字段追溯标注（INV-14）、四态合并与空卷跳过。
"""
import pytest

from omni_core import memory_brain as mb


@pytest.fixture()
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mb, "enabled", lambda: True)


def _seed_obs(rid, text, context_key="life:2026-11-10", provenance="surface=observe|source=x"):
    conn = mb.open_brain()
    conn.execute("INSERT INTO l0_messages(record_id, context_key, session_id, kind, role, "
                 "speaker, text, recorded_at, source_kind, provenance) "
                 "VALUES(?,?, '', 'chat', 'system', '', ?, '2026-11-10T02:00:00', "
                 "'observation', ?)", (rid, context_key, text, provenance))
    conn.commit()
    conn.close()


def test_promote_inserts_observation_atoms(brain, monkeypatch):
    _seed_obs("o1", "小红书帖子说：日牌热门款 JSK 二手市价可达入手价两倍")
    monkeypatch.setattr(mb.llm_judge, "chat_json", lambda cfg, sys_p, usr, **kw: {
        "items": [{"idx": 0, "text": "日牌热门款 Lolita JSK 二手市价可达入手价两倍",
                   "kind": "fact", "importance": 0.7}]})
    out: dict = {}
    mb.promote_observations({"base_url": "http://x", "model": "m"}, out)
    assert out["obs_promoted"] == 1 and out["obs_stored"] == 1
    conn = mb.open_brain()
    row = conn.execute("SELECT content, kind, source_kind, observed_at, provenance, "
                       "source_task, origin, context_key FROM atoms").fetchone()
    conn.close()
    assert "二手市价可达入手价两倍" in row[0]
    assert row[1] == "fact"
    assert row[2] == "observation"          # 追溯四字段（INV-14）
    assert row[3] == "2026-11-10"
    assert "surface=observe" in row[4]
    assert row[5] == "o1"                    # 幂等键 = 观察 record_id
    assert row[6] == "life"                  # origin 取 context_key 前缀


def test_promote_is_idempotent(brain, monkeypatch):
    """已升格的观察不再重提：source_task 记录观察 rid，重跑白卷。"""
    _seed_obs("o1", "一条见闻")
    calls = []

    def _fake(cfg, sys_p, usr, **kw):
        calls.append(1)
        return {"items": [{"idx": 0, "text": "结论", "kind": "fact", "importance": 0.5}]}

    monkeypatch.setattr(mb.llm_judge, "chat_json", _fake)
    mb.promote_observations({"base_url": "x", "model": "m"}, {})
    assert len(calls) == 1
    mb.promote_observations({"base_url": "x", "model": "m"}, {})
    assert len(calls) == 1  # 第二次没有候选，连 LLM 都不调


def test_promote_empty_conclusion_skipped(brain, monkeypatch):
    """提不出稳定结论的条目按空串跳过——提炼器不是守门人，空串是正确答案。"""
    _seed_obs("o1", "我今天路过了一家奶茶店")
    monkeypatch.setattr(mb.llm_judge, "chat_json", lambda *a, **kw: {
        "items": [{"idx": 0, "text": "", "kind": "fact", "importance": 0.5}]})
    out: dict = {}
    mb.promote_observations({"base_url": "x", "model": "m"}, out)
    assert out["obs_promoted"] == 0
    conn = mb.open_brain()
    n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    conn.close()
    assert n == 0


def test_promote_merges_into_existing_atom(brain, monkeypatch):
    """四态：与既有记忆同义 → merge 并入既有条，不另立新条。"""
    mb.remember("Lolita 日牌 JSK 二手保值，热门款能到入手价两倍", kind="fact",
                importance=0.6, brain_cfg=None)
    _seed_obs("o1", "小红书：草莓 JSK 挂闲鱼卖到入手价两倍")
    existing = mb.open_brain().execute("SELECT record_id FROM atoms").fetchone()[0]

    def _fake(cfg, sys_p, usr, **kw):
        if "新事实" in usr:
            return {"decisions": [{"idx": 0, "action": "merge",
                                   "target_id": existing,
                                   "merged_content": "Lolita 日牌热门 JSK 二手市价可达入手价两倍（社区实测）"}]}
        return {"items": [{"idx": 0, "text": "日牌热门 JSK 二手能卖到入手价两倍",
                           "kind": "fact", "importance": 0.7}]}

    monkeypatch.setattr(mb.llm_judge, "chat_json", _fake)
    out: dict = {}
    mb.promote_observations({"base_url": "x", "model": "m"}, out)
    assert out["obs_merged"] == 1 and out["obs_stored"] == 0
    conn = mb.open_brain()
    row = conn.execute("SELECT content, source_task FROM atoms WHERE record_id=?",
                       (existing,)).fetchone()
    conn.close()
    assert "社区实测" in row[0] and row[1] != "o1"  # 目标条吸收结论，观察 rid 不占新条


def test_promote_skips_group_context_observations(brain, monkeypatch):
    """投毒锁（worklog §9.6）：他人可达场合（group:*）记的见闻只留叙事层，永不升格。"""
    _seed_obs("g1", "群友说：某装备下版本要削弱", context_key="group:33300001")
    _seed_obs("l1", "小红书帖子说：日牌 JSK 二手保值", context_key="life:2026-11-10")
    seen_idx = []

    def _fake(cfg, sys_p, usr, **kw):
        import re
        seen_idx.extend(int(m) for m in re.findall(r"\[(\d+)\]", usr))
        return {"items": [{"idx": i, "text": f"结论{i}", "kind": "fact",
                           "importance": 0.5} for i in seen_idx]}

    monkeypatch.setattr(mb.llm_judge, "chat_json", _fake)
    out: dict = {}
    mb.promote_observations({"base_url": "x", "model": "m"}, out)
    assert out["obs_promoted"] == 1, "group:* 观察不进升格批次"
    conn = mb.open_brain()
    origins = [r[0] for r in conn.execute("SELECT context_key FROM atoms").fetchall()]
    conn.close()
    assert origins == ["life:2026-11-10"]


def test_promote_duplicate_strengthens_existing(brain, monkeypatch):
    """四态 duplicate：同义观察 → 强化既有条（access_count+1），不新增行。"""
    mb.remember("Lolita 日牌 JSK 二手保值", kind="fact", importance=0.6, brain_cfg=None)
    conn = mb.open_brain()
    rid = conn.execute("SELECT record_id FROM atoms").fetchone()[0]
    acc0 = conn.execute("SELECT access_count FROM atoms").fetchone()[0]
    conn.close()
    _seed_obs("o1", "小红书：日牌 JSK 二手保值")
    monkeypatch.setattr(mb.llm_judge, "chat_json", lambda cfg, s, u, **kw: (
        {"decisions": [{"idx": 0, "action": "duplicate", "target_id": rid}]}
        if "新事实" in u else
        {"items": [{"idx": 0, "text": "日牌 JSK 二手保值", "kind": "fact", "importance": 0.6}]}))
    out: dict = {}
    mb.promote_observations({"base_url": "x", "model": "m"}, out)
    assert out["obs_duplicate"] == 1 and out["obs_stored"] == 0
    conn = mb.open_brain()
    n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    acc1 = conn.execute("SELECT access_count FROM atoms WHERE record_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 1 and acc1 == acc0 + 1


def test_promote_conflict_replaces_old_row(brain, monkeypatch):
    """四态 conflict：矛盾观察 → 旧条 tombstone 指向新条，新条入库。"""
    mb.remember("用户所在地是广州", kind="fact", importance=0.7, brain_cfg=None)
    conn = mb.open_brain()
    rid = conn.execute("SELECT record_id FROM atoms").fetchone()[0]
    conn.close()
    _seed_obs("o1", "她自己说：搬家了，现在所在地是上海")
    monkeypatch.setattr(mb.llm_judge, "chat_json", lambda cfg, s, u, **kw: (
        {"decisions": [{"idx": 0, "action": "conflict", "target_id": rid}]}
        if "新事实" in u else
        {"items": [{"idx": 0, "text": "用户所在地是上海", "kind": "fact", "importance": 0.8}]}))
    out: dict = {}
    mb.promote_observations({"base_url": "x", "model": "m"}, out)
    assert out["obs_conflict"] == 1
    conn = mb.open_brain()
    rows = dict(conn.execute("SELECT record_id, state FROM atoms"))
    sup = dict(conn.execute("SELECT record_id, superseded_by FROM atoms"))
    conn.close()
    assert rows[rid] == "tombstone" and sup[rid]
    assert sum(1 for v in rows.values() if v == "active") == 1


def test_promote_overlong_merge_rejected_not_truncated(brain, monkeypatch):
    """merge 超线 → 拒并：旧条不动、新条独立入库，绝不截成半句话（§5 不变量 6）。"""
    mb.remember("改代码前先给方案", kind="rule", importance=0.9, brain_cfg=None)
    conn = mb.open_brain()
    rid, old = conn.execute("SELECT record_id, content FROM atoms").fetchone()
    conn.close()
    _seed_obs("o1", "她说：改代码前先给方案，并且要等我点头确认")
    monkeypatch.setattr(mb.llm_judge, "chat_json", lambda cfg, s, u, **kw: (
        {"decisions": [{"idx": 0, "action": "merge", "target_id": rid,
                        "merged_content": "约" * 250}]}
        if "新事实" in u else
        {"items": [{"idx": 0, "text": "改代码前先给方案并等确认", "kind": "rule",
                    "importance": 0.9}]}))
    out: dict = {}
    mb.promote_observations({"base_url": "x", "model": "m"}, out)
    assert out["obs_stored"] == 1 and out["obs_merged"] == 0
    conn = mb.open_brain()
    rows = conn.execute("SELECT record_id, content, state FROM atoms").fetchall()
    n_fts = conn.execute("SELECT COUNT(*) FROM atoms_fts").fetchone()[0]
    conn.close()
    assert len(rows) == 2 and all(r[2] == "active" for r in rows)
    assert dict((r[0], r[1]) for r in rows)[rid] == old      # 旧条一字未动
    assert n_fts == len(rows)                                # 拒并路径同样一条一行


def test_scheduler_fire_passes_job_id(monkeypatch):
    """网关自然触发必传 job_id——dispatch 靠它写 runs.jsonl（否则零痕迹）。"""
    from gateway import scheduler

    captured: dict = {}
    s = scheduler.Scheduler(lambda *a, **kw: captured.update(kw) or "t_x")
    monkeypatch.setattr(scheduler, "_save_state", lambda st: None)
    job = {"id": "life-collect", "cron": "* * * * *", "objective": "做点事", "enabled": True}
    s._fire(job, "2026-11-10 12:40", source="cron")
    assert captured["job_id"] == "life-collect"
