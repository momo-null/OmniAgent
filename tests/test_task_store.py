"""TaskStore / ProjectStore 单测（方案 C）。

用临时 HOME 隔离真实 ~/.omniagent，避免污染用户数据。
P3.1: _isolated_home fixture 已移至 conftest.py 统一管理。
"""
import os

import pytest

from omni_core.local import runtime_paths as P
from omni_core.local.task_store import TaskStore, ProjectStore


def test_slugify_path():
    assert P.slugify_path("D:\\AI\\OmniAgent") == "d-AI-OmniAgent"
    assert P.slugify_path("C:/Users/user/WorkBuddy") == "c-Users-user-WorkBuddy"
    assert P.slugify_path("/home/user/x") == "home-user-x"


def test_auto_project_id_unique():
    a = P.auto_project_id()
    assert a.startswith("OmniAgent-")
    # 第二次调用应不同（目录已建）
    b = P.auto_project_id()
    assert a != b


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
