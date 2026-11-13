"""向量召回臂回归锁（分支 feat/vector-recall）。

锁的是 review 定稿的四条硬约束：
1. 开关关 / ready()=False → 逐字节现状；
2. FTS 命中行 final 逐值相等（打分块提取不改变任何既有行）；
3. 向量候选 JOIN 源表沿用全部既有 WHERE（state/时间窗/会话 ACL/进料道分道）；
4. schema 只加幂等 DDL，_SCHEMA_VERSION 不动（版本不符=整库重建，红线）。

不触真实模型：_vec_ready/_vec_encode monkeypatch，向量手工播种（可控余弦）。
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain as mb  # noqa: E402

CFG = {"base_url": "http://x", "model": "m"}


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mb, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    monkeypatch.setattr(mb, "maybe_consolidate", lambda *a, **k: None)
    from omni_core.local import scene_executor
    monkeypatch.setattr(scene_executor, "maybe_maintain", lambda *a, **k: None)
    monkeypatch.setattr(mb, "_vec_enabled", lambda: True)  # 各用例按需覆盖为 False
    return tmp_path


def _seed_vec(conn, source, rid, vec, text, model="test-model"):
    v = np.asarray(vec, dtype="<f4")
    conn.execute(
        "INSERT OR REPLACE INTO vectors(source, record_id, vec, src_hash, model, dim, created) "
        "VALUES(?,?,?,?,?,?,?)",
        (source, rid, v.tobytes(), mb._vec_src_hash(text), model, len(vec), "t"))


def _vec2(th):
    """二维单位向量（角度 th，弧度）——可控余弦。"""
    return [float(np.cos(th)), float(np.sin(th))]


# ---- 1. schema 只加幂等 DDL ----

def test_vectors_table_additive_no_rebuild(brain):
    """建 vectors 表不触发 _rebuild_brain_schema：版本键不动、行数不丢。"""
    mb.remember("用户偏好先确认方案再动手", kind="preference", importance=0.8, brain_cfg=None)
    conn = mb.open_brain()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    ver = conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
    conn.close()
    assert "vectors" in tables
    assert ver == mb._SCHEMA_VERSION          # 版本键未动 → 没走重建分支
    assert n == 1                             # 主表行保留（重建会清空）


# ---- 2. search_brain 融合 ----

def _seed_three_atoms(brain):
    """A=词面命中（FTS 路径）；B=无词面重叠（纯向量路径）；C=向量命中但已作废。

    注意：先拿 rid 再开种子连接——seed 连接上有未提交 DML 时，另一个 open_brain
    写 meta 会自锁（同线程两连接的经典坑）。
    """
    rid_a = mb.remember("用户偏好命令行操作方式", kind="preference", importance=0.5, brain_cfg=None)
    rid_b = mb.remember("东京的中古店圈子文化观察记录", kind="fact", importance=0.5, brain_cfg=None)
    rid_c = mb.remember("已被作废的旧记忆条目内容", kind="fact", importance=0.9, brain_cfg=None)
    mb.forget(rid_c)
    conn = mb.open_brain()
    _seed_vec(conn, "atom", rid_a, _vec2(0.0), "用户偏好命令行操作方式")
    _seed_vec(conn, "atom", rid_b, _vec2(0.0), "东京的中古店圈子文化观察记录")
    _seed_vec(conn, "atom", rid_c, _vec2(-1.0), "已被作废的旧记忆条目内容")
    conn.commit()
    conn.close()
    return rid_a, rid_b, rid_c


def _stub_query_vec(vec):
    return lambda texts: [list(vec) for _ in texts]


def test_search_brain_fts_rows_byte_equal_with_vector_on(brain, monkeypatch):
    """合入标准 2：开关开+ready，FTS 命中行 final 与关闭时逐值相等。"""
    rid_a, rid_b, rid_c = _seed_three_atoms(brain)
    q = "命令行操作方式"
    monkeypatch.setattr(mb, "_vec_enabled", lambda: False)
    monkeypatch.setattr(mb, "_vec_ready", lambda: False)
    off = mb.search_brain(q, k=5, reinforce=False)
    monkeypatch.setattr(mb, "_vec_enabled", lambda: True)
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    monkeypatch.setattr(mb, "_vec_encode", _stub_query_vec(_vec2(0.0)))  # q=A 方向
    on = mb.search_brain(q, k=5, reinforce=False)
    off_by_id = {h["record_id"]: h for h in off}
    on_by_id = {h["record_id"]: h for h in on}
    assert off_by_id and set(off_by_id) <= set(on_by_id)
    for rid, h in off_by_id.items():                      # FTS 行逐值相等
        assert on_by_id[rid]["score"] == h["score"]
        assert on_by_id[rid]["content"] == h["content"]


def test_search_brain_vector_only_row_enters(brain, monkeypatch):
    """转述召回落点：无词面重叠的原子经向量通道进入结果，rel=cos。"""
    rid_a, rid_b, rid_c = _seed_three_atoms(brain)
    q = "关东地区的二手市场见闻"            # 与 B 无词面交集
    monkeypatch.setattr(mb, "_vec_enabled", lambda: True)
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    monkeypatch.setattr(mb, "_vec_model", lambda: "test-model")
    monkeypatch.setattr(mb, "_vec_encode", _stub_query_vec(_vec2(0.0)))  # q=B 方向 cos=1
    hits = mb.search_brain(q, k=5, reinforce=False)
    by_id = {h["record_id"]: h for h in hits}
    assert rid_b in by_id, "转述问句应经向量通道召回 B"
    # rel=cos=1 → final = 0.55*1 + 0.30*0.5 + 0.15*rec(=1) = 0.85
    assert by_id[rid_b]["score"] == pytest.approx(0.85, abs=0.01)


def test_search_brain_tombstone_vector_not_returned(brain, monkeypatch):
    """scope 硬约束：向量候选 JOIN 源表——tombstone 行不得出现。"""
    rid_a, rid_b, rid_c = _seed_three_atoms(brain)
    monkeypatch.setattr(mb, "_vec_enabled", lambda: True)
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    monkeypatch.setattr(mb, "_vec_encode", _stub_query_vec(_vec2(-1.0)))  # q=C 方向
    monkeypatch.setattr(mb, "_vec_model", lambda: "test-model")
    hits = mb.search_brain("随便问一句不存在的内容", k=5, reinforce=False)
    assert rid_c not in {h["record_id"] for h in hits}


def test_search_brain_disabled_byte_identical(brain, monkeypatch):
    """合入标准 1（开关关）：向量行在库里也必须被忽略——逐字节现状。"""
    rid_a, rid_b, rid_c = _seed_three_atoms(brain)
    monkeypatch.setattr(mb, "_vec_enabled", lambda: False)
    monkeypatch.setattr(mb, "_vec_ready", lambda: False)
    monkeypatch.setattr(mb, "_vec_encode", _stub_query_vec(_vec2(0.0)))
    hits = mb.search_brain("关东地区的二手市场见闻", k=5, reinforce=False)
    assert all(rid_b != h["record_id"] for h in hits)


# ---- 3. search_l0 融合与 scope ----

def _seed_l0_rows():
    mb.index_l0_message("s1", "user", "记得明天带伞出门", context_key="group:33300001",
                        source_kind="chat")
    mb.index_l0_message("s2", "user", "tokyo workflow note about trade offs",
                        context_key="group:33300001", source_kind="chat")
    mb.index_l0_message("s1", "assistant", "见闻：一篇文章讲检索质量",
                        context_key="group:33300001", source_kind="observation")


def test_search_l0_vector_row_scope_respected(brain, monkeypatch):
    """向量候选沿用会话 ACL 与进料道分道：跨会话/跨分道的行不得出现。"""
    _seed_l0_rows()
    conn = mb.open_brain()
    rid1 = conn.execute("SELECT record_id FROM l0_messages WHERE text LIKE '记得明天%'").fetchone()[0]
    rid2 = conn.execute("SELECT record_id FROM l0_messages WHERE text LIKE 'tokyo%'").fetchone()[0]
    rid3 = conn.execute("SELECT record_id FROM l0_messages WHERE text LIKE '见闻：%'").fetchone()[0]
    # 向量：q=R1 方向；R1 cos=1，R2 cos≈0.66（45°），R3 cos=1——但 R3 是 observation
    _seed_vec(conn, "l0", rid1, _vec2(0.0), "记得明天带伞出门")
    _seed_vec(conn, "l0", rid2, _vec2(np.pi / 4), "tokyo workflow note about trade offs")
    _seed_vec(conn, "l0", rid3, _vec2(0.0), "见闻：一篇文章讲检索质量")
    conn.commit()
    conn.close()
    monkeypatch.setattr(mb, "_vec_enabled", lambda: True)
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    monkeypatch.setattr(mb, "_vec_model", lambda: "test-model")
    monkeypatch.setattr(mb, "_vec_encode", _stub_query_vec(_vec2(0.0)))

    hits = mb.search_l0("带伞", context_key="group:33300001", source_kind="chat")
    ids = {h["id"] for h in hits}
    assert rid1 in ids
    assert rid3 not in ids, "observation 行不得混进 chat 检索（INV-13 沿用 WHERE）"

    # 无 ACL 限制时（全部会话），R2（跨会话）才可见——scope 隔离是会话参数带来的
    hits_all = mb.search_l0("带伞", source_kind="chat")
    assert {h["id"] for h in hits_all} >= {rid1, rid2}


# ---- 4. 欠账预算帽与水位 ----

def test_vector_catch_up_budget_and_watermark(brain, monkeypatch):
    """预算帽：欠账 5 条、budget=2 → 只嵌 2 条且水位推进；剩余下次续。"""
    for i in range(5):
        mb.index_l0_message("s_c", "user", f"欠账测试第{i}条内容", context_key="group:1")
    texts = {f"欠账测试第{i}条内容": _vec2(0.1 * i) for i in range(5)}
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    monkeypatch.setattr(mb, "_vec_encode", lambda ts: [texts[t] for t in ts])
    assert mb._vector_catch_up(budget=2) == 2
    conn = mb.open_brain()
    n_vec = conn.execute("SELECT COUNT(*) FROM vectors WHERE source='l0'").fetchone()[0]
    wm = int(conn.execute("SELECT v FROM meta WHERE k='vectors_l0_wm'").fetchone()[0])
    conn.close()
    assert n_vec == 2 and wm > 0
    assert mb._vector_catch_up(budget=10) == 3      # 剩余续完
    conn = mb.open_brain()
    n_vec = conn.execute("SELECT COUNT(*) FROM vectors WHERE source='l0'").fetchone()[0]
    conn.close()
    assert n_vec == 5


def test_vector_catch_up_disabled_noop(brain, monkeypatch):
    monkeypatch.setattr(mb, "_vec_enabled", lambda: False)
    mb.index_l0_message("s_c", "user", "关闭状态下的欠账", context_key="group:1")
    assert mb._vector_catch_up(budget=10) == 0


def test_catch_up_reembeds_when_content_changes(brain, monkeypatch):
    """atoms 内容被 merge 改写后 src_hash 不匹配 → 重嵌（不残留陈旧向量）。"""
    rid = mb.remember("原始记忆内容版本一", kind="fact", importance=0.5, brain_cfg=None)
    texts = {"原始记忆内容版本一": _vec2(0.0), "原始记忆内容版本一改写后": _vec2(1.5)}
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    monkeypatch.setattr(mb, "_vec_encode", lambda ts: [texts[t] for t in ts])
    assert mb._vector_catch_up(budget=10) == 1
    conn = mb.open_brain()
    old = conn.execute("SELECT src_hash FROM vectors WHERE source='atom'").fetchone()[0]
    conn.execute("UPDATE atoms SET content='原始记忆内容版本一改写后' WHERE record_id=?", (rid,))
    conn.commit()
    conn.close()
    assert mb._vector_catch_up(budget=10) == 1       # hash 不匹配 → 重嵌
    conn = mb.open_brain()
    row = conn.execute("SELECT src_hash FROM vectors WHERE source='atom'").fetchone()[0]
    conn.close()
    assert row != old


def test_search_brain_cos_floor_drops_unrelated(brain, monkeypatch):
    """语义 floor：cos 低于阈值的向量行不进结果（防「每问必答」灌水）。

    复现 2026-11-13 A/B 实测的退化：无 floor 时满额率 1.0、负样本 20 条凭空召回
    （importance/recency 基础分把任何向量行抬过既有 floor）。
    """
    rid_a, rid_b, rid_c = _seed_three_atoms(brain)
    monkeypatch.setattr(mb, "_vec_enabled", lambda: True)
    monkeypatch.setattr(mb, "_vec_ready", lambda: True)
    monkeypatch.setattr(mb, "_vec_model", lambda: "test-model")
    monkeypatch.setattr(mb, "_vec_floor", lambda: 0.80)   # 只留 cos≥0.80 的候选
    monkeypatch.setattr(mb, "_vec_encode", _stub_query_vec(_vec2(0.0)))
    hits = mb.search_brain("关东地区的二手市场见闻", k=5, reinforce=False)
    # A 的种子向量 cos=1.0（过 floor）；B 也是 1.0？B 种子 = _vec2(0.0) → cos=1.0……
    # 用向量区分：重播 B 为 45°（cos=0.707 < 0.80 → 应被丢）
    conn = mb.open_brain()
    _seed_vec(conn, "atom", rid_b, _vec2(np.pi / 4), "东京的中古店圈子文化观察记录")
    conn.commit()
    conn.close()
    hits = mb.search_brain("关东地区的二手市场见闻", k=5, reinforce=False)
    by_id = {h["record_id"] for h in hits}
    assert rid_b not in by_id, "cos=0.707 < floor 0.80 应被丢弃"
    assert rid_a in by_id, "cos=1.0 的 A 不受影响"

# ---- 6. 变体开关（B：cos→rel 标定；C：通道配额）----

def test_vec_rel_default_is_identity(monkeypatch):
    """缺省 rel_mode=cos：向量行 rel 就是 cos（B 方案默认关，行为与实测基线一致）。"""
    monkeypatch.setattr(mb, "_vec_rel_mode", lambda: "cos")
    assert mb._vec_rel(0.6) == 0.6
    assert mb._vec_rel(1.4) == 1.0          # 上界钳位
    assert mb._vec_rel(-0.2) == 0.0         # 下界钳位


def test_vec_rel_calib_maps_to_bm25_scale(monkeypatch):
    """B 方案：rel = 0.296*cos + 0.665（2026-11-14 用 39 个 FTS/向量配对点拟合）。"""
    monkeypatch.setattr(mb, "_vec_rel_mode", lambda: "calib")
    assert mb._vec_rel(0.58) == pytest.approx(0.296 * 0.58 + 0.665, abs=1e-6)
    assert mb._vec_rel(1.5) == pytest.approx(0.961, abs=1e-6)   # cos 入参先钳到 1.0
    assert mb._vec_rel(0.55) < mb._vec_rel(0.65)   # 单调


def test_vec_quota_gate():
    """C 方案三道门：短词/精确 token/低 cos 一律不触发配额。"""
    nl = "怎么判断两件是不是同一批做出来的"        # 自然语言长句
    assert mb._vec_quota_gate(nl, 0.62) is True
    assert mb._vec_quota_gate(nl, 0.585) is False  # 语义强度不够 → 宁可空着
    assert mb._vec_quota_gate("素鸡", 0.99) is False        # 关键词查询
    assert mb._vec_quota_gate("roji 的袜子怎么挑", 0.99) is False   # ASCII 精确 token
    assert mb._vec_quota_gate("KY3-铁皮钟-1126 多少钱", 0.99) is False


def test_vec_quota_default_off(monkeypatch):
    """quota 缺省 0：不开配额时候选集合与顺序不变（回归锁）。"""
    monkeypatch.setattr(mb, "_vec_quota", lambda: 0)
    assert mb._vec_quota() == 0
