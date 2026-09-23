"""M6 共享黑板（WorldModel）+ 子任务板（SubtaskStore）。

设计：doc/plans/multi-agent-redesign-2026-09-13.md §3 / §4

核心场景：
1. shared() 同 task 同实例；空 task_id 不登记；release 后可回收
2. 并发写不丢事实（RLock 生效）
3. 溯源：add_fact(source=) + view(scope) 只看到「全局 + 本分支」
4. 事实来源跨 save/load 保持
5. 落盘原子：无 .tmp 残留，文件可解析
6. 子任务板：claim CAS / claim_next / finish / release
7. 主路径确实取共享黑板并在任务结束释放登记
"""
import inspect
import json
import threading
from pathlib import Path

import pytest

from omni_core.local.tool_loop import ToolLoop
from omni_core.local.world_model import WorldModel
from omni_core.local.task_store import SubtaskStore, PENDING, RUNNING, DONE, FAILED
import omni_core.local.runtime_paths as _RP


def _redirect(tmp_path):
    """把所有 task 资产重定向到临时目录（不污染真实 ~/.omniagent）。"""
    _RP._GLOBAL = tmp_path / ".omniagent"
    _RP.ensure_global_dirs()
    return _RP._GLOBAL


@pytest.fixture(autouse=True)
def _clean_registry():
    WorldModel.clear_registry()
    yield
    WorldModel.clear_registry()


# === 1. 共享注册表 =========================================================
def test_shared_returns_same_instance():
    a = WorldModel.shared("t_shared")
    b = WorldModel.shared("t_shared")
    assert a is b
    assert "t_shared" in WorldModel.registry_keys()


def test_empty_task_id_not_registered():
    a = WorldModel.shared("")
    b = WorldModel.shared("")
    assert a is not b          # 无 id 的临时 world 不共享，避免互相串台
    assert WorldModel.registry_keys() == []


def test_release_then_new_instance():
    a = WorldModel.shared("t_rel")
    WorldModel.release("t_rel")
    assert "t_rel" not in WorldModel.registry_keys()
    b = WorldModel.shared("t_rel")
    assert a is not b


