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
    monkeypatch.setattr(mb, "maintain_diary", lambda cfg, persona="": calls.append(1) or 0)
    mb._consolidate_impl({"base_url": "http://x", "model": "m"}, "")
    assert calls


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
