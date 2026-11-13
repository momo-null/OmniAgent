"""P2 · L0 逐字检索的回归锁（依据见 implemented/memory-architecture.md §13.1）。

覆盖：写入即入表（ProjectStore.append_message → brain.db.l0_messages）、时间窗（7 天前的
约定查得到、窗口外查不到）、会话范围隔离、conversation_search 的越权拒绝与配额记账、
一次性回填（幂等 + 跳过 partial 增量行）。

库与家目录都落 tmp（_isolated_home 是 autouse），不碰真实数据。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _quota_scope_off():
    """直调工具不在 run 作用域内：配额不强制（防批量顺序下共享账本泄漏）。"""
    from omni_core.tools import memory_tool as mt
    mt._run_stats["active"] = False
    yield
    mt._run_stats["active"] = False


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain  # noqa: E402
from omni_core.local.task_store import ProjectStore  # noqa: E402


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(memory_brain, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(memory_brain, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    monkeypatch.setattr(memory_brain, "_cfg",
                        lambda key, default=None: default if default is not None
                        else memory_brain._LTM_DEFAULTS.get(key))
    yield


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---- 1. 写入即入表 + 结构化说话人 ----------------------------------------------

def test_append_message_writes_through_to_l0(brain):
    ProjectStore.append_message("d-p2", "s_napcat_aaa", "user",
                                "「小明」(10001): 我们约好每周一发盘前分析",
                                context_key="group:10001", speaker="小明(10001)")
    rows = memory_brain.search_l0("盘前分析", session_id="s_napcat_aaa")
    assert len(rows) == 1, rows
    assert rows[0]["who"] == "小明(10001)"      # 边界给的结构化 speaker
    assert rows[0]["context_key"] == "group:10001"
    assert rows[0]["kind"] == "chat"


def test_search_l0_by_speaker_filters_and_allows_empty_keyword(brain):
    """按人查：只返回该对象说过的话；keyword 为空也能列出该对象的发言。"""
    ProjectStore.append_message("d-p2", "s_napcat_bbb", "user",
                                "「小明」(10001): 我周末想去爬山",
                                context_key="group:10001", speaker="小明(10001)")
    ProjectStore.append_message("d-p2", "s_napcat_bbb", "user",
                                "「小红」(10002): 我想去看电影",
                                context_key="group:10001", speaker="小红(10002)")
    rows = memory_brain.search_l0("", speaker="小明", context_key="group:10001")
    assert [r["who"] for r in rows] == ["小明(10001)"]
    assert "爬山" in rows[0]["text"]
    assert memory_brain.search_l0("", speaker="10001", context_key="group:10001")
    assert memory_brain.search_l0("", speaker="小明(10001)", context_key="group:10001")
    # 不传 speaker 时行为不变：按词面各查到对应的人
    assert len(memory_brain.search_l0("爬山", context_key="group:10001")) == 1
    assert len(memory_brain.search_l0("电影", context_key="group:10001")) == 1


def test_conversation_search_peer_returns_only_that_person(brain):
    from omni_core.tools import memory_tool as mt

    ProjectStore.append_message("d-p2", "s_napcat_ccc", "user",
                                "「小明」(10001): 我们约好周一发盘前分析",
                                context_key="group:10001", speaker="小明(10001)")
    ProjectStore.append_message("d-p2", "s_napcat_ccc", "user",
                                "「小红」(10002): 我也想看盘前分析",
                                context_key="group:10001", speaker="小红(10002)")
    r = mt.conversation_search(peer="小明", scope="all")
    assert r["ok"] is True and r["count"] == 1
    assert "小明" in r["results"][0]
    r2 = mt.conversation_search(keyword="盘前分析", peer="小红", scope="all")
    assert r2["count"] == 1 and "小红" in r2["results"][0]
    assert mt.conversation_search()["ok"] is False


def test_write_through_is_fail_soft(brain, monkeypatch):
    """索引坏了不得阻断会话落盘：search 端异常返回空，append_message 照常写 jsonl。"""
    monkeypatch.setattr(memory_brain, "index_l0_message",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("库炸了")))
    ProjectStore.append_message("d-p2b", "s_x", "user", "照常落盘")
    assert ProjectStore.read_session("d-p2b", "s_x")[-1]["content"] == "照常落盘"


def test_index_l0_noop_when_memory_disabled(brain, monkeypatch):
    monkeypatch.setattr(memory_brain, "enabled", lambda: False)
    assert memory_brain.index_l0_message("s_y", "user", "不该入表") == ""
    assert memory_brain.search_l0("不该") == []


# ---- 2. 时间窗（验收：查到 7 天前的具体约定） ----------------------------------

def test_seven_day_old_agreement_is_findable(brain):
    memory_brain.index_l0_message("s_napcat_bbb", "user",
                                  "「阿强」(20002): 周四例会改到下午三点",
                                  context_key="group:20002", recorded_at=_ago(7))
    assert memory_brain.search_l0("例会", session_id="s_napcat_bbb"), "30 天窗口要能覆盖 7 天前"
    assert memory_brain.search_l0("例会", session_id="s_napcat_bbb", days=3) == [], \
        "超出窗口必须查不到"


def test_results_are_chronological(brain):
    memory_brain.index_l0_message("s_c", "user", "第二次的盘前分析约定", recorded_at=_ago(2))
    memory_brain.index_l0_message("s_c", "user", "第一次的盘前分析约定", recorded_at=_ago(9))
    texts = [r["text"] for r in memory_brain.search_l0("盘前分析", session_id="s_c")]
    assert texts == ["第一次的盘前分析约定", "第二次的盘前分析约定"]


# ---- 3. 会话范围隔离 ----------------------------------------------------------

def test_scope_current_does_not_leak_other_chats(brain):
    memory_brain.index_l0_message("s_c1", "user", "便签放在桌面上", context_key="group:111")
    memory_brain.index_l0_message("s_c2", "user", "便签买过三盒", context_key="private:222")
    only = memory_brain.search_l0("便签", context_key="group:111")
    assert len(only) == 1 and only[0]["context_key"] == "group:111"
    everything = memory_brain.search_l0("便签")
    assert len(everything) == 2


# ---- 4. conversation_search 工具：ACL + 配额记账 -------------------------------

@pytest.fixture
def _ch_ctx(monkeypatch):
    """边界层结构写入 context_signal（内核只读它，不读网关句柄）。"""
    from omni_core.local.loop import context_signal as cs
    cs.set_boundary()
    cs._CURRENT.clear()
    yield cs
    cs.set_boundary()
    cs._CURRENT.clear()


def test_conversation_search_scopes_to_current(_ch_ctx, brain):
    from omni_core.tools import memory_tool as mt
    _ch_ctx.set_boundary(context_key="group:333")
    _ch_ctx.build_run(context_key="group:333")
    memory_brain.index_l0_message("s_d1", "user", "会议改到周三", context_key="group:333")
    memory_brain.index_l0_message("s_d2", "user", "会议改到周五", context_key="group:999")
    mt.reset_recall_stats()
    r = mt.conversation_search(keyword="会议", scope="current")
    assert r["ok"] and r["count"] == 1 and "会议改到周三" in r["results"][0]
    assert mt.take_recall_stats()["calls"] == 1, "自取工具必须进本 run 账本（≤3 次口径）"
    allr = mt.conversation_search(keyword="会议", scope="all")
    assert allr["ok"] and allr["count"] == 2 and mt.take_recall_stats()["calls"] == 2


def test_conversation_search_defaults_to_all_for_internal(_ch_ctx, brain):
    """黑名单式范围：内部来源缺省全量，新增场合/新人不必再配 scope。"""
    from omni_core.tools import memory_tool as mt
    _ch_ctx.set_boundary(context_key="private:777")
    _ch_ctx.build_run(context_key="private:777")
    memory_brain.index_l0_message("s_a1", "user", "群聊的约定甲", context_key="group:888")
    memory_brain.index_l0_message("s_a2", "user", "私聊的约定乙", context_key="private:777")
    mt.reset_recall_stats()
    r = mt.conversation_search(keyword="约定")
    assert r["ok"] and r["scope"] == "all" and r["count"] == 2
    assert mt.take_recall_stats()["calls"] == 1


def test_conversation_search_empty_result_does_not_claim_absence(_ch_ctx, brain):
    """空结果只能说「本轮没检索到」，不能说「没有」——后者等于对外断言事实不存在。"""
    from omni_core.tools import memory_tool as mt
    _ch_ctx.set_boundary(context_key="group:321")
    _ch_ctx.build_run(context_key="group:321")
    r = mt.conversation_search(keyword="不存在的词", peer="某个人")
    assert r["ok"] and r["count"] == 0
    assert "没有" not in r["note"]
    assert "没找到" in r["note"] and "不等于没说过" in r["note"]


def test_conversation_search_external_private_may_cross_context(_ch_ctx, brain):
    """私聊不收窄：主人私聊里问群聊的事应当查得到（external 不等于必须锁会话）。"""
    from omni_core.tools import memory_tool as mt
    memory_brain.index_l0_message("s_p1", "user", "群里的约定甲", context_key="group:900")
    memory_brain.index_l0_message("s_p2", "user", "私聊的约定乙", context_key="private:901")
    _ch_ctx.set_boundary(context_key="private:901", source_external=True)
    _ch_ctx.build_run(context_key="private:901")
    r = mt.conversation_search(keyword="约定")
    assert r["ok"] and r["scope"] == "all" and r["count"] == 2
    # 私聊里显式 scope=current 是空操作，不会把群聊记录切掉
    cur = mt.conversation_search(keyword="约定", scope="current")
    assert cur["ok"] and cur["scope"] == "all" and cur["count"] == 2


def test_conversation_search_master_is_never_scoped(_ch_ctx, brain):
    """来源是主人时不做范围限制：群里问「他今天说了什么」也该一次查全。"""
    from omni_core.tools import memory_tool as mt
    memory_brain.index_l0_message("s_m1", "user", "甲群的约定", context_key="group:111")
    memory_brain.index_l0_message("s_m2", "user", "乙群的约定", context_key="group:222")
    _ch_ctx.set_boundary(context_key="group:111", source_external=True,
                         peer="姐姐(master)")
    _ch_ctx.build_run(context_key="group:111")
    r = mt.conversation_search(keyword="约定")
    assert r["ok"] and r["scope"] == "all" and r["count"] == 2


def test_conversation_search_external_cannot_escape_current(_ch_ctx, monkeypatch, brain):
    """外部来源（他人可发消息）即便点名 scope=all，也只能查当前会话；定不到当前会话就拒。"""
    from omni_core.tools import memory_tool as mt
    memory_brain.index_l0_message("s_e1", "user", "秘密约定在老地方", context_key="group:500")
    memory_brain.index_l0_message("s_e2", "user", "别处的秘密约定", context_key="group:600")
    _ch_ctx.set_boundary(context_key="group:500", source_external=True)
    _ch_ctx.build_run(context_key="group:500")
    r = mt.conversation_search(keyword="秘密约定", scope="all")
    assert r["ok"] and r["scope"] == "current" and r["count"] == 1
    assert "老地方" in r["results"][0]
    _ch_ctx.set_boundary(source_external=True)
    _ch_ctx.build_run()
    mt.set_l0_task_context("")
    denied = mt.conversation_search(keyword="秘密约定", scope="all")
    assert denied["ok"] is False and "不允许跨会话检索" in denied["error"]


def test_conversation_search_rejects_empty_keyword(_ch_ctx, brain):
    from omni_core.tools import memory_tool as mt
    assert mt.conversation_search(keyword="")["ok"] is False



def test_no_legacy_backfill_entrypoints():
    """H-8 零兼容：一次性回填 / 叙事迁移入口与 meta 标记全部不存在。"""
    from omni_core import memory_brain as mb
    for name in ("backfill_l0_from_jsonl", "start_l0_backfill",
                 "migrate_narrative_once", "start_narrative_migration",
                 "_sid_project_map", "_diary_material_facts", "_meta_extra_keys"):
        assert not hasattr(mb, name), name
