"""读侧候选包（高置信线索 / 一次取用 / 独立额度）回归锁。"""
from omni_core.tools import memory_tool as mt


class _FakeBrain:
    """只实现候选包用到的检索面，避免碰真库。"""

    def enabled(self):
        return True

    def _query_terms(self, q):
        return ["裙子", "小裙"]

    def search_brain(self, query, k=3, reinforce=False, peer="", context_key=""):
        return [
            {"record_id": "m_hi", "content": "过关的高置信结论", "score": 0.70},
            {"record_id": "m_lo", "content": "低分噪声", "score": 0.20},
        ]

    def search_narrative(self, query, limit=3, peer="", context_key="",
                         require_match=False):
        return [
            {"doc_id": "d1", "kind": "diary", "summary": "裙子相关的一段", "topics": [],
             "score": 0.70},
            {"doc_id": "d2", "kind": "month", "summary": "完全无关的固定近因条目",
             "topics": [], "score": 0.70},
        ]

    def search_l0(self, keyword, limit=1, context_key="", session_id=""):
        return [{"text": "一句逐字原话线索"}]


CFG = {"enabled": True, "total_cap": 2, "item_chars": 60,
       "l1_min_score": 0.60, "narrative_min_score": 0.65, "l0_limit": 0}


def _setup(monkeypatch, **overrides):
    monkeypatch.setattr(mt, "_bundle_cfg", lambda: dict(CFG, **overrides))
    monkeypatch.setattr(mt, "_brain", lambda: _FakeBrain())
    mt.reset_recall_stats()


def test_bundle_keeps_only_high_confidence(monkeypatch):
    _setup(monkeypatch)
    n = mt.build_candidate_bundle("想穿小裙子", context_key="ctx:1", session_id="s1")
    assert n == 2                      # L1 过线 1 + 叙事 FTS/词面过线 1
    items = mt.take_candidate_bundle()
    assert {x["src"] for x in items} == {"atom", "narrative"}
    assert all("低分噪声" not in x["text"] for x in items)
    assert all("完全无关" not in x["text"] for x in items)
    assert all(len(x["text"]) <= CFG["item_chars"] for x in items)


def test_bundle_l0_channel_is_optional(monkeypatch):
    _setup(monkeypatch, l0_limit=1, total_cap=3)
    assert mt.build_candidate_bundle("想穿小裙子") == 3
    assert "raw" in {x["src"] for x in mt.take_candidate_bundle()}


def test_bundle_empty_query_is_noop(monkeypatch):
    _setup(monkeypatch)
    assert mt.build_candidate_bundle("") == 0
    assert mt.bundle_size() == 0


def test_bundle_disabled(monkeypatch):
    _setup(monkeypatch, enabled=False)
    assert mt.build_candidate_bundle("想穿小裙子") == 0


def test_empty_query_recall_uses_bundle_without_deep_quota(monkeypatch):
    _setup(monkeypatch)
    mt.build_candidate_bundle("想穿小裙子")
    before = mt.quota_left()["total"]
    r = mt.memory_recall(query="")
    assert r["ok"] and r["candidates"] and r["hits"] == []
    assert mt.quota_left()["total"] == before       # 不占深挖 3 次配额
    assert mt.take_recall_stats()["bundle_taken"] == 1
    r2 = mt.memory_recall(query="")
    assert r2["ok"] and r2["candidates"] == []     # 1 次/run


def test_reset_clears_bundle_and_counters(monkeypatch):
    _setup(monkeypatch)
    mt.build_candidate_bundle("想穿小裙子")
    mt.memory_recall(query="")
    mt.reset_recall_stats()
    st = mt.take_recall_stats()
    assert st["evidence"] == 0 and st["bundle_taken"] == 0
    assert mt.bundle_size() == 0


def test_quota_block_is_counted(monkeypatch):
    _setup(monkeypatch)
    for _ in range(3):
        mt.note_self_fetch(1, "m1", tool="memory_recall")
    blocked = mt._quota_block("conversation_search")
    assert blocked and not blocked["ok"]
    assert mt.take_recall_stats()["quota_blocked"] == 1


def test_history_fold_note_is_generic_capability_hint():
    from gateway import dispatch
    recs = [{"role": "user", "content": "x"}] * 15
    items = [{"role": "user", "content": "x"}] * 12
    note = dispatch._history_fold_note(recs, items)
    assert "3 轮" in note and "conversation_search" in note
    assert dispatch._history_fold_note(items, items) == ""
