"""社交日记（plan persona-social-memory M2）单测：日记块写入/导航窗口/维护隔离/巩固挂载。

不拉真实模型：llm_judge.chat_json 打桩；_isolated_home 隔离全局根（conftest autouse）。
"""
import json
import time

import pytest

from omni_core import memory_brain as mb
from omni_core.local import scene_executor as se


@pytest.fixture
def brain_cfg():
    return {"base_url": "http://x", "model": "m", "request": {"max_tokens": 100}}


def test_write_diary_block_and_idempotent():
    assert se.write_diary_block("qq", "2026-10-31", "今天被踩了语音，怼回去了") is True
    assert se.write_diary_block("qq", "2026-10-31", "再来一篇") is False   # 当日一篇幂等
    raw = se.read_scene_block("qq", "diary-2026-10-31.md")
    assert "被踩了语音" in raw
    assert se.write_diary_block("qq", "2026-10-31", "  ") is False        # 白卷不落盘
    assert se.write_diary_block("qq", "../../etc", "x") is False          # 日期非法拒绝


def test_nav_diary_window_and_task_blocks():
    for d in range(1, 10):
        se.write_diary_block("qq", f"2026-10-{d:02d}", f"第{d}天")
    se.write_diary_block("qq", "2026-10-31", "正文") if not True else None
    # 两个任务块（带正文，避免被清理逻辑误伤的歧义——这里只测导航）
    from omni_core.local.runtime_paths import project_dir

    ddir = project_dir("qq") / "memory" / "scene_blocks"
    ddir.mkdir(parents=True, exist_ok=True)
    for name in ("任务知识一.md", "任务知识二.md"):
        (ddir / name).write_text("-----META-----\nsummary: s\nheat: 9\n-----META-END-----\n正文\n",
                                 encoding="utf-8")
    se.sync_scene_index("qq")
    nav = se.load_scene_nav("qq")
    names = [e["filename"] for e in nav]
    diary_names = [n for n in names if n.startswith("diary-")]
    assert len(diary_names) == 7                          # 日记窗口=最近 7 篇
    assert "任务知识一.md" in names and "任务知识二.md" in names
    assert diary_names == sorted(diary_names, reverse=True)  # 日期倒序


def test_maintain_scenes_hides_diary(monkeypatch, brain_cfg):
    se.write_diary_block("qq", "2026-10-31", "日记正文")
    from omni_core.local.runtime_paths import project_dir

    ddir = project_dir("qq") / "memory" / "scene_blocks"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "任务块.md").write_text("-----META-----\nsummary: s\nheat: 1\n-----META-END-----\n正文\n",
                                    encoding="utf-8")
    se.sync_scene_index("qq")
    seen = {}

    def fake_run(brain_cfg_, system, user, tools, **kw):
        seen["user"] = user
        return "不需要动手"
    monkeypatch.setattr(se, "_run_agent", fake_run)
    se.maintain_scenes("qq", brain_cfg, "新证据：测试", stats={})
    u = seen["user"]
    assert "diary-2026-10-31.md" not in u                 # 维护 agent 看不见日记
    assert "场景总数:1" in u                               # 容量只数任务块


def test_scene_write_rejects_diary(brain_cfg):
    se.write_diary_block("qq", "2026-10-31", "正文")
    r = se._scene_dispatch("scene_write",
                           {"scene_file": "diary-2026-10-31.md", "content": "篡改"},
                           se.scene_blocks_dir("qq"))
    assert r["ok"] is False and "只读" in r["error"]


def test_consolidation_calls_diary_step(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "_fts_ok", lambda conn: True)   # 隔离库不探 FTS，直测第 8 步挂载
    calls = []
    monkeypatch.setattr(mb, "maintain_diary",
                        lambda cfg, persona="", day="": calls.append(day) or 0)
    mb._consolidate_impl({"base_url": "http://x", "model": "m"}, "")
    # 补齐模式（2026-11-03）：巩固期对回看窗口内每天各调一次（昨日优先）
    assert len(calls) == mb._DIARY_CATCHUP_DAYS
    assert calls[0] == mb._catchup_diary_days()[0]


