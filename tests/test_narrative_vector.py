"""叙事层向量 + 索引自愈 + 入库去重的回归锁（2026-11-14，分支 feat/vector-recall）。

锁五件事：
1. 叙事层 doc 级向量：回填幂等、候选 JOIN 源表、kind/日期过滤照旧、
   `require_match=True` 时 OFF 为空 / ON 命中（语义补召回的落点）；
2. 删除清理：`delete_narrative_index` 连带删该条向量；
3. 读时修：索引行在、正文文件不在 → `read_narrative` 顺手撤索引 + 向量；
4. 后台扫：断链索引（限量）+ 孤儿向量（三通道）；
5. 去重 A（写路径精确重复）/ B（Jaccard，缺省不误合并） / C（cos 叠加）/ 冲突提示。

全部合成语料 + tmp 库 + 手工播种向量：不拉真实模型、不落真实目录、不读用户数据。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain as mb  # noqa: E402

MODEL = "test-model"


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mb, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "maybe_consolidate", lambda *a, **k: None)
    monkeypatch.setattr(mb, "_vec_model", lambda: MODEL)
    monkeypatch.setattr(mb, "_vec_enabled", lambda: True)
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    # 叙事正文解析不碰真实日记目录 / 关系卡目录
    from omni_core.local import scene_executor as se

    monkeypatch.setattr(se, "maybe_maintain", lambda *a, **k: None)
    monkeypatch.setattr(se, "read_scene_block", lambda *a, **k: "")
    return tmp_path


def _seed_vec(conn, source, rid, vec, text):
    v = np.asarray(vec, dtype="<f4")
    conn.execute(
        "INSERT OR REPLACE INTO vectors(source, record_id, vec, src_hash, model, dim, created) "
        "VALUES(?,?,?,?,?,?,?)",
        (source, rid, v.tobytes(), mb._vec_src_hash(text), MODEL, len(vec), "t"))


def _vec_at(th):
    return [float(np.cos(th)), float(np.sin(th))]


def _index(mod, summary, doc_id, kind="diary", **kw):
    return mod.upsert_narrative_index(doc_id, kind, summary=summary, **kw)


# ---- 1. 叙事向量：回填幂等 + 融合召回 ----

def test_narr_vector_catch_up_idempotent(brain, monkeypatch):
    _index(mb, "卧室布置了白色的窗帘和一张小书桌", "diary-2026-01-01.md")
    _index(mb, "河边钓鱼坐了一下午", "diary-2026-01-02.md")
    calls = []

    def fake_encode(texts):
        calls.append(list(texts))
        return [_vec_at(0.0) for _ in texts]

    monkeypatch.setattr(mb, "_vec_encode", fake_encode)
    n = mb._vec_narr_catch_up(10, MODEL)
    assert n == 2 and len(calls) == 1
    conn = mb.open_brain()
    rows = conn.execute("SELECT record_id, src_hash FROM vectors "
                        "WHERE source='narrative'").fetchall()
    assert {r[0] for r in rows} == {
        mb._narr_vec_id("diary", "diary-2026-01-01.md"),
        mb._narr_vec_id("diary", "diary-2026-01-02.md")}
    # 第二次：src_hash 全等 → 零欠账、不再调用嵌入
    assert mb._vec_narr_catch_up(10, MODEL) == 0 and len(calls) == 1
    conn.close()


def test_narr_vector_search_on_off(brain, monkeypatch):
    _index(mb, "卧室布置了白色的窗帘和一张小书桌", "diary-a.md",
           date_from="2026-01-01", date_to="2026-01-01")
    _index(mb, "河边钓鱼坐了一下午", "diary-b.md",
           date_from="2026-01-02", date_to="2026-01-02")
    conn = mb.open_brain()
    _seed_vec(conn, "narrative", mb._narr_vec_id("diary", "diary-a.md"), [1.0, 0.0],
              "卧室布置了白色的窗帘和一张小书桌")
    _seed_vec(conn, "narrative", mb._narr_vec_id("diary", "diary-b.md"), [0.0, 1.0],
              "河边钓鱼坐了一下午")
    conn.commit()
    conn.close()

    monkeypatch.setattr(mb, "_vec_encode", lambda texts: [[1.0, 0.0] for _ in texts])
    # 词面完全不重叠的转述问句：FTS 无命中
    q = "睡觉那个房间窗子挂的什么"
    on = mb.search_narrative(q, require_match=True)
    assert [r["doc_id"] for r in on] == ["diary-a.md"]
    # 关掉向量通道 → 逐字节旧行为（空结果，不是"随便给一条"）
    monkeypatch.setattr(mb, "_vec_ready", lambda: False)
    off = mb.search_narrative(q, require_match=True)
    assert off == []


def test_narr_vector_respects_kind_and_date_filter(brain, monkeypatch):
    _index(mb, "卧室布置了白色的窗帘", "d1.md", kind="diary", date_to="2026-01-01")
    _index(mb, "卧室布置了白色的窗帘", "r1.md", kind="relation")
    conn = mb.open_brain()
    _seed_vec(conn, "narrative", mb._narr_vec_id("diary", "d1.md"), [1.0, 0.0],
              "卧室布置了白色的窗帘")
    _seed_vec(conn, "narrative", mb._narr_vec_id("relation", "r1.md"), [1.0, 0.0],
              "卧室布置了白色的窗帘")
    conn.commit()
    conn.close()
    monkeypatch.setattr(mb, "_vec_encode", lambda texts: [[1.0, 0.0] for _ in texts])
    got = mb.search_narrative("窗子挂什么", kind="diary", require_match=True)
    assert [r["doc_id"] for r in got] == ["d1.md"]
    # 日期区间落在条目之外 → 向量候选也必须被挡（WHERE 下推，不是先取 top 再过滤）
    got2 = mb.search_narrative("窗子挂什么", kind="diary",
                              date_from="2026-02-01", require_match=True)
    assert [r["doc_id"] for r in got2] == []


# ---- 2. 删除清理 ----

def test_delete_narrative_index_removes_vector(brain):
    _index(mb, "某个场景块的摘要", "diary-c.md")
    conn = mb.open_brain()
    _seed_vec(conn, "narrative", mb._narr_vec_id("diary", "diary-c.md"), [1.0, 0.0],
              "某个场景块的摘要")
    conn.commit()
    conn.close()
    assert mb.delete_narrative_index("diary-c.md", "diary") is True
    conn = mb.open_brain()
    assert conn.execute("SELECT COUNT(*) FROM vectors WHERE source='narrative'"
                        ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM narrative_fts").fetchone()[0] == 0
    conn.close()


# ---- 3. 读时修 ----

def test_read_narrative_heals_broken_pointer(brain):
    _index(mb, "文件已经被手动删掉了", "diary-gone.md")
    conn = mb.open_brain()
    _seed_vec(conn, "narrative", mb._narr_vec_id("diary", "diary-gone.md"), [1.0, 0.0],
              "文件已经被手动删掉了")
    conn.commit()
    conn.close()
    res = mb.read_narrative("diary-gone.md", "diary")
    assert res["ok"] is False
    conn = mb.open_brain()
    assert conn.execute("SELECT COUNT(*) FROM narrative_index").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vectors WHERE source='narrative'"
                        ).fetchone()[0] == 0
    conn.close()
    # 幂等：再读一次不再报错、也不再动库
    assert mb.read_narrative("diary-gone.md", "diary")["ok"] is False


# ---- 4. 后台扫：断链 + 孤儿 ----

def test_reconcile_removes_only_missing(brain, monkeypatch):
    _index(mb, "还在的块", "keep.md")
    _index(mb, "没了的块", "drop.md")
    monkeypatch.setattr(mb, "_narr_body_exists",
                        lambda doc_id, kind, pid="": doc_id != "drop.md")
    stats = mb._reconcile_narrative_index(20)
    assert stats["scanned"] == 2 and stats["missing"] == 1 and stats["removed"] == 1
    conn = mb.open_brain()
    ids = {r[0] for r in conn.execute("SELECT doc_id FROM narrative_index")}
    conn.close()
    assert ids == {"keep.md"}


def test_prune_orphan_vectors_three_channels(brain):
    conn = mb.open_brain()
    conn.execute("INSERT OR REPLACE INTO atoms(record_id, content, kind, persona, origin, "
                 "source_task, importance, salience, access_count, last_access, state, "
                 "version, created, updated, peer, context_key, source_kind, observed_at, "
                 "provenance, review_after) VALUES('a1','有效原子','fact','','','',0.8,0.8,"
                 "0,'','active',1,'t','t','','','dialogue','','','')")
    _seed_vec(conn, "atom", "a1", [1.0, 0.0], "有效原子")
    _seed_vec(conn, "atom", "ghost", [1.0, 0.0], "已删原子")
    _seed_vec(conn, "l0", "ghost-l0", [1.0, 0.0], "已删原话")
    _seed_vec(conn, "narrative", mb._narr_vec_id("diary", "gone.md"), [1.0, 0.0], "已删叙事")
    conn.commit()
    conn.close()
    assert mb._prune_orphan_vectors(50) == 3
    conn = mb.open_brain()
    left = conn.execute("SELECT source, record_id FROM vectors").fetchall()
    conn.close()
    assert left == [("atom", "a1")]


# ---- 5. 去重 A/B/C + 冲突提示 ----

def test_dedupe_a_same_text_reuses_record(brain):
    rid1 = mb.remember("用户偏好先确认方案再动手", kind="preference")
    rid2 = mb.remember("用户偏好 先确认方案，再动手。", kind="preference")  # 归一化后全等
    assert rid1 and rid2 == rid1
    conn = mb.open_brain()
    assert conn.execute("SELECT COUNT(*) FROM atoms WHERE state='active'").fetchone()[0] == 1
    assert conn.execute("SELECT access_count FROM atoms WHERE record_id=?",
                        (rid1,)).fetchone()[0] == 1
    conn.close()


def test_dedupe_b_default_threshold_is_conservative(brain):
    mb.remember("用户喜欢在晚上十点以后写代码", kind="fact")
    mb.remember("用户喜欢在晚上十点之后写代码", kind="fact")
    # 这一对 Jaccard≈0.73（差一个词会连带挪动 3 个 bigram），缺省 0.85 下不该合并
    stats = mb.dedupe_atoms(apply=True)
    assert stats["pairs_b"] == 0 and stats["merged"] == 0
    conn = mb.open_brain()
    assert conn.execute("SELECT COUNT(*) FROM atoms WHERE state='active'").fetchone()[0] == 2
    conn.close()


def test_dedupe_b_merges_and_is_reversible(brain):
    mb.remember("用户喜欢在晚上十点以后写代码", kind="fact", importance=0.9)
    mb.remember("用户喜欢在晚上十点以后写代码了", kind="fact", importance=0.4)
    # Jaccard≈0.93 → 缺省 0.85 就该判为重复
    stats = mb.dedupe_atoms(apply=True)
    assert stats["merged"] == 1
    conn = mb.open_brain()
    # 注意：聚合必须 GROUP BY state——裸 state + COUNT(*) 是「任意行取值 + 总计数」，
    # 会给出 ('active', 2) 这种误导结果（2026-11-14 踩过）。
    states = dict(conn.execute(
        "SELECT state, COUNT(*) FROM atoms GROUP BY state").fetchall())
    assert states == {"active": 1, "tombstone": 1}
    dup = conn.execute("SELECT state, superseded_by FROM atoms WHERE state='tombstone'"
                       ).fetchone()
    assert dup[1]  # 指向 keeper，可追溯
    assert conn.execute("SELECT COUNT(*) FROM atoms_fts").fetchone()[0] == 1
    keeper = conn.execute("SELECT importance FROM atoms WHERE state='active'").fetchone()[0]
    assert abs(keeper - 0.9) < 1e-9   # 取 max，不是丢掉高重要度那条
    conn.close()


def test_dedupe_c_only_when_vector_ready(brain):
    rid_a = mb.remember("晚上睡不着觉", kind="fact")
    rid_b = mb.remember("深夜毫无睡意", kind="fact")
    conn = mb.open_brain()
    _seed_vec(conn, "atom", rid_a, [1.0, 0.0], "晚上睡不着觉")
    _seed_vec(conn, "atom", rid_b, [1.0, 0.0], "深夜毫无睡意")
    conn.commit()
    conn.close()
    # 字面 Jaccard 低 → B 不触发；cos=1.0 → C 触发
    stats = mb.dedupe_atoms(apply=False)
    assert stats["pairs_c"] >= 1
    monkeypatch_ready = mb._vec_ready
    assert monkeypatch_ready() is True
    stats2 = mb.dedupe_atoms(apply=True)
    assert stats2["merged"] == 1


def test_dedupe_conflict_hint_reports_without_merging(brain):
    rid_a = mb.remember("房间的窗帘是白色的", kind="fact")
    rid_b = mb.remember("客厅的灯是暖黄色的", kind="fact")
    conn = mb.open_brain()
    _seed_vec(conn, "atom", rid_a, [1.0, 0.0], "房间的窗帘是白色的")
    _seed_vec(conn, "atom", rid_b, _vec_at(0.55), "客厅的灯是暖黄色的")  # cos≈0.85
    conn.commit()
    conn.close()
    stats = mb.dedupe_atoms(apply=True)
    assert stats["merged"] == 0          # 语义近但字面差很多 → 只报告
    assert stats["conflicts"]             # 冲突候选非空（供人/模型判断）
    conn = mb.open_brain()
    assert conn.execute("SELECT COUNT(*) FROM atoms WHERE state='active'").fetchone()[0] == 2
    conn.close()
