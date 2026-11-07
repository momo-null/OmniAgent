"""Debug 记忆全景 / 监控指标 API（backend/api/routers/debug_api.py）。

全部只读端点在隔离的临时 brain.db + 临时 ~/.omniagent 下验收；主人控制面
（纠正 / 遗忘 / 物理删）复用 memory_brain 的既有原语，不新建写入路径。
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omni_core import memory_brain as mb
from omni_core.local import runtime_paths as rp


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "memory" / "brain.db"))
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    # 所有正文/任务目录都指到临时根，避免真机数据混进断言
    monkeypatch.setattr(rp, "_GLOBAL", tmp_path / "omni")
    from backend.api.routers.debug_api import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _seed_layers(tmp_path):
    """造齐 L0–L3 各层各一份样本。"""
    mb.remember("测试事实：主人偏好清蒸鱼", origin="test")
    mb.remember("测试事实：主人不喜欢香菜", origin="test")
    assert mb.upsert_narrative_index("diary-2026-11-01.md", "diary", project_id="",
                                     date_from="2026-11-01", date_to="2026-11-01",
                                     summary="那天去看了展", topics=["看展"],
                                     atmosphere="人挺多但开心")
    d = rp.global_diary()
    d.mkdir(parents=True, exist_ok=True)
    (d / "diary-2026-11-01.md").write_text("-----META-END-----\n那天去看了展，人挺多但开心。",
                                            encoding="utf-8")
    (d / "diary-month-2026-11.md").write_text("这个月看了展。", encoding="utf-8")
    rel = rp.global_relations()
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "u1.md").write_text("-----META-END-----\n甲，爱看展。", encoding="utf-8")
    rp.user_profile().parent.mkdir(parents=True, exist_ok=True)
    rp.user_profile().write_text("主人偏好清淡。", encoding="utf-8")
    rp.global_agents_file().parent.mkdir(parents=True, exist_ok=True)
    rp.global_agents_file().write_text("说人话。", encoding="utf-8")
    scenes = rp.project_dir("demo") / "memory" / "scene_blocks"
    scenes.mkdir(parents=True, exist_ok=True)
    (scenes / "topic.md").write_text("场景块正文", encoding="utf-8")
    sess = rp.project_dir("demo")
    (sess / "s_1.jsonl").write_text('{"role":"user","content":"你好"}\n', encoding="utf-8")
    conn = mb.open_brain()
    try:
        conn.execute("INSERT OR REPLACE INTO l0_messages"
                     "(record_id, context_key, session_id, kind, role, speaker, text, recorded_at) "
                     "VALUES(?,?,?,?,?,?,?,?)",
                     ("l0-1", "group:1", "s_1", "chat", "user", "甲", "你好呀", mb._now()))
        conn.execute("INSERT OR REPLACE INTO circle_note"
                     "(context_key, summary, topics, tone, source_count, version, updated) "
                     "VALUES(?,?,?,?,?,?,?)",
                     ("group:1", "群里聊看展", '["看展"]', "轻松", 3, 2, mb._now()))
        conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                     ("last_consolidation", "2026-11-01T10:00:00"))
        conn.commit()
    finally:
        conn.close()


def test_landscape_covers_all_layers(client, tmp_path):
    _seed_layers(tmp_path)
    r = client.get("/api/debug/memory/landscape")
    assert r.status_code == 200 and r.json()["ok"]
    d = r.json()
    brain = d["brain"]
    assert brain["total"] >= 2 and brain["l0"] == 1 and brain["narrative"] == 1
    assert brain["circle_notes"] == 1 and brain["by_kind"].get("fact", 0) >= 2
    assert "atoms_fts_gap" in d["health"] and "last_consolidation" in d["health"]
    assert d["l0"]["recent"][0]["text"] == "你好呀"
    assert d["l3"]["profile"]["exists"] and "主人偏好清淡" in d["l3"]["profile"]["preview"]
    assert d["context"]["notes"][0]["summary"] == "群里聊看展"
    assert "tables" in brain and "writer" in d
    kinds = {x["kind"] for x in d["documents"]}
    assert {"diary", "month", "relation", "scene", "profile", "agents", "session"} <= kinds
    # 叙事正文清单里不能把月记算成日记
    assert len([x for x in d["documents"] if x["kind"] == "diary"]) == 1


def test_atoms_full_pagination_and_filters(client):
    for i in range(5):
        mb.remember(f"测试事实：条目{i}号 关键词{'特殊' if i == 0 else '普通'}", origin="test")
    r = client.get("/api/debug/memory/atoms", params={"limit": 2, "offset": 0})
    assert r.status_code == 200 and r.json()["total"] == 5 and len(r.json()["items"]) == 2
    r2 = client.get("/api/debug/memory/atoms", params={"limit": 2, "offset": 2})
    assert [x["record_id"] for x in r.json()["items"]] != [x["record_id"] for x in r2.json()["items"]]
    r3 = client.get("/api/debug/memory/atoms", params={"q": "特殊"})
    assert r3.json()["total"] == 1 and "特殊" in r3.json()["items"][0]["content"]
    r4 = client.get("/api/debug/memory/atoms", params={"state": "active"})
    assert r4.json()["total"] == 5
    # dump_atoms 的旧字段之外，全量视图能看见审计链字段
    assert "superseded_by" in r.json()["items"][0]
    assert "created" in r.json()["items"][0]
    assert client.get("/api/debug/memory", params={"limit": 3}).json()["count"] == 3


def test_l0_and_narrative_views(client, tmp_path):
    _seed_layers(tmp_path)
    l0 = client.get("/api/debug/memory/l0", params={"q": "你好"}).json()
    assert l0["total"] == 1 and l0["items"][0]["speaker"] == "甲"
    assert client.get("/api/debug/memory/l0", params={"q": "没有"}).json()["total"] == 0
    n = client.get("/api/debug/memory/narrative", params={"kind": "diary"}).json()
    assert n["total"] == 1 and "text" not in n["items"][0]      # 只给指针不给正文
    assert client.get("/api/debug/memory/narrative", params={"kind": "month"}).json()["total"] == 0


def test_document_read_whitelist(client, tmp_path):
    _seed_layers(tmp_path)
    docs = client.get("/api/debug/memory/documents").json()["items"]
    assert docs and all("key" in x and "chars" in x for x in docs)
    r = client.get("/api/debug/memory/document", params={"key": "diary/diary-2026-11-01.md"})
    assert r.json()["ok"] and "看了展" in r.json()["text"]
    assert client.get("/api/debug/memory/document", params={"key": "profile/user_profile.md"}).json()["text"] == "主人偏好清淡。"
    assert client.get("/api/debug/memory/document", params={"key": "scene/demo/topic.md"}).json()["ok"]
    assert client.get("/api/debug/memory/document", params={"key": "session/demo/s_1.jsonl"}).json()["ok"]
    for bad in ("diary/../secret.txt", "nope/x", "diary//etc/passwd", "profile/../../config.py"):
        assert client.get("/api/debug/memory/document", params={"key": bad}).json()["ok"] is False
    assert client.get("/api/debug/memory/document").status_code == 422


def test_metrics_observation_and_telemetry(client, tmp_path):
    mb.log_recall_observability(project="demo", run_id="t1:r1", calls=3, hits=3,
                                by_tool={"memory_recall": 2, "narrative_search": 1},
                                context_chars=120, injected_context=True,
                                narrative_cross=1)
    mb.log_recall_observability(project="demo", run_id="t1:r2", calls=0, hits=0,
                                by_tool={}, context_chars=0)
    report = rp.tasks_root() / "t1" / "reports" / "r1.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"run_id": "r1", "success": True, "steps": 4,
                                  "metrics": {"success_rate": 1.0,
                                              "brain_intervention_rate": 0.25,
                                              "tool_efficiency": 0.75,
                                              "recovery_rate": 1.0}}), encoding="utf-8")
    r = client.get("/api/debug/memory/metrics")
    assert r.status_code == 200 and r.json()["ok"]
    d = r.json()
    obs = d["observation"]
    assert obs["runs"] == 2 and obs["calls"] == 3 and obs["hits"] == 3
    assert obs["zero_rate"] == 0.5 and obs["hit_rate"] == 1.0
    assert obs["by_tool"]["memory_recall"] == 2 and obs["narrative_cross"] == 1
    assert len(obs["daily"]) == 30 and obs["daily"][-1]["runs"] == 2
    assert d["telemetry"]["runs"] == 1 and d["telemetry"]["averages"]["tool_efficiency"] == 0.75
    assert d["store"]["active_atoms"] == 0 and "writer" in d
    assert isinstance(d["effect_report"]["available"], bool)


def test_owner_controls_and_validation(client):
    rid = mb.remember("测试事实：待纠正", origin="test")
    r = client.post("/api/debug/memory/correct", json={"record_id": rid, "new_text": "测试事实：已纠正"})
    assert r.status_code == 200 and r.json()["ok"]
    new_id = r.json()["new_record_id"]
    conn = mb.open_brain()
    try:
        old = conn.execute("SELECT state, superseded_by FROM atoms WHERE record_id=?",
                           (rid,)).fetchone()
    finally:
        conn.close()
    assert old[0] == "tombstone" and old[1] == new_id
    assert client.post("/api/debug/memory/forget", json={"record_id": new_id}).status_code == 200
    assert client.post("/api/debug/memory/forget", json={"record_id": new_id}).status_code == 404
    assert client.post("/api/debug/memory/correct", json={"record_id": "x", "new_text": " "}).status_code == 422
    assert client.post("/api/debug/memory/forget", json={}).status_code == 422
    assert client.post("/api/debug/memory/delete", json={"record_id": rid}).json()["ok"]
    assert client.post("/api/debug/memory/delete", json={}).status_code == 422