def test_catchup_diary_days_window():
    """补齐窗口：昨日优先、共 7 天、不重不漏（D-1：日期为**本地日**）。"""
    from omni_core.local import localtime

    days = mb._catchup_diary_days()
    assert len(days) == mb._DIARY_CATCHUP_DAYS == 7
    assert days[0] == localtime.local_days_ago(1)   # 昨日（D+1 语义，本地日）
    assert days[-1] == localtime.local_days_ago(7)  # 超窗的旧账不管
    assert len(set(days)) == 7


def test_catchup_backfills_within_window_only(monkeypatch, brain_cfg, tmp_path):
    """缺日记且有素材的日子补写；超窗（>7 天）放弃；已有日记幂等不重写。"""
    import sys
    import types

    from omni_core.local import localtime, runtime_paths as rp

    stub = types.ModuleType("config")
    stub.get_config = lambda k, d=None: True
    monkeypatch.setitem(sys.modules, "config", stub)
    monkeypatch.setattr(mb, "enabled", lambda: True)
    # 隔离 brain.db：写链尾部的 upsert_narrative_index 会开库——不隔离就写进真实数据
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))

    d2 = localtime.local_days_ago(2)                        # 窗口内，缺日记
    d9 = localtime.local_days_ago(9)                        # 超窗：时效已过，放弃

    def _iso_for(day: str) -> str:
        """该本地日正午（本地时区）的 UTC ISO——保证 local_date(ts)==day 恒成立。"""
        from datetime import datetime, timezone
        dt = datetime.strptime(f"{day}T12:00:00", "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=localtime.local_tz())
        return dt.astimezone(timezone.utc).isoformat()

    sdir = rp.projects_root() / "qq"
    sdir.mkdir(parents=True, exist_ok=True)
    body = ("「张三」(1): 今天群里可热闹了，先是大肥鱼们为了一碗白米饭吵了一整个上午，"
            "下午又有谁踩了谁的语音被追着怼了半天，到了晚上大家还聊起剑网3梦江南的新副本，"
            "这条素材拉长是为了稳稳超过八十字的白卷门槛")
    (sdir / "s_t.jsonl").write_text(
        "\n".join(json.dumps({"ts": _iso_for(d), "role": "user", "content": body},
                             ensure_ascii=False) for d in (d2, d9)) + "\n",
        encoding="utf-8")
    llm_calls = []
    monkeypatch.setattr(mb.llm_judge, "chat_json",
                        lambda *a, **k: llm_calls.append(1) or {"diary": "这天鱼缸很热闹"})

    n = mb.maintain_diaries(brain_cfg, "")
    assert n == 1                                    # 只有 d2 成篇
    sblocks = se.diary_dir()                          # H-4：日记落全局叙事目录
    assert (sblocks / f"diary-{d2}.md").exists()
    assert not (sblocks / f"diary-{d9}.md").exists()  # 超窗不补
    assert len(llm_calls) == 1                        # 其余日子白卷/无素材，不调 LLM

    # 幂等：再跑一遍不再产出、不再调 LLM
    assert mb.maintain_diaries(brain_cfg, "") == 0
    assert len(llm_calls) == 1


def test_napcat_display_attribution(monkeypatch, tmp_path):
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    from gateway.channels.napcat import NapcatAdapter

    a = NapcatAdapter({"enabled": True,
                       "group_presence": {"enabled_groups": ["333"], "timers": False}})
    got = []
    ev = {"post_type": "message", "message_type": "group", "self_id": 111,
          "user_id": 222, "group_id": 333, "message_id": 900, "time": time.time(),
          "sender": {"nickname": "张三"},
          "message": [{"type": "at", "data": {"qq": 111}},
                      {"type": "text", "data": {"text": "在吗"}}]}
    a._handle_event(ev, got.append)
    assert got and got[0].display_text.startswith("「张三」(222): ")


def test_nav_includes_month_beyond_daily_window():
    for d in range(1, 9):
        se.write_diary_block("qq", f"2026-09-{d:02d}", f"九月{d}日")
    se.write_diary_block("qq", "month-2026-09", "九月地图行")
    names = [e["filename"] for e in se.load_scene_nav("qq")]
    assert "diary-month-2026-09.md" in names                # 月记全露（不受 7 篇窗口挤掉）
    diary_daily = [n for n in names if n.startswith("diary-2")]
    assert len(diary_daily) == 7


def test_diary_unified_across_projects(monkeypatch, brain_cfg, tmp_path):
    """H-4：同一天跨项目素材只成**一篇**全局日记、一次 LLM、索引裸 doc_id。"""
    import sys
    import types

    from omni_core.local import localtime, runtime_paths as rp

    stub = types.ModuleType("config")
    stub.get_config = lambda k, d=None: True
    monkeypatch.setitem(sys.modules, "config", stub)
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))

    day = localtime.local_days_ago(2)

    def _iso(hour: int) -> str:
        from datetime import datetime, timezone
        dt = datetime.strptime(f"{day}T{hour:02d}:00:00", "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=localtime.local_tz())
        return dt.astimezone(timezone.utc).isoformat()

    body = ("今天群里为了一碗白米饭吵了一上午，下午聊起游戏副本，晚上又测了几次响应速度；"
            "这条素材拉长是为了稳稳超过八十字的白卷门槛")
    pids = ("qq", "etf-analysis", "default")
    for idx, pid in enumerate(pids):
        d = rp.projects_root() / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"s_{pid}.jsonl").write_text(
            json.dumps({"ts": _iso(10 + idx), "role": "user",
                        "content": f"[{pid}] {body}"}, ensure_ascii=False) + "\n",
            encoding="utf-8")

    seen, systems = [], []

    def _fake_chat(cfg, system, user, **kw):
        seen.append(user)
        systems.append(system)
        return {"diary": "今天群里吵饭、又做了几份报告，晚上还测了响应速度。",
                "topics": ["群聊", "报告"]}

    monkeypatch.setattr(mb.llm_judge, "chat_json", _fake_chat)
    assert mb.maintain_diary(brain_cfg, "", day=day) == 1
    assert len(seen) == 1                                   # 一次 LLM，不是每项目一次
    assert all(f"[{pid}]" in seen[0] for pid in pids)      # 三处经历进同一份素材
    assert "一篇" in systems[0] and "按场合自然分段" in systems[0]
    assert [p.name for p in se.diary_dir().glob(f"diary-{day}.md")] == [f"diary-{day}.md"]
    for pid in pids:
        assert not (rp.project_dir(pid) / "memory" / "scene_blocks" / f"diary-{day}.md").exists()
    rows = mb.search_narrative(kind="diary", date_from=day, date_to=day)
    assert len(rows) == 1 and rows[0]["doc_id"] == f"diary-{day}.md"
    assert ":" not in rows[0]["doc_id"]                    # 不再有 pid: 前缀
    assert mb.read_narrative(rows[0]["doc_id"])["ok"]
    for pid in pids:                                       # 各项目看到同一份叙事
        names = [e["filename"] for e in se.load_scene_nav(pid)]
        assert f"diary-{day}.md" in names


def test_month_diary_unified_global(monkeypatch, brain_cfg, tmp_path):
    """H-4：月记也按月份全局一篇，不再按项目各压一份。"""
    import sys
    import types

    stub = types.ModuleType("config")
    stub.get_config = lambda k, d=None: True
    monkeypatch.setitem(sys.modules, "config", stub)
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    for d in (1, 2, 3):
        assert se.write_diary_block("", f"2026-09-{d:02d}", f"九月{d}日的经历。" + "内容" * 40)
    calls = []
    monkeypatch.setattr(mb.llm_judge, "chat_json",
                        lambda *a, **k: calls.append(1) or
                        {"summary": "九月总结", "month_diary": "这个月发生了不少事。"})
    assert mb.maintain_month_diary(brain_cfg, "") == 1
    assert len(calls) == 1
    assert (se.diary_dir() / "diary-month-2026-09.md").exists()
