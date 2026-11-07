"""P6-3（H-6）：主人可见/可控/可遗忘的本地入口（backend/api/routers/memory_api.py）。

不拉真实模型：memory_brain 原语直接落隔离库；TestClient 只挂 memory 路由。
验收（plan §16.5）：查看/纠正/删除可用；删除后 atoms_since 不回吐；tombstone 软删
不改写 content、审计可溯。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omni_core import memory_brain as mb


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    from backend.api.routers.memory_api import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_search_view_and_correct_forget(client):
    """查看 → 纠正（旧条软删+新条入库）→ 遗忘（软删）→ 视图不再出现。"""
    rid = mb.remember("测试事实：主人偏好清蒸鱼", origin="test")
    assert rid
    r = client.get("/memory/search", params={"query": "清蒸鱼"})
    assert r.status_code == 200 and r.json()["ok"]
    assert any(h["record_id"] == rid for h in r.json()["hits"])

    # 纠正：tombstone 旧条 + 新条入库，历史 content 不改写
    r = client.post(f"/memory/atoms/{rid}/correct",
                    json={"new_text": "测试事实：主人偏好清蒸鱼，尤其清蒸鲈鱼"})
    assert r.status_code == 200 and r.json()["ok"]
    new_id = r.json()["new_record_id"]
    assert new_id and new_id != rid
    conn = mb.open_brain()
    try:
        old = conn.execute("SELECT state, content, superseded_by FROM atoms "
                           "WHERE record_id=?", (rid,)).fetchone()
        new = conn.execute("SELECT state, content FROM atoms "
                           "WHERE record_id=?", (new_id,)).fetchone()
    finally:
        conn.close()
    assert old[0] == "tombstone" and old[1] == "测试事实：主人偏好清蒸鱼"
    assert old[2] == new_id                                    # 审计链：旧→新
    assert new[0] == "active"
    r = client.get("/memory/search", params={"query": "清蒸鱼"})
    ids = [h["record_id"] for h in r.json()["hits"]]
    assert new_id in ids and rid not in ids                    # 视图只出有效条

    # 遗忘：软删（保留内容与原因）、不物理删
    r = client.post(f"/memory/atoms/{new_id}/forget", json={"reason": "主人要求遗忘"})
    assert r.status_code == 200 and r.json()["ok"]
    conn = mb.open_brain()
    try:
        row = conn.execute("SELECT state, content, superseded_by FROM atoms "
                           "WHERE record_id=?", (new_id,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    finally:
        conn.close()
    assert row[0] == "tombstone" and row[1] == "测试事实：主人偏好清蒸鱼，尤其清蒸鲈鱼"
    assert "forget" in str(row[2]) and n == 2                  # 行还在（软删）
    r = client.get("/memory/search", params={"query": "清蒸鱼"})
    assert r.json()["hits"] == []
    r = client.post(f"/memory/atoms/{new_id}/forget", json={})
    assert r.status_code == 404                                # 重复遗忘


def test_forget_hidden_from_l2_feed(client):
    """删除后 atoms_since（L2 进料）不回吐被遗忘条目。"""
    rid = mb.remember("测试事实：待遗忘条目", origin="test")
    assert client.post(f"/memory/atoms/{rid}/forget", json={"reason": "t"}).json()["ok"]
    items = mb.atoms_since("")                                 # 游标空 = 全量起点
    assert all(it["record_id"] != rid for it in items)


def test_correct_validation_and_missing(client):
    r = client.post("/memory/atoms/nope/correct", json={"new_text": "x"})
    assert r.status_code == 404
    r = client.post("/memory/atoms/whatever/correct", json={"new_text": "  "})
    assert r.status_code == 422
    r = client.post("/memory/atoms/whatever/forget", json={})
    assert r.status_code == 404
    r = client.get("/memory/search", params={"query": ""})     # 空 query 合法返回空
    assert r.status_code == 200 and r.json()["hits"] == []


def test_narrative_view_and_read(client):
    """叙事查看：条目检索（无正文）+ 按需读全文（正文文件是唯一真相）。"""
    from omni_core.local import scene_executor as se

    assert mb.upsert_narrative_index("diary-2026-11-01.md", "diary", project_id="",
                                     date_from="2026-11-01", date_to="2026-11-01",
                                     summary="那天去看了展", topics=["看展"],
                                     atmosphere="人挺多但开心")
    r = client.get("/memory/narrative", params={"query": "看展"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert items and "text" not in items[0]                    # 条目不含正文
    # 无正文文件 → 404；有文件 → 读到全文
    assert client.get("/memory/narrative/read",
                      params={"doc_id": "diary-2026-11-01.md",
                              "kind": "diary"}).status_code == 404
    assert se.write_diary_block("", "2026-11-01", "那天去看了展，人挺多但开心。")
    r = client.get("/memory/narrative/read",
                   params={"doc_id": "diary-2026-11-01.md", "kind": "diary"})
    assert r.status_code == 200 and r.json()["ok"]
    assert "看了展" in r.json()["text"]
