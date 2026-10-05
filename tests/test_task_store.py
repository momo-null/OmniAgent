"""TaskStore / ProjectStore 单测（方案 C）。

用临时 HOME 隔离真实 ~/.omniagent，避免污染用户数据。
P3.1: _isolated_home fixture 已移至 conftest.py 统一管理。
"""
import json
import os

import pytest

from omni_core.local import runtime_paths as P
from omni_core.local.task_store import TaskStore, ProjectStore


def test_slugify_path():
    assert P.slugify_path("D:\\AI\\OmniAgent") == "d-AI-OmniAgent"
    assert P.slugify_path("C:/Users/user/WorkBuddy") == "c-Users-user-WorkBuddy"
    assert P.slugify_path("/home/user/x") == "home-user-x"


def test_default_project_when_no_project_id():
    """无显式归属 → 缺省 default 项目（auto_project_id 已废除，防知识碎片化）。"""
    meta = TaskStore.create(objective="x")
    assert meta["project_id"] == P.DEFAULT_PROJECT_ID
    # 逻辑归属恒定存在；物理目录懒创建（ensure 由创建路径触发）
    assert P.project_dir(P.DEFAULT_PROJECT_ID).exists()


def test_task_create_and_get():
    meta = TaskStore.create(objective="do something", done_when="it's done", project_id="d-AI-X")
    assert meta["task_id"].startswith("t_")
    assert meta["state"] == "pending"
    assert meta["project_id"] == "d-AI-X"
    got = TaskStore.get(meta["task_id"])
    assert got["objective"] == "do something"
    # 目录已建
    assert P.task_dir(meta["task_id"]).exists()


def test_task_list_flat_and_sorted():
    m1 = TaskStore.create(objective="a")
    m2 = TaskStore.create(objective="b")
    allm = TaskStore.list()
    assert len(allm) == 2
    # 倒序：m2 晚于 m1
    assert allm[0]["task_id"] == m2["task_id"]


def test_task_update_state_finished_at():
    meta = TaskStore.create(objective="x")
    updated = TaskStore.update(meta["task_id"], state="done", success=True)
    assert updated["state"] == "done"
    assert updated["finished_at"]
    assert updated["success"] is True


def test_task_add_run():
    meta = TaskStore.create(objective="x")
    TaskStore.add_run(meta["task_id"], "run1")
    TaskStore.add_run(meta["task_id"], "run2")
    got = TaskStore.get(meta["task_id"])
    assert got["runs"] == ["run1", "run2"]


def test_project_session_roundtrip():
    pid = "d-AI-X"
    ProjectStore.append_message(pid, "sid1", "user", "hello")
    ProjectStore.append_message(pid, "sid1", "assistant", "hi", task_id="t_abc")
    msgs = ProjectStore.read_session(pid, "sid1")
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["task_id"] == "t_abc"
    # 列表
    projects = ProjectStore.list()
    assert any(p["id"] == pid for p in projects)
    assert "sid1" in ProjectStore.list_sessions(pid)


def test_project_display_name_and_list():
    """C3：slug 唯一 id + project.json 存显示名；list() 产出 display_name。"""
    ProjectStore.ensure("d-AI-X")
    assert ProjectStore.display_name("d-AI-X") == ""
    ProjectStore.set_display_name("d-AI-X", "我的 AI 工作台")
    assert ProjectStore.display_name("d-AI-X") == "我的 AI 工作台"
    items = {p["id"]: p for p in ProjectStore.list()}
    assert items["d-AI-X"]["display_name"] == "我的 AI 工作台"


def test_project_remove_deletes_container(tmp_path):
    """C5：remove 删整个项目目录（会话 + project 级知识资产）。"""
    pid = "d-rem"
    ProjectStore.ensure(pid)
    ProjectStore.append_message(pid, "s1", "user", "hi")
    import omni_core.local.runtime_paths as _P
    skills = _P.project_skills(pid)
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "a.md").write_text("x", encoding="utf-8")
    ProjectStore.remove(pid)
    assert not _P.project_dir(pid).exists()


def test_move_session_follows_project_change():
    """保存到项目：TaskStore.update 改 project_id 时，会话 jsonl 一并搬到新项目目录。

    回归背景：早期只改 task.json，jsonl 留在 default → 项目 session_count 恒 0、
    刷新后历史读空（read_session 去新目录找文件）。
    """
    meta = TaskStore.create(objective="chat")
    pid_old = meta["project_id"]  # default
    sid = meta["session_id"]
    ProjectStore.append_message(pid_old, sid, "user", "hello")
    ProjectStore.append_message(pid_old, sid, "assistant", "hi", task_id=meta["task_id"])

    updated = TaskStore.update(meta["task_id"], project_id="d-new")
    assert updated["project_id"] == "d-new"

    # jsonl 已搬到新项目：新项目可读、count=1；旧项目不再有该会话
    msgs = ProjectStore.read_session("d-new", sid)
    assert [m["content"] for m in msgs] == ["hello", "hi"]
    assert not P.session_file(pid_old, sid).exists()
    counts = {p["id"]: p["session_count"] for p in ProjectStore.list()}
    assert counts["d-new"] == 1
    assert counts[pid_old] == 0


