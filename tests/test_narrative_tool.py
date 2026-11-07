"""NP-1 测试锁（plan personal-agent-memory §8.5 NP-1 行 / §13.6）：

- N-1  >7 天前的日记可由 narrative_search 命中（R6）；
- N-2  同 query 两个 context_key 排序有差异、两条都可召回（INV-3，core 面已在
       test_memory_localtime 锁过，这里锁工具返回形态）；
- N-7  explicit 白名单含两个新工具（群聊 run 可调用）；
- N-10 外部来源：relation 不返回正文；跨场合叙事 cross_context=True；
       内部轮全库可读（INV-8）；
- INV-12 检索条目不含正文；
- N-5  配额：run 作用域内七工具合计 ≤3 次、narrative_* ≤2 次（超限明确拒绝）。

不拉真实模型；brain.db 落 tmp_path。
"""
import sys

import pytest

sys.path.insert(0, ".")

from omni_core import memory_brain as mb  # noqa: E402
from omni_core.tools import memory_tool as mt  # noqa: E402
from omni_core.tools import narrative_tool as nt  # noqa: E402


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(mb, "enabled", lambda: True)
    mt.reset_recall_stats()
    mt._run_stats["active"] = False          # 默认非 run 作用域（配额不强制）
    yield
    mt.reset_recall_stats()


def _seed(brain):
    # 索引行 + 真实正文块（read_narrative 走文件真相路径）
    from omni_core.local import scene_executor as se

    se.write_diary_block("p", "2026-10-01", "那天的抬杠。正文里聊了白米饭和剑网3。")
    se.write_diary_block("p", "2026-10-02", "另一天的钓鱼。河边坐了一下午。")
    mb.upsert_narrative_index("diary-2026-10-01.md", "diary", project_id="p",
                              date_from="2026-10-01", date_to="2026-10-01",
                              summary="那天的抬杠", topics=["抬杠", "白米饭"],
                              context_keys=["group:a"])
    mb.upsert_narrative_index("diary-2026-10-02.md", "diary", project_id="p",
                              date_from="2026-10-02", date_to="2026-10-02",
                              summary="另一天的钓鱼", topics=["钓鱼"],
                              context_keys=["group:b"])
    rel = __import__("omni_core.local.runtime_paths", fromlist=["global_relations"]) \
        .global_relations()
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "999999.md").write_text(
        "-----META-START-----\nname: 成员甲\nuid: 999999\nupdated: x\n"
        "-----META-END-----\n最近交手：起哄。\n他对我的态度：试探。", encoding="utf-8")


# ---- N-1：>7 天日记可命中（工具面） ----

def test_stale_diary_hit_via_tool(brain):
    _seed(brain)
    r = nt.narrative_search(query="抬杠")
    assert r["ok"] and r["count"] >= 1
    assert any(x["doc_id"] == "diary-2026-10-01.md" for x in r["results"])
    # INV-12：条目不含正文
    assert all("text" not in x for x in r["results"])
    # 全文按需读
    rd = nt.narrative_read(doc_id="diary-2026-10-01.md", kind="diary")
    assert rd["ok"] and "text" in rd


# ---- N-10：出口约束（外部来源） ----

def _set_external(monkeypatch, context_key, external=True):
    """边界层写入本轮结构；build_run 把它同步进 run 级信号。"""
    from omni_core.local.loop import context_signal as cs
    cs.set_boundary(context_key=context_key, source_external=external)
    cs.build_run(context_key=context_key)


def test_external_relation_summary_only(brain, monkeypatch):
    _seed(brain)
    _set_external(monkeypatch, "group:c", external=True)
    r = nt.narrative_read(doc_id="999999", kind="relation")
    assert r["ok"] and r["text"] == ""
    assert "起哄" not in str(r.get("summary") or "")   # 关系卡无 summary，正文不外泄
    assert r.get("note")


def test_external_cross_context_diary_flagged(brain, monkeypatch):
    _seed(brain)
    _set_external(monkeypatch, "group:c", external=True)
    r = nt.narrative_read(doc_id="diary-2026-10-01.md", kind="diary")
    assert r["ok"] and r["cross_context"] is True
    assert "不得把这里的内容当当前场合的事直接说出口" in str(r.get("note") or "")
    # 同场合：可读且不带跨场合标记
    _set_external(monkeypatch, "group:a", external=True)
    r2 = nt.narrative_read(doc_id="diary-2026-10-01.md", kind="diary")
    assert r2["ok"] and r2["cross_context"] is False
    assert r2["text"]
    # 内部轮：全库可读、无标记
    _set_external(monkeypatch, "", external=False)
    r3 = nt.narrative_read(doc_id="diary-2026-10-02.md", kind="diary")
    assert r3["ok"] and r3["text"] and r3["cross_context"] is False


