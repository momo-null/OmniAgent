"""记忆召回整治（plan memory-recall-2026-11-02）P0/P1/P5/P7 行为的回归锁。

覆盖：绝对分（bm25_rank_to_score）、覆盖门槛（单命中高频词才丢，低频专名保留）、
同主题去冗余、
新鲜度轮换（×0.85 只降权不排除）、巩固期 FTS 近邻候选（P5）、
state 只剩 active/tombstone 且 salience=importance 零衰减（P7）、
top_k=5 与每 run 自取观测行 memory_recall.jsonl（P1）。

库落 tmp_path（brain fixture 同 test_memory_brain），不碰真实数据。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain  # noqa: E402


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(memory_brain, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(memory_brain, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    monkeypatch.setattr(memory_brain, "_cfg",
                        lambda key, default=None: {"persona": "tangtang"}.get(
                            key, default if default is not None
                            else memory_brain._LTM_DEFAULTS.get(key)))
    memory_brain._recent_ring.clear()
    yield
    memory_brain._recent_ring.clear()


# ---- P0.1 绝对分 ----

def test_bm25_rank_to_score_absolute():
    f = memory_brain.bm25_rank_to_score
    assert f(-2.0) == pytest.approx(2.0 / 3.0)
    assert f(-1.0) == pytest.approx(0.5)
    assert f(0.0) == 0.0                      # 非负（异常值）→ 0
    # 与候选集无关：同一 rank 在任何查询下得分相同
    assert f(-1.64) == pytest.approx(1.64 / 2.64)


def test_rel_no_longer_rank_normalized_inject_floor_gates(brain):
    """排名归一下末位恒 0.50 全过 floor；绝对分下强/弱命中分差真实体现（P0 根因 A）。"""
    conn = memory_brain.open_brain()
    now = memory_brain._now()
    memory_brain._insert_atom(conn, "r1", {"text": "盘前分析", "kind": "fact",
                                           "importance": 0.9}, "", "p", "t", now)
    memory_brain._insert_atom(conn, "r2", {"text": "另一条完全无关的持仓纪律",
                                           "kind": "rule", "importance": 0.9},
                              "", "p", "t", now)
    conn.commit()
    h1 = memory_brain.search_brain("盘前分析", conn=conn, reinforce=False)
    conn.close()
    assert [h["record_id"] for h in h1] == ["r1"]
    assert 0.0 < h1[0]["score"] <= 1.0


# ---- P0.2 覆盖门槛 ----

def test_coverage_gate_drops_single_word_glue(brain):
    """只拼到一个**高频词**的弱命中直接丢；单命中低频词（真主题）保留。"""
    glue_ids = [memory_brain.remember(f"第{i}份报告已归档入库", kind="fact", importance=0.6)
                for i in range(4)]
    spec = memory_brain.remember("盘前分析包含一句话结论", kind="fact", importance=0.9)
    ids = {h["record_id"] for h in
           memory_brain.search_brain("盘前分析报告在哪看", reinforce=False)}
    assert spec in ids            # 多词命中：真相关
    assert not set(glue_ids) & ids  # 「报告」df>3 且单命中 → glue 丢


def test_coverage_keeps_rare_single_term_hit(brain):
    """单命中但是低频专名（df≤3）：是主题词本身，不能当 glue 杀（白丝那个事）。

    query 里把「白丝」用引号断开——simple 下走短语、trigram 下走 LIKE 兜底，两种
    分词都能召回；门槛只看匹配词表，与分词模式无关。
    """
    for i in range(5):
        memory_brain.remember(f"无关条目{i}：群聊回复格式约定", kind="rule", importance=0.6)
    rid = memory_brain.remember("用户对白丝表现出明显喜爱", kind="preference", importance=0.9)
    ids = [h["record_id"] for h in memory_brain.search_brain("「白丝」那个事", reinforce=False)]
    assert rid in ids


def test_query_terms_extraction():
    t = memory_brain._query_terms
    assert t("买入点 ETF 纪律") == ["买入", "入点", "etf", "纪律"]
    assert t("a") == []                       # 单字丢弃
    assert t("「白丝」!") == ["白丝"]


# ---- P0.3 同主题去冗余 ----

def test_dedupe_same_kind_peer_prefix(brain):
    now = memory_brain._now()
    conn = memory_brain.open_brain()
    for i, suffix in enumerate(("", "（补充）", "（再次）")):
        memory_brain._insert_atom(conn, f"d{i}",
                                  {"text": "成员甲第0次踩我语音记录" + suffix,
                                   "kind": "episode", "importance": 0.6,
                                   "peer": "成员甲(999999)"}, "", "p", "t", now)
    conn.commit()
    conn.close()
    hits = memory_brain.search_brain("成员甲踩我语音", k=5, reinforce=False)
    assert len(hits) == 1                      # 同组（kind+peer+前12字）只留最高分


# ---- P0.4 新鲜度轮换 ----

def test_recency_rotation_penalizes_only(brain):
    now = memory_brain._now()
    conn = memory_brain.open_brain()
    memory_brain._insert_atom(conn, "x1", {"text": "盘前分析固定结论", "kind": "rule",
                                           "importance": 0.9}, "", "p", "t", now)
    memory_brain._insert_atom(conn, "x2", {"text": "盘前分析之补充说明", "kind": "rule",
                                           "importance": 0.9}, "", "p", "t", now)
    conn.commit()
    first = memory_brain.search_brain("盘前分析补充", k=2, conn=conn, reinforce=True)
    top1 = first[0]["record_id"]
    assert "x1" in {h["record_id"] for h in first}
    second = memory_brain.search_brain("盘前分析补充", k=2, conn=conn, reinforce=True)
    # 轮换只降权不排除：上一轮 top1 仍在结果里
    assert top1 in {h["record_id"] for h in second}
    conn.close()


# ---- P5 巩固期全文近邻候选 ----

def test_fts_neighbors_full_text_candidates(brain):
    """同义不同措辞：content[:12] 精确短语命不中，全文 FTS 近邻能命中的场景。

    语料设计：两条共享中后段词面「以主人身份」，但 n2 的前 12 字短语在 n1 中
    不是连续子串——trigram 与 simple 两种分词下旧探针都命不中（实测 0/33 的复现）。
    """
    now = memory_brain._now()
    conn = memory_brain.open_brain()
    memory_brain._insert_atom(conn, "n1", {"text": "提醒：以主人身份只在飞书", "kind": "rule",
                                           "importance": 0.9}, "", "p", "t", now)
    memory_brain._insert_atom(conn, "n2", {"text": "QQ 上也以主人身份说话", "kind": "rule",
                                           "importance": 0.9}, "", "p", "t", now)
    conn.commit()
    cands = dict(memory_brain._fts_neighbors(conn, "QQ 上也以主人身份说话", "n2"))
    conn.close()
    assert "n1" in cands                        # 全文近邻：共享词面可见
    conn = memory_brain.open_brain()
    c12 = conn.execute("SELECT record_id FROM atoms_fts WHERE atoms_fts MATCH ? "
                       "AND record_id != ?", ('"QQ 上也以主人身份说话"', "n2")).fetchall()
    conn.close()
    assert c12 == []                            # 旧机制：0 候选（去重空转的根因）


def test_consolidate_uses_fulltext_neighbors_and_merges(brain, monkeypatch):
    """构造同义不同措辞一对：巩固期能拿到候选并执行 merge（P5 验收用例）。"""
    now = memory_brain._now()
    conn = memory_brain.open_brain()
    memory_brain._insert_atom(conn, "m1", {"text": "提醒：以主人身份只在飞书", "kind": "rule",
                                           "importance": 0.9}, "", "p", "t", now)
    memory_brain._insert_atom(conn, "m2", {"text": "QQ 上也以主人身份说话", "kind": "rule",
                                           "importance": 0.9}, "", "p", "t", now)
    conn.commit()
    conn.close()
    for fn in ("maintain_profile", "maintain_diary", "maintain_month_diary",
               "maintain_relations"):
        monkeypatch.setattr(memory_brain, fn, lambda *a, **k: 0)

    def _router(cfg, system, user, timeout=60.0):
        if "一致性评审器" in system:
            # 处理 m1（新）×m2（旧候选）：判 merge，目标=m2，merged 覆盖目标
            return {"decisions": [{"idx": 0, "action": "merge", "target_id": "m2",
                                   "merged_content": "主人身份只在飞书与 QQ 都说"}]}

        return {"rule": ""}

    monkeypatch.setattr(memory_brain.llm_judge, "chat_json", _router)
    out = memory_brain._consolidate_impl({"base_url": "http://x"}, "tangtang")
    conn = memory_brain.open_brain()
    st2 = conn.execute("SELECT content, state FROM atoms WHERE record_id='m2'").fetchone()
    st1 = conn.execute("SELECT state FROM atoms WHERE record_id='m1'").fetchone()
    conn.close()
    assert out["merged"] >= 1
    assert st1[0] == "tombstone"                      # 被取代 → tombstone（P7 保留态）
    assert st2[1] == "active" and "飞书" in st2[0]


# ---- P7 遗忘删除 ----

def test_states_only_active_and_tombstone(brain):
    conn = memory_brain.open_brain()
    now = memory_brain._now()
    memory_brain._insert_atom(conn, "s1", {"text": "静态显著性检查", "kind": "fact",
                                           "importance": 0.4}, "", "p", "t", now)
    conn.commit()
    conn.close()
    rid = memory_brain.remember("另一条老事实", kind="fact", importance=0.4)
    # 巩固跑一轮：salience 不再衰减（=importance），也不产生 dormant/archived
    memory_brain._consolidate_impl({"base_url": "http://x"}, "tangtang")
    conn = memory_brain.open_brain()
    rows = conn.execute("SELECT record_id, state, salience, importance FROM atoms").fetchall()
    conn.close()
    assert all(r[1] in ("active", "tombstone") for r in rows)
    sal_of_s1 = {r[0]: (r[2], r[3]) for r in rows}["s1"]
    assert abs(sal_of_s1[0] - sal_of_s1[1]) < 1e-9   # salience == importance
    assert rid


def test_forgetting_config_keys_gone(brain):
    assert "forgetting_enabled" not in memory_brain._LTM_DEFAULTS
    assert "dormant_threshold" not in memory_brain._LTM_DEFAULTS
    assert "archive_threshold" not in memory_brain._LTM_DEFAULTS
    assert not hasattr(memory_brain, "live_salience")
    import inspect
    sig = inspect.signature(memory_brain.search_brain)
    assert "include_dormant" not in sig.parameters


# ---- P1 观测：memory_recall.jsonl ----

def test_top_k_default_is_five(brain):
    assert memory_brain._LTM_DEFAULTS["top_k"] == 5


def test_recall_stats_accumulate_and_observability_line(brain):
    import json
    from omni_core.tools import memory_tool as mt

    rid = memory_brain.remember("用户对白丝表现出明显喜爱", kind="preference",
                                importance=0.9)
    mt.reset_recall_stats()
    # 2 字 query：trigram 下走 LIKE 兜底，simple 下走 bigram 短语——两种分词都有召回
    r = mt.memory_recall(query="白丝", k=5)
    assert r["ok"] and r["hits"]
    st = mt.take_recall_stats()
    assert st["calls"] == 1 and st["hits"] == len(r["hits"]) and st["top1_id"] == rid
    memory_brain.log_recall_observability(project="qq", source="T0", calls=st["calls"],
                                          hits=st["hits"], top1_id=st["top1_id"])
    memory_brain.log_recall_observability(project="qq", source="private", calls=0)
    path = os.path.join(os.path.dirname(memory_brain._BRAIN_PATH), "memory_recall.jsonl")
    recs = [json.loads(x) for x in open(path, encoding="utf-8")]
    assert recs[0]["zero_query"] is False and recs[0]["top1_id"] == rid
    assert recs[1]["zero_query"] is True          # 「该查而未查」由此对出