def test_move_session_idempotent_guards():
    """move_session 幂等护栏：同项目 / 源缺失 / 目标已存在 → 跳过返回 False。"""
    ProjectStore.append_message("d-a", "s1", "user", "hi")
    assert ProjectStore.move_session("d-a", "d-a", "s1") is False      # 同项目
    assert ProjectStore.move_session("d-a", "d-b", "s-missing") is False  # 源缺失
    ProjectStore.append_message("d-b", "s1", "user", "already there")
    assert ProjectStore.move_session("d-a", "d-b", "s1") is False      # 目标已存在，不覆盖
    assert ProjectStore.move_session("d-a", "d-c", "s1") is True
    assert ProjectStore.read_session("d-c", "s1")[0]["content"] == "hi"


def test_reconcile_sessions_heals_legacy_strays():
    """自愈历史残留：task.json 已归属新项目、jsonl 仍留在旧项目 → reconcile 搬回。

    模拟早期版本留下的脏数据：直接手动把 jsonl 写回 default（绕过 update 的搬迁）。
    """
    meta = TaskStore.create(objective="legacy")
    sid = meta["session_id"]
    pid_new = "d-legacy"
    ProjectStore.ensure(pid_new)
    TaskStore.update(meta["task_id"], project_id=pid_new)
    # 复刻历史脏数据：会话文件躺在 default
    ProjectStore.append_message(P.DEFAULT_PROJECT_ID, sid, "user", "old msg")

    assert ProjectStore.reconcile_sessions() == 1
    assert ProjectStore.read_session(pid_new, sid)[0]["content"] == "old msg"
    assert not P.session_file(P.DEFAULT_PROJECT_ID, sid).exists()
    # 幂等：再跑一遍不再搬
    assert ProjectStore.reconcile_sessions() == 0


# === 会话重放：增量落库 coalesce（2026-10-04） ==============================

def _rec(role, content, extra=None):
    return {"role": role, "content": content, "ts": "t", **({"extra": extra} if extra else {})}


def test_coalesce_legacy_records_pass_through():
    """无 run_key 的历史记录原样透传（兼容旧数据，不合并不丢弃）。"""
    recs = [_rec("user", "hi"),
            _rec("assistant", "done", {"steps": [{"type": "tool_call"}], "final": True}),
            _rec("user", "next")]
    out = ProjectStore.coalesce_records(recs)
    assert out == recs


def test_coalesce_prefers_final_with_steps():
    """同 run 的 partial + final(带完整 steps) → 只保留 final。"""
    steps_a = [{"type": "tool_call", "name": "a"}]
    steps_b = [{"type": "tool_call", "name": "b"}]
    recs = [
        _rec("user", "开始"),
        _rec("assistant", "", {"steps": steps_a, "partial": True, "run_key": "rk1"}),
        _rec("assistant", "", {"steps": steps_b, "partial": True, "run_key": "rk1"}),
        _rec("assistant", "完成", {"steps": steps_a + steps_b, "final": True, "run_key": "rk1"}),
        _rec("user", "下一轮"),
    ]
    out = ProjectStore.coalesce_records(recs)
    assert len(out) == 3
    assert out[1]["content"] == "完成" and out[1]["extra"]["final"] is True


def test_coalesce_merges_partials_when_final_omits_steps():
    """final 因超长省略 steps（steps_omitted）→ 由 partial 拼接回放，content/meta 取 final。"""
    p1 = [{"type": "tool_call", "name": "a"}]
    p2 = [{"type": "tool_call", "name": "b"}]
    recs = [
        _rec("user", "开始"),
        _rec("assistant", "", {"steps": p1, "partial": True, "run_key": "rk2"}),
        _rec("assistant", "", {"steps": p2, "partial": True, "run_key": "rk2"}),
        _rec("assistant", "做完了", {"meta": {"success": True}, "final": True,
                                     "steps_omitted": True, "run_key": "rk2"}),
    ]
    out = ProjectStore.coalesce_records(recs)
    assert len(out) == 2
    m = out[1]
    assert m["content"] == "做完了"
    assert [s["name"] for s in m["extra"]["steps"]] == ["a", "b"]
    assert m["extra"]["meta"] == {"success": True}
    assert m["extra"]["partial"] is True


def test_coalesce_crashed_run_partials_only():
    """run 被杀只有 partial（无 final）→ 拼接出完整过程流（崩溃恢复）。"""
    recs = [
        _rec("user", "开始游戏"),
        _rec("assistant", "", {"steps": [{"type": "thinking", "content": "开局"}],
                               "partial": True, "run_key": "rk3"}),
        _rec("assistant", "", {"steps": [{"type": "tool_call", "name": "play"}],
                               "partial": True, "run_key": "rk3"}),
    ]
    out = ProjectStore.coalesce_records(recs)
    assert len(out) == 2
    assert len(out[1]["extra"]["steps"]) == 2
    assert out[1]["extra"]["partial"] is True


def test_read_session_skips_corrupt_tail_lines():
    """崩溃留下的半截行：跳过坏行返回有效历史，不整份报错。"""
    from omni_core.local import runtime_paths as _P
    ProjectStore.ensure("d-corrupt")
    f = _P.session_file("d-corrupt", "s_ok")
    good = json.dumps({"role": "user", "content": "hello"}, ensure_ascii=False)
    f.write_text(good + "\n" + '{"role": "assistant", "conte', encoding="utf-8")
    msgs = ProjectStore.read_session("d-corrupt", "s_ok")
    assert [m["content"] for m in msgs] == ["hello"]
