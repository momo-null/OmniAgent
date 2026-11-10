"""H-8 收口回归锁：边界分层 / 零兼容 / 结构化 speaker / P5 消费端。

用户 2026-11-08 要求把全面 review 的五层全部落地，这些锁防止历史红线回流。
"""
import importlib.util
import json
import re
import sys
import types
from pathlib import Path

import pytest

from omni_core import memory_brain as mb
from omni_core.local import runtime_paths as rp

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mb, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_cfg",
                        lambda key, default=None: default if default is not None
                        else mb._LTM_DEFAULTS.get(key))
    mb._L0_CONN.update({"path": "", "conn": None})
    yield
    mb._L0_CONN.update({"path": "", "conn": None})


def _diary_stub(monkeypatch, tmp_path):
    stub = types.ModuleType("config")
    stub.get_config = lambda k, d=None: True
    monkeypatch.setitem(sys.modules, "config", stub)
    monkeypatch.setattr(mb, "enabled", lambda: True)
    diary = tmp_path / "diary"
    diary.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rp, "projects_root", lambda: tmp_path / "projects")
    monkeypatch.setattr(rp, "global_diary", lambda: diary)
    return tmp_path / "projects"


def test_kernel_never_imports_gateway_channels():
    """边界映射只能在网关层；内核不得反向 import 通道上下文。"""
    bad = []
    for p in sorted((ROOT / "omni_core").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "from gateway.channels import context" in line:
                bad.append(f"{p.relative_to(ROOT).as_posix()}:{i}")
    assert bad == [], bad


def test_kernel_has_no_scene_vocabulary():
    """INV-4：内核（含注释与 LLM prompt）不出现具体场合/渠道/平台词表。"""
    pat = re.compile(
        r"群聊|私聊|群名片|群里|群友|跨群|分群|名片\(uid\)|「名片」|"
        r"QQ|飞书|企业微信|抖音|B站|视频站|napcat|feishu|wecom|onebot|"
        r"startswith\(['\"]group|startswith\(['\"]private")
    bad = []
    for p in sorted((ROOT / "omni_core").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            m = pat.search(line)
            if m:
                bad.append(f"{p.relative_to(ROOT).as_posix()}:{i} {m.group(0)}")
    assert bad == [], bad


def test_no_legacy_migration_code(brain):
    """H-8：无一次性迁移 / ALTER 补列 / 旧 ID 兼容 / meta 标记闸门。"""
    for name in ("backfill_l0_from_jsonl", "start_l0_backfill",
                 "migrate_narrative_once", "start_narrative_migration",
                 "_sid_project_map", "_diary_material_facts", "_meta_extra_keys",
                 "_ensure_columns", "_ensure_peer_column",
                 "_ensure_atom_source_columns", "_ensure_l0_source_columns",
                 "_ensure_narrative_ext_columns", "_ensure_dead_dims_cleaned",
                 "_l0_speaker", "extract_peer"):
        assert not hasattr(mb, name), name
    assert not (ROOT / "scripts/migrate_diary_unify.py").exists()
    conn = mb.open_brain()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(atoms)").fetchall()}
        l0cols = {r[1] for r in conn.execute("PRAGMA table_info(l0_messages)").fetchall()}
    finally:
        conn.close()
    assert {"peer", "context_key", "source_kind", "observed_at", "provenance",
            "review_after"} <= cols                      # 列在 DDL，不靠 ALTER 补
    assert {"source_kind", "provenance", "source_external"} <= l0cols


def test_observation_search_reads_observation_lane_only(brain):
    """P5 消费端：见闻检索只查 source_kind=observation，并进同一本配额账本。"""
    from omni_core.tools import memory_tool as mt
    from omni_core.tools import observation_tool

    mb.capture_observation("group:9", "看到一篇讲检索的帖子",
                           provenance={"platform": "x", "ref": "h1"})
    mb.index_l0_message("s1", "user", "聊天里也提到检索", context_key="group:9")
    mt.reset_recall_stats()
    r = observation_tool.observation_search(keyword="检索")
    assert r["ok"] and r["count"] == 1
    assert "讲检索的帖子" in r["results"][0]
    assert mt.take_recall_stats()["calls"] == 1


def test_collector_capability_routes_to_unified_lane(brain):
    """采集器只暴露能力名，条目走 capture_observation 统一入口、不带渠道知识。"""
    from omni_core.tools import capability

    spec = importlib.util.spec_from_file_location(
        "collector_plugin", ROOT / "plugins" / "collector" / "plugin.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rid = mod.collect("group:9", "采集到一条见闻", {"platform": "x", "ref": "h2"})
    assert rid
    conn = mb.open_brain()
    try:
        row = conn.execute("SELECT source_kind, provenance FROM l0_messages "
                           "WHERE record_id=?", (rid,)).fetchone()
    finally:
        conn.close()
    assert row[0] == "observation" and "ref=h2" in row[1]
    mod.startup(None)
    assert capability.get_capability("observation_collector") is mod.collect
    mod.shutdown()
    assert capability.get_capability("observation_collector") is None


def test_revisit_atom_lines_only_marks_narrative(brain):
    """H-5：review_after 到期只产出 [回看] 素材，不判真假、不改状态。"""
    conn = mb.open_brain()
    mb._insert_atom(conn, "rid-r",
                    {"text": "见闻：某说法", "kind": "fact", "importance": 0.6,
                     "review_after": "2020-01-01"}, "", "t", mb._now())
    conn.commit()
    conn.close()
    lines = mb._revisit_atom_lines("2026-11-08")
    assert any("[回看]" in t and "某说法" in t for _, t in lines)
    conn = mb.open_brain()
    try:
        row = conn.execute("SELECT state, review_after FROM atoms "
                           "WHERE record_id='rid-r'").fetchone()
    finally:
        conn.close()
    assert row[0] == "active" and row[1] == "2020-01-01"


def test_diary_material_carries_structured_speaker(brain, monkeypatch, tmp_path):
    """P6-1：原话片段带结构化说话人，内核不解析名片版式。"""
    from omni_core.local import localtime

    projects = _diary_stub(monkeypatch, tmp_path)
    day = localtime.local_days_ago(1)
    pdir = projects / "d1"
    pdir.mkdir(parents=True)
    rows = [
        {"ts": f"{day}T09:00:00+00:00", "role": "user",
         "content": "早上聊了聊最近在看的资料，说起了检索和归档的差别，还提到上次那篇长文",
         "speaker": "张三(1)", "context_key": "group:1"},
        {"ts": f"{day}T09:10:00+00:00", "role": "assistant",
         "content": "我记得那篇长文讲的是记忆巩固，当时你还说想再看一遍"},
        {"ts": f"{day}T10:00:00+00:00", "role": "user",
         "content": "你今天都看什么了？说来听听，我也正想找点东西看",
         "speaker": "张三(1)", "context_key": "group:1"},
        {"ts": f"{day}T10:05:00+00:00", "role": "assistant",
         "content": "在看注意力经济那篇，里面说人的注意力被切成碎片是设计出来的"},
    ]
    (pdir / "s_1.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    seen = []
    monkeypatch.setattr(mb.llm_judge, "chat_json",
                        lambda *a, **k: seen.append(a[2]) or {"diary": "今天和张三聊了聊。"})
    mb.maintain_diary({"base_url": "http://x"}, day=day)
    assert seen and "[user] 张三(1):" in seen[0]