# === 2. 并发写不丢事实 =====================================================
def test_concurrent_add_fact_no_loss():
    wm = WorldModel.shared("t_conc")
    n_threads, n_facts = 8, 50

    def _worker(tid: int):
        for i in range(n_facts):
            wm.add_fact(f"f-{tid}-{i}", source=f"agent-{tid}")

    threads = [threading.Thread(target=_worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(wm.facts) == n_threads * n_facts
    assert wm.fact_source("f-3-7") == "agent-3"


# === 3. 溯源与视图隔离 =====================================================
def test_view_scope_only_global_and_own():
    wm = WorldModel(task_id="t_view")
    wm.add_fact("全局事实", source="")
    wm.add_fact("A 分支事实", source="agent-a")
    wm.add_fact("B 分支事实", source="agent-b")

    va = wm.view(scope="agent-a")
    assert "全局事实" in va
    assert "A 分支事实" in va
    assert "B 分支事实" not in va

    vb = wm.view(scope="agent-b")
    assert "全局事实" in vb
    assert "B 分支事实" in vb
    assert "A 分支事实" not in vb

    # 无 scope = 全部（兼容 summary 语义）
    assert "A 分支事实" in wm.view() and "B 分支事实" in wm.view()
    assert wm.facts_for(scope="agent-a") == ["全局事实", "A 分支事实"]


def test_merge_collection_carries_source():
    """子任务 flush 到黑板时自带来源（tool_loop._run_subtask 的语义）。"""
    parent = WorldModel(task_id="t_merge")
    sub = WorldModel()
    sub.add_fact("子任务发现的事实")
    sub.add_collected("条目1")
    sub.add_note("备注1")

    parent.merge_collection(sub, source="sub:目标A")

    assert "条目1" in [c.get("text") for c in parent.collected]
    assert "备注1" in parent.notes
    assert parent.fact_source("子任务发现的事实") == "sub:目标A"


# === 4. 溯源跨 save/load 保持 ==============================================
def test_fact_meta_survives_save_load(tmp_path):
    _redirect(tmp_path)
    wm = WorldModel(task_id="t_meta")
    wm.add_fact("带来源的事实", source="agent-a")
    wm.add_fact("无来源的事实")
    wm.save()

    other = WorldModel(task_id="t_meta")
    assert other.load() is True
    assert "带来源的事实" in other.facts
    assert other.fact_source("带来源的事实") == "agent-a"
    assert other.fact_source("无来源的事实") == ""


# === 5. 原子写 =============================================================
def test_save_atomic_no_tmp_left(tmp_path):
    _redirect(tmp_path)
    wm = WorldModel(task_id="t_atomic")
    for i in range(30):
        wm.add_fact(f"f{i}", source="agent-a")
    wm.save()

    d = _RP.task_dir("t_atomic")
    assert not list(d.glob("*.tmp"))
    assert (d / "world_model.md").exists()
    payload = json.loads((d / "collected.json").read_text(encoding="utf-8"))
    assert payload["facts_meta"]["f7"]["source"] == "agent-a"


def test_concurrent_save_produces_readable_file(tmp_path):
    _redirect(tmp_path)
    wm = WorldModel.shared("t_atomic2")
    wm.add_fact("基础事实")

    def _writer(tid: int):
        for i in range(10):
            wm.add_fact(f"c-{tid}-{i}", source=f"agent-{tid}")
            wm.save()

    threads = [threading.Thread(target=_writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = (_RP.task_dir("t_atomic2") / "world_model.md").read_text(encoding="utf-8")
    assert text.startswith("---")          # 半截文件不会以 frontmatter 开头
    assert "已知事实" in text


# === 6. 子任务板 ===========================================================
def test_subtask_claim_is_cas(tmp_path):
    _redirect(tmp_path)
    _RP.ensure_task_dirs("t_board")
    st = SubtaskStore.add("t_board", "子任务1")
    assert st["state"] == PENDING

    first = SubtaskStore.claim("t_board", st["id"], "agent-a")
    assert first is not None and first["state"] == RUNNING
    assert first["owner_agent_id"] == "agent-a"

    # 已被领取：同一子任务第二次领取失败（CAS 生效）
    assert SubtaskStore.claim("t_board", st["id"], "agent-b") is None
    assert SubtaskStore.list("t_board", state=RUNNING)[0]["owner_agent_id"] == "agent-a"


def test_subtask_claim_next_distributes(tmp_path):
    _redirect(tmp_path)
    _RP.ensure_task_dirs("t_board2")
    SubtaskStore.add_many("t_board2", [{"desc": "a"}, {"desc": "b"}, {"desc": "c"}])

    got = []
    while True:
        item = SubtaskStore.claim_next("t_board2", "agent-a")
        if item is None:
            break
        got.append(item["desc"])
        SubtaskStore.finish("t_board2", item["id"], success=True)
    assert got == ["a", "b", "c"]
    assert len(SubtaskStore.list("t_board2", state=DONE)) == 3
    assert SubtaskStore.claim_next("t_board2", "agent-a") is None


def test_subtask_failed_and_release(tmp_path):
    _redirect(tmp_path)
    _RP.ensure_task_dirs("t_board3")
    st = SubtaskStore.add("t_board3", "会失败的任务")
    SubtaskStore.claim("t_board3", st["id"], "agent-a")
    SubtaskStore.finish("t_board3", st["id"], success=False, result_ref="升级")
    assert SubtaskStore.list("t_board3", state=FAILED)[0]["result_ref"] == "升级"

    st2 = SubtaskStore.add("t_board3", "可重试任务")
    SubtaskStore.claim("t_board3", st2["id"], "agent-a")
    released = SubtaskStore.release("t_board3", st2["id"])
    assert released["state"] == PENDING
    # 退回后可再次领取，attempts 累加
    again = SubtaskStore.claim("t_board3", st2["id"], "agent-b")
    assert again["attempts"] == 2
    assert again["owner_agent_id"] == "agent-b"


# === 7. 主路径确实使用共享黑板 =============================================
def test_m10_single_entry_point():
    """M10：只有 run_task 一个入口；run_task_two_layer 退化为它的兼容别名。"""
    import inspect
    from omni_core.local.tool_loop import ToolLoop
    import omni_core.local.loop as _lp

    _ld = Path(inspect.getfile(_lp)).parent
    src = "\n".join((_ld / f).read_text(encoding="utf-8") for f in sorted(_ld.glob("*.py")))
    assert "def run_task(" in src
    assert "def _run_graph(" in src
    # 旧的两条路径入口都不复存在
    assert "def _run_two_layer_inner(" not in src
    # 两层兼容别名也已随 Phase 0 退役删除
    assert "def run_task_two_layer(" not in src


def test_main_paths_use_shared_board():
    import omni_core.local.loop as _lp
    _ld = Path(inspect.getfile(_lp)).parent
    src = "\n".join((_ld / f).read_text(encoding="utf-8") for f in sorted(_ld.glob("*.py")))
    assert "WorldModel.shared(" in src
    assert "WorldModel.release(" in src
    # 旧的 per-run 私有实例写法已不在主路径
    assert "world = WorldModel(task_id=spec.task_id)" not in src


def test_unified_entry_releases_board_on_exception():
    """M10 统一入口：异常路径也必须释放登记（finally）。"""
    import omni_core.local.loop as _lp
    _ld = Path(inspect.getfile(_lp)).parent
    src = "\n".join((_ld / f).read_text(encoding="utf-8") for f in sorted(_ld.glob("*.py")))
    _, _, tail = src.partition("def run_task(")
    wrapper = tail.split("def run_task_two_layer(")[0]
    assert "try:" in wrapper and "WorldModel.release(spec.task_id)" in wrapper