def test_external_search_still_recalls_all(brain, monkeypatch):
    """INV-3：外部来源检索仍全库召回（条目带 cross_context 标记），不因出口收紧而过滤。"""
    _seed(brain)
    _set_external(monkeypatch, "group:a", external=True)
    r = nt.narrative_search(query="钓鱼")
    assert r["ok"] and any(x["doc_id"] == "diary-2026-10-02.md" for x in r["results"])
    hit = [x for x in r["results"] if x["doc_id"] == "diary-2026-10-02.md"][0]
    assert hit["cross_context"] is True
    _set_external(monkeypatch, "", external=False)


# ---- N-7：白名单 ----

def test_explicit_allowlist_includes_narrative_tools():
    import inspect

    from gateway import dispatch
    src = inspect.getsource(dispatch._run_task_once)
    assert '"narrative_search"' in src and '"narrative_read"' in src
    # 工具已注册（导入即注册）
    from omni_core.tools.base import TOOL_REGISTRY
    assert "narrative_search" in TOOL_REGISTRY and "narrative_read" in TOOL_REGISTRY


# ---- N-5：配额（run 作用域） ----

def test_quota_enforced_within_run_scope(brain):
    _seed(brain)
    mt.reset_recall_stats()                       # 置 active=True
    ok = [nt.narrative_search(query="抬杠") for _ in range(2)]
    assert all(x["ok"] for x in ok)               # narrative_* ≤2：前两次放行
    third = nt.narrative_search(query="钓鱼")
    assert third["ok"] is False and "narrative" in third["error"]
    # 总配额：reset 后用 memory_recall×3 打满
    mt.reset_recall_stats()
    conn = mb.open_brain()
    try:
        for i in range(4):
            mb._insert_atom(conn, f"r{i}", {"text": f"白米饭记忆第{i}条",
                                            "kind": "fact", "importance": 0.6},
                            "p", "t", mb._now())
        conn.commit()
    finally:
        conn.close()
    for _ in range(3):
        assert mt.memory_recall(query="白米饭")["ok"] is True
    fourth = mt.memory_recall(query="白米饭")
    assert fourth["ok"] is False and "≤3" in fourth["error"]
    # 非 run 作用域（直调）不强制
    mt._run_stats["active"] = False
    assert mt.memory_recall(query="白米饭")["ok"] is True


def test_observability_row_schema(brain, monkeypatch, tmp_path):
    """§8.3 定稿字段：run_id / context_key / source_kind / by_tool / context_chars。"""
    import json as _json
    import gateway.channels.context as ch

    monkeypatch.setattr(ch, "_CURRENT_REPLY", None, raising=False)
    monkeypatch.setattr(ch, "_SOURCE_KIND", "chat", raising=False)
    monkeypatch.setattr(ch, "_SOURCE_EXTERNAL", False, raising=False)
    shard = mb._now()[:7]
    log = tmp_path / f"memory_recall-{shard}.jsonl"
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    # run_id 由 context_signal 构造（§8.3 F-1：两源共同主键）
    from omni_core.local.loop import context_signal as _cs
    _cs._CURRENT.update({"run_id": "t_abc:rk_1"})
    mt.reset_recall_stats()
    _seed(brain)
    nt.narrative_search(query="抬杠")
    mt.log_recall_run(project="p", context_key="group:x", source_kind="chat",
                      injected_context=True, context_chars=180, sent=True,
                      llm_calls_delta=2)
    rows = [ _json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip() ]
    assert rows
    row = rows[-1]
    assert row["run_id"] == "t_abc:rk_1"
    for key in ("run_id", "ts", "project", "context_key", "source_kind",
                "source_external", "injected_context", "context_chars", "by_tool",
                "calls", "hits", "top1_id", "zero_query", "context_hits",
                "narrative_cross", "sent", "llm_calls_delta"):
        assert key in row, key
    assert row["by_tool"].get("narrative_search") == 1
    assert row["injected_context"] is True and row["context_chars"] == 180
