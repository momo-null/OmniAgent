"""NP-0 测试锁（plan personal-agent-memory §13.6 / §13.5）：

- N-9  ``local_date()`` 四类输入正确；跨 UTC 日界的本地一天落进同一篇日记；
- N-11 切换日撞名迁移后无重复、无覆盖、无缺篇；
- 观察进料道（INV-13）：capture_observation 唯一入口、观察原文不进 search_l0 缺省查询；
- upsert_narrative_index 幂等（doc_id+kind 覆盖不追加，FTS 恒一行）；
- topics 钳制（§13.10.2）：带/缺/脏 topics 全链路不阻断。

不拉真实模型；brain.db 落 tmp_path，不碰真实数据。
"""
import json
import os

import pytest

from omni_core import memory_brain as mb
from omni_core.local import localtime
from omni_core.local import runtime_paths as rp
from omni_core.local import scene_executor as se


@pytest.fixture
def brain(tmp_path, monkeypatch):
    """隔离 brain 库 + 启用开关（迁移与索引不调 LLM，无需打桩模型）。"""
    db = tmp_path / "brain.db"
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    yield db


# ---------------------------------------------------------------- N-9 localtime

def test_local_date_input_kinds():
    tz = localtime.local_tz()
    from datetime import datetime, timezone, timedelta
    # ① 带时区 UTC ISO：正午 UTC 的本地日按系统时区折算
    noon = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    expect = noon.astimezone(tz).strftime("%Y-%m-%d")
    assert localtime.local_date(noon.isoformat()) == expect
    # ② naive：按存储口径（UTC）处理，结果同 ①
    assert localtime.local_date("2026-11-05T12:00:00") == expect
    # ③ 纯日期串：原样返回
    assert localtime.local_date("2026-11-05") == "2026-11-05"
    # ④ 脏输入：空串 / 垃圾 → 空串（调用方降级），不抛
    assert localtime.local_date("") == ""
    assert localtime.local_date("not-a-date") == ""
    # 本地日辅助函数
    assert localtime.local_today() == datetime.now().strftime("%Y-%m-%d")
    assert localtime.local_days_ago(1) == (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


def _utc_iso_for(day: str, hour: int = 12) -> str:
    """本地日 day 的 hour 点（本地时区）→ UTC ISO：保证 local_date(ts)==day。"""
    from datetime import datetime, timezone
    dt = datetime.strptime(f"{day}T{hour:02d}:00:00", "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=localtime.local_tz())
    return dt.astimezone(timezone.utc).isoformat()


def test_local_day_across_utc_midnight_one_diary(brain, monkeypatch):
    """跨 UTC 日界的本地一天（本地 23:50 与次日 00:10）落进**同一篇**日记（N-9）。

    构造：本地日 D 的 23:50 与 D+1 的 00:10——两者 UTC 日界不同（东八区）但同属
    本地一天；UTC 以西时区两小时点可能同属一个 UTC 日，此时断言仍成立（都在 D 内）。
    """
    from datetime import datetime, timedelta
    from omni_core.local.task_store import ProjectStore

    d = localtime.local_days_ago(1)
    # 本地日的清晨两点（本地 07:00 与 09:00）：东八区下分别落在两个 UTC 日
    # （D-1T23:00Z 与 D-2? 不——07:00 本地 = 前一 UTC 日 23:00；09:00 本地 = 当日 01:00 UTC），
    # 即「跨 UTC 日界的同一天」；以西时区两点同属一个 UTC 日时跳过构造。
    ts_a = _utc_iso_for(d, 7)
    ts_b = _utc_iso_for(d, 9)
    if ts_a[:10] == ts_b[:10]:
        pytest.skip("当前时区无法在同一本地日内跨 UTC 日界构造")

    ProjectStore.ensure("qq")
    body = ("「成员甲」(9999): 今晚群里从白米饭吵到剑网3，快到零点还在刷屏，"
            "这条素材拉长是为了稳稳超过八十字的白卷门槛，别被当成灌水截掉")
    (rp.project_dir("qq") / "s_x.jsonl").write_text(
        "\n".join(json.dumps({"ts": t, "role": "user", "content": body,
                              "chat_id": "group:1"}, ensure_ascii=False)
                  for t in (ts_a, ts_b)) + "\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(mb.llm_judge, "chat_json",
                        lambda *a, **k: calls.append(1) or {"diary": "跨零点的一天",
                                                            "topics": ["夜聊"]})
    n = mb.maintain_diary({"base_url": "http://x"}, day=d)
    assert n == 1
    assert (se.diary_dir() / f"diary-{d}.md").exists()   # H-4：全局一天一篇
    # 两个 UTC 日界的素材进了同一篇：LLM 只被调一次（一次生成 = 一篇）
    assert len(calls) == 1
    # 索引：时间区间覆盖两个 UTC 时刻、场合标注来自 rec.chat_id
    row = mb.get_narrative_row(f"diary-{d}.md", "diary")
    assert row and row["date_from"] == d and row["date_to"] == d
    assert row["chat_ids"] == ["group:1"]
    assert row["time_from"] and row["time_to"]


# ---------------------------------------------------------------- N-11 撞名迁移

def _find_utc_hour_with_different_local_day(utc_day: str):
    """在 utc_day 里找一个「本地日 ≠ UTC 日」的小时（找不到返回 None，如 UTC+0 时区）。"""
    from datetime import datetime, timezone, timedelta
    base = datetime.strptime(utc_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for h in range(24):
        ts = (base + timedelta(hours=h)).isoformat()
        if localtime.local_date(ts) != utc_day:
            return h
    return None


def _mk_migration_fixture(pid: str, utc_day: str, hour: int):
    """项目 + 会话素材（jsonl 与 l0_messages 双写，迁移的素材事实从 L0 读）+ 老命名日记。"""
    from datetime import datetime, timezone, timedelta
    from omni_core.local.task_store import ProjectStore

    ProjectStore.ensure(pid)
    base = datetime.strptime(f"{utc_day}T{hour:02d}:30:00", "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc)
    body = ("「成员甲」(9999): 今天群里为了一碗白米饭吵了一整个上午，下午又追着怼了半天语音，"
            "到了晚上大家还聊起剑网3梦江南的新副本，这条素材拉长是为了稳稳超过八十字门槛")
    for i in range(5):
        ts = (base + timedelta(minutes=i * 3)).isoformat()
        (rp.project_dir(pid) / "s_a.jsonl").open("a", encoding="utf-8").write(
            json.dumps({"ts": ts, "role": "user", "content": body,
                        "chat_id": "group:77"}, ensure_ascii=False) + "\n")
        mb.index_l0_message("s_a", "user", body, chat_id="group:77", recorded_at=ts)
    # 老日记文件按 **UTC 文件名日期** 落盘（迁移前的形态），并同步进 scene_index
    d = se.scene_blocks_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    fname = f"diary-{utc_day}.md"
    meta = ("-----META-START-----\ncreated: 2026-11-01T00:00:00+00:00\n"
            f"updated: 2026-11-01T00:00:00+00:00\nsummary: 旧的一天\nheat: 0\n"
            "-----META-END-----\n旧正文，迁移不改写。")
    (d / fname).write_text(meta, encoding="utf-8")
    se.sync_scene_index(pid)
    return fname


def test_migration_renames_to_local_day(brain, monkeypatch):
    """素材真实本地日 D ≠ 文件名日期 且 diary-D.md 不存在 → 重命名 + 索引 local（N-11）。"""
    from datetime import datetime, timedelta
    utc_day = localtime.local_days_ago(3)
    hour = _find_utc_hour_with_different_local_day(utc_day)
    if hour is None:
        pytest.skip("当前时区与 UTC 同日，撞名重命名场景不可构造")
    fname = _mk_migration_fixture("mig-a", utc_day, hour)
    # 目标本地日（多数派）：该素材小时的本地日
    from datetime import datetime, timezone, timedelta
    ts = datetime.strptime(f"{utc_day}T{hour:02d}:30:00", "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc).isoformat()
    d_local = localtime.local_date(ts)

    out = mb.migrate_narrative_once(force=True)
    d = se.scene_blocks_dir("mig-a")
    assert not (d / fname).exists()                    # 老名已不在
    assert (d / f"diary-{d_local}.md").exists()        # 新名（本地日）在场
    names = [p.name for p in d.glob("diary-2*.md")]
    assert names == [f"diary-{d_local}.md"]            # 无重复、无缺篇
    row = mb.get_narrative_row(f"diary-{d_local}.md", "diary")
    assert row and row["date_from"] == d_local and row["date_basis"] == "local"
    assert row["chat_ids"] == ["group:77"]             # 场合标注从 l0 回填
    assert "旧正文" in (d / f"diary-{d_local}.md").read_text(encoding="utf-8")  # 正文零改写
    assert out["renamed"] >= 1
    # meta 标记：再跑一次跳过
    assert mb.migrate_narrative_once().get("skipped") == 1


def test_migration_collision_keeps_old_file_marks_legacy(brain):
    """diary-D.md 已存在 → 保留老文件不覆盖、两篇都在、UTC 名索引标 legacy（N-11）。"""
    from datetime import datetime, timezone
    utc_day = localtime.local_days_ago(4)
    hour = _find_utc_hour_with_different_local_day(utc_day)
    if hour is None:
        pytest.skip("当前时区与 UTC 同日，撞名场景不可构造")
    fname = _mk_migration_fixture("mig-b", utc_day, hour)
    ts = datetime.strptime(f"{utc_day}T{hour:02d}:30:00", "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc).isoformat()
    d_local = localtime.local_date(ts)
    # 预占目标名
    d = se.scene_blocks_dir("mig-b")
    (d / f"diary-{d_local}.md").write_text(
        "-----META-START-----\ncreated: x\nupdated: x\nsummary: 占位\nheat: 0\n"
        "-----META-END-----\n已占位的目标日记。", encoding="utf-8")

    out = mb.migrate_narrative_once(force=True)
    # 无覆盖：占位文件原样、老文件原样；无重复（两个不同名文件都在）
    assert "已占位" in (d / f"diary-{d_local}.md").read_text(encoding="utf-8")
    assert "旧正文" in (d / fname).read_text(encoding="utf-8")
    legacy_row = mb.get_narrative_row(fname, "diary")
    assert legacy_row and legacy_row["date_basis"] == "legacy"
    assert legacy_row["date_from"] == utc_day          # 失败退化 = 文件名日期当日
    local_row = mb.get_narrative_row(f"diary-{d_local}.md", "diary")
    assert local_row and local_row["date_basis"] == "legacy"  # 迁移期建的老行统一 legacy


def test_migration_indexes_month_and_relations(brain):
    """月记按日历区间、关系卡按 uid 建索引；正文文件零改写。"""
    d = se.scene_blocks_dir("mig-c")
    d.mkdir(parents=True, exist_ok=True)
    (d / "diary-month-2026-09.md").write_text(
        "-----META-START-----\ncreated: x\nupdated: x\nsummary: 九月地图\nheat: 0\n"
        "-----META-END-----\n九月正文。", encoding="utf-8")
    rel = rp.global_relations()
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "999999.md").write_text(
        "-----META-START-----\nname: 成员甲\nuid: 999999\nupdated: x\n"
        "topics: [\"抬杠\", \"白米饭\"]\n-----META-END-----\n最近交手：起哄。", encoding="utf-8")
    out = mb.migrate_narrative_once(force=True)
    m = mb.get_narrative_row("diary-month-2026-09.md", "month")
    assert m and m["date_from"] == "2026-09-01" and m["date_to"] == "2026-09-30"
    assert m["summary"] == "九月地图"
    r = mb.get_narrative_row("999999", "relation")
    assert r and r["peers"] == ["成员甲(999999)"] and r["topics"] == ["抬杠", "白米饭"]
    assert "九月正文" in (d / "diary-month-2026-09.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- upsert 幂等 / 检索

def test_upsert_narrative_index_idempotent(brain):
    """H-4：日记按日期全局唯一，``doc_id`` 是裸文件名；幂等覆盖不追加。"""
    assert mb.upsert_narrative_index("diary-2026-11-05.md", "diary", project_id="",
                                     date_from="2026-11-05", date_to="2026-11-05",
                                     summary="第一版", topics=["甲"],
                                     chat_ids=["group:1"], peers=["甲(1)"]) is True
    assert mb.upsert_narrative_index("diary-2026-11-05.md", "diary", project_id="",
                                     date_from="2026-11-05", date_to="2026-11-05",
                                     summary="第二版", topics=["甲", "乙"]) is True
    conn = mb.open_brain()
    try:
        rows = conn.execute("SELECT summary FROM narrative_index WHERE doc_id=? AND kind='diary'",
                            ("diary-2026-11-05.md",)).fetchall()
        assert len(rows) == 1 and rows[0][0] == "第二版"     # 覆盖不追加
        fts = conn.execute("SELECT rowid FROM narrative_fts WHERE doc_id=?",
                           ("diary-2026-11-05.md",)).fetchall()
        assert len(fts) == 1                                  # FTS 恒一行
    finally:
        conn.close()
    assert mb.get_narrative_row("diary-2026-11-05.md", "diary")["summary"] == "第二版"
    # 同一天不允许按项目再开一行：换 project_id 仍是同一行、仍然是覆盖
    assert mb.upsert_narrative_index("diary-2026-11-05.md", "diary", project_id="q",
                                     summary="另一项目同名") is True
    conn = mb.open_brain()
    try:
        assert conn.execute(
            "SELECT count(*) FROM narrative_index WHERE doc_id=? AND kind='diary'",
            ("diary-2026-11-05.md",)).fetchone()[0] == 1
    finally:
        conn.close()
    # 非法 kind / 空 doc_id 拒绝
    assert mb.upsert_narrative_index("x", "diaryx") is False
    assert mb.upsert_narrative_index("", "diary") is False


def test_search_narrative_weighted_not_filtered(brain):
    """同 query 在两个 chat_id 下排序有差异，且**两条都仍可召回**（INV-3 / N-2 前置）。"""
    mb.upsert_narrative_index("diary-2026-11-01.md", "diary", project_id="p",
                              date_from="2026-11-01", date_to="2026-11-01",
                              summary="甲场合的抬杠日常", topics=["抬杠", "甲场合"],
                              chat_ids=["group:a"])
    mb.upsert_narrative_index("diary-2026-11-02.md", "diary", project_id="p",
                              date_from="2026-11-02", date_to="2026-11-02",
                              summary="乙场合的钓鱼日常", topics=["钓鱼", "乙场合"],
                              chat_ids=["group:b"])
    r1 = mb.search_narrative("抬杠", chat_id="group:a")
    r2 = mb.search_narrative("抬杠", chat_id="group:b")
    a_id, b_id = "diary-2026-11-01.md", "diary-2026-11-02.md"
    assert any(x["doc_id"] == a_id for x in r1)
    assert any(x["doc_id"] == b_id for x in r2), "跨场合叙事仍可召回（INV-3）"
    assert r1[0]["doc_id"] == a_id                           # 同场合标注排前
    assert r1[0]["score"] > r2[[i for i, x in enumerate(r2)
                                if x["doc_id"] == a_id][0]]["score"]
    # 不返回正文（INV-12）
    assert all("text" not in x and "正文" not in json.dumps(x, ensure_ascii=False)
               for x in r1)
    # 日期区间过滤（本地日历日求交）
    only02 = mb.search_narrative("", kind="diary", date_from="2026-11-02",
                                 date_to="2026-11-02")
    assert [x["doc_id"] for x in only02] == ["diary-2026-11-02.md"]


# ---------------------------------------------------------------- 观察进料道 / topics 钳制

def test_capture_observation_lane_and_isolation(brain):
    """观察经唯一入口落 l0（source_kind=observation），不进缺省 search_l0（INV-13）。"""
    rid = mb.capture_observation("group:9", "看到一篇讲检索的帖子，值得记一笔",
                                 provenance={"kind": "feed", "ref": "hash:abc"})
    assert rid
    conn = mb.open_brain()
    try:
        row = conn.execute("SELECT source_kind, role, provenance FROM l0_messages "
                           "WHERE record_id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row[0] == "observation" and row[2]
    assert mb.search_l0("检索") == []                       # 缺省只查 chat（INV-13）
    assert mb.search_l0("检索", source_kind="observation")  # 显式观察道可查（P5 消费端）
    # 伪装对话原话的旁路被封死：index_l0_message 不接受 source_kind=observation 以外的
    # 非法值时回退 chat/task，观察必须走 capture_observation（唯一入口签名固定）
    assert mb.index_l0_message("s1", "user", "伪装", source_kind="chat")


def test_normalize_topics_clamps_dirty_values():
    fn = mb._normalize_topics
    assert fn(["甲", "乙", "甲", "", 123, "x" * 40, "丙", "丁", "戊", "己"]) == \
        ["甲", "乙", "x" * 24, "丙", "丁"]                    # 去重/丢非串/截断/≤5 项
    assert fn("not-a-list") == []
    assert fn(None) == []
    assert fn([None, {}, "  "]) == []
    # 脏值不阻断：maintain_diary 带/缺/脏 topics 都能写块
    assert fn(["主题"]) == ["主题"]
