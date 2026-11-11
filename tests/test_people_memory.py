"""按「人」记忆的 L0 直驱回归锁。

四个契约：
1. 无 L1 增量的收尾（2026-11-10 严格口径）也能由 L0 更新关系卡，空 atoms 库不挡路；
2. 关系卡索引带可检索摘要，按名字/标识能命中；
3. 外部来源读 relation 仍只给摘要、正文不外泄（N-10 不破）；
4. 入站 speaker 兜底 + 本轮边界 peer 盖章，通道无关。

构造值，不触真实数据；不拉真实模型。
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain as mb  # noqa: E402
from omni_core.local import runtime_paths as rp  # noqa: E402

CFG = {"base_url": "http://x", "model": "m"}


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mb, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    rel = tmp_path / "relations"
    monkeypatch.setattr(rp, "global_relations", lambda: rel)
    stub = types.ModuleType("config")
    stub.get_config = lambda k, d=None: d
    monkeypatch.setitem(sys.modules, "config", stub)
    return tmp_path


def _seed_l0(n=12, speaker="构造甲(10001)"):
    for i in range(n):
        mb.index_l0_message("s_people", "user", f"构造往来第{i}句",
                            context_key="group:10001", speaker=speaker)


# ---- 1. 空库直驱 ----

def test_relations_driven_by_l0_with_empty_atoms(brain, monkeypatch):
    """atoms 为空时关系卡仍能生成：巩固周期的空库短路不再挡路。"""
    monkeypatch.setattr(mb, "_REL_MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(mb, "llm_judge", types.SimpleNamespace(
        chat_json=lambda *a, **k: {"card": "最近交手：常来聊天。\n他对我的态度：熟络。\n"
                                           "我对他的态度：愿意接话。",
                                   "topics": ["构造话题"]}))
    _seed_l0()
    conn = mb.open_brain()
    try:
        assert conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 0
    finally:
        conn.close()

    assert mb.maybe_maintain_relations(CFG) == 0       # 首轮只记基线，不灌历史
    _seed_l0(n=12)                                     # 新增量到阈值
    assert mb.maybe_maintain_relations(CFG) == 1       # 由 L0 直驱写出一张卡
    assert (rp.global_relations() / "10001.md").is_file()
    conn = mb.open_brain()
    try:
        assert conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0] == 0  # 仍未放开 L1
    finally:
        conn.close()


def test_flush_tail_runs_relations_on_l1_skip(brain, monkeypatch):
    """普通收尾路径（无 L1 增量）确实会调用关系卡维护（接线不丢）。"""
    calls = []
    monkeypatch.setattr(mb, "maybe_maintain_relations",
                        lambda cfg: calls.append(cfg) or 0)
    monkeypatch.setattr(mb, "maybe_consolidate", lambda cfg: None)
    monkeypatch.setattr(mb, "run_context_reflections", lambda *a, **k: 0)
    from omni_core.local import scene_executor
    monkeypatch.setattr(scene_executor, "maybe_maintain", lambda *a, **k: None)
    mb._flush_tail("p_people", CFG, {}, True, msgs=[])
    assert calls == [CFG]


# ---- 2. 索引可检索 ----

def test_relation_card_index_has_searchable_summary(brain, monkeypatch):
    monkeypatch.setattr(mb, "_peer_lines", lambda limit_days=7: {
        "10001": {"name": "构造甲", "lines": ["往来"] * 5}})
    monkeypatch.setattr(mb, "llm_judge", types.SimpleNamespace(
        chat_json=lambda *a, **k: {"card": "最近交手：常来聊天。",
                                   "topics": ["构造话题"]}))
    assert mb.maintain_relations(CFG) == 1
    row = mb.get_narrative_row("10001", "relation")
    assert row and "构造甲" in row["summary"] and "10001" in row["summary"]
    assert "最近交手" not in row["summary"]          # 摘要不含印象正文
    hits = mb.search_narrative(query="构造甲", kind="relation")
    assert any(h["doc_id"] == "10001" for h in hits)


# ---- 3. 出口约束 ----

def test_external_relation_reads_summary_only(brain, monkeypatch):
    from omni_core.local.loop import context_signal as cs
    from omni_core.tools import memory_tool as mt
    from omni_core.tools import narrative_tool as nt

    monkeypatch.setattr(mb, "_peer_lines", lambda limit_days=7: {
        "10001": {"name": "构造甲", "lines": ["往来"] * 5}})
    monkeypatch.setattr(mb, "llm_judge", types.SimpleNamespace(
        chat_json=lambda *a, **k: {"card": "他对我的态度：试探。",
                                   "topics": ["构造话题"]}))
    assert mb.maintain_relations(CFG) == 1
    mt.reset_recall_stats()
    mt._run_stats["active"] = False
    cs.set_boundary(context_key="group:10001", source_external=True)
    cs.build_run(context_key="group:10001")
    r = nt.narrative_read(doc_id="10001", kind="relation")
    assert r["ok"] and r["text"] == ""
    assert "构造甲" in r["summary"]                  # 认得出是谁
    assert "试探" not in r["summary"]                # 印象不外泄
    mt.reset_recall_stats()
    cs.set_boundary()


# ---- 4. 入站归一与边界盖章 ----

def test_gateway_fills_speaker_from_user_id(monkeypatch):
    from gateway import channel_gateway as cgw
    from gateway.channels.base import InboundMessage

    captured = {}
    monkeypatch.setattr(cgw.dispatch, "ensure_worker", lambda: None)
    monkeypatch.setattr(cgw, "_route_chat", lambda msg: ("p_people", "s_people"))
    monkeypatch.setattr(cgw.dispatch, "submit",
                        lambda text, **kw: captured.update(kw) or "t_1")

    cgw._on_message(InboundMessage(channel="c", chat_id="c:1",
                                   user_id="u_42", text="构造原文"))
    assert captured["speaker"] == "u_42(u_42)"

    # 适配层已归一则原样透传，不覆盖
    cgw._on_message(InboundMessage(channel="c", chat_id="c:1",
                                   user_id="u_42", text="构造原文",
                                   speaker="构造甲(10001)"))
    assert captured["speaker"] == "构造甲(10001)"


def test_dispatch_stamps_peer_boundary(monkeypatch):
    from gateway import dispatch
    from omni_core.local.loop import context_signal as cs

    seen = []
    real = cs.set_boundary

    def spy(**kw):
        if kw.get("peer"):
            seen.append(kw["peer"])
        return real(**kw)

    monkeypatch.setattr(cs, "set_boundary", spy)
    monkeypatch.setitem(sys.modules, "config", types.ModuleType("config"))
    import config as cfg_mod
    cfg_mod.load_config = lambda: {"brain": {"reasoning_mode": "native"}}
    from omni_core.brain import router as model_router
    monkeypatch.setattr(model_router, "_catalog", lambda: {
        "providers": {"p": {"label": "p", "base_url": "http://127.0.0.1:9",
                            "api_key": "k", "models": [{"id": "m", "label": "m"}]}},
        "defaults": {"main": "p/m"}})

    class _Loop:
        def __init__(self, *a, **k):
            pass

        def run_task(self, spec):
            return {"success": True, "summary": "构造回复"}

    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _Loop)
    dispatch.dispatch_sync("构造原文", session_id="s_people_boundary",
                           speaker="构造甲(10001)")
    assert seen == ["构造甲(10001)"]


# ---- 5. 主人卡称呼：名字的唯一宿主是卡（identity 署名跟随；不介绍=恒默认） ----

def test_master_display_name_defaults_without_card(brain, monkeypatch):
    """程序一致性：无卡/未介绍 → 署名恒为缺省「主人」，与历史行为逐字节一致。"""
    from omni_core.local import identity
    identity._NAME_CACHE.update({"mtime": None, "name": None})
    assert identity.master_speaker() == "主人(master)"
    assert identity.master_display_name() == "主人"


def test_master_display_name_follows_card_meta(brain, monkeypatch):
    """卡里有了称呼（维护写入）→ 署名标签跟随；缓存按 mtime 失效。"""
    from omni_core.local import identity
    identity._NAME_CACHE.update({"mtime": None, "name": None})
    card = identity and rp.global_relations() / "master.md"
    rp.global_relations().mkdir(parents=True, exist_ok=True)
    card.write_text(
        "-----META-START-----\nname: 阿蕾\nuid: master\nupdated: t\n-----META-END-----\n"
        "【稳定背景】\n- 搭起这个世界的人\n", encoding="utf-8")
    assert identity.master_display_name() == "阿蕾"
    assert identity.master_speaker() == "阿蕾(master)"
    # 坏值回退：META 无 name → 缺省
    card.write_text("-----META-START-----\nuid: master\n-----META-END-----\n", encoding="utf-8")
    identity._NAME_CACHE.update({"mtime": None, "name": None})
    assert identity.master_display_name() == "主人"


def test_master_card_maintenance_adopts_name(brain, monkeypatch):
    """维护者从素材采纳称呼 → 写进 META + 署名跟随；没给称呼 = 原样保留。"""
    from omni_core.local import identity
    identity._NAME_CACHE.update({"mtime": None, "name": None})
    mb._rel_l0_cursor = None  # 关系卡游标在 meta，不受影响；保险清零
    monkeypatch.setattr(mb, "_peer_lines", lambda limit_days=7: {
        "master": {"name": "主人", "lines": ["- 他自我介绍：叫我阿蕾，是搭起这个世界的人",
                                             "- 他说今天缸温刚好"] * 6}})
    script = [{"card": "【稳定背景】\n- 他叫我阿蕾，是搭起这个世界的人\n【最近交手】聊了称呼\n"
                       "【他对我的态度】随意\n【我对他的态度】熟",
               "topics": ["称呼"], "name": "阿蕾"}]
    monkeypatch.setattr(mb.llm_judge, "chat_json", lambda *a, **k: script.pop(0))
    assert mb.maintain_relations(CFG) == 1
    card = rp.global_relations() / "master.md"
    assert "name: 阿蕾" in card.read_text(encoding="utf-8")
    assert identity.master_display_name() == "阿蕾"

    # 下一轮没给称呼 → 保留现有称呼（不被缺省冲掉）
    script.append({"card": "【稳定背景】\n- 他叫我阿蕾，是搭起这个世界的人\n【最近交手】日常\n"
                           "【他对我的态度】随意\n【我对他的态度】熟",
                   "topics": ["称呼"], "name": ""})
    assert mb.maintain_relations(CFG) == 1
    assert "name: 阿蕾" in card.read_text(encoding="utf-8")


def test_master_card_without_introduction_stays_default(brain, monkeypatch):
    """从不介绍：维护照跑、称呼恒为缺省（程序一致性硬约束）。"""
    from omni_core.local import identity
    identity._NAME_CACHE.update({"mtime": None, "name": None})
    monkeypatch.setattr(mb, "_peer_lines", lambda limit_days=7: {
        "master": {"name": "主人", "lines": ["- 他让我跑一个采集任务"] * 6}})
    monkeypatch.setattr(mb.llm_judge, "chat_json", lambda *a, **k: {
        "card": "【最近交手】派了活\n【他对我的态度】公事公办\n【我对他的态度】配合",
        "topics": ["任务"], "name": ""})
    assert mb.maintain_relations(CFG) == 1
    card = rp.global_relations() / "master.md"
    assert "name: 主人" in card.read_text(encoding="utf-8")
    assert identity.master_display_name() == "主人"
