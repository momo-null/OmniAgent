"""runtime_paths 单测（方案 C）+ 北极星红线自检。

红线自检：源码不得含场景 / 业务词，不得出现 `app` 作为分区键。

P3.1: _isolated_home fixture 已移至 conftest.py 统一管理。
"""
import os
import re

import pytest

from omni_core.local import runtime_paths as P


def test_paths_are_under_global_root():
    assert P.global_omni().name == ".omniagent"
    assert P.global_skills().parts[-1] == "skills"
    assert P.projects_root().parts[-1] == "projects"
    assert P.tasks_root().parts[-1] == "tasks"


def test_slugify_and_task_paths():
    pid = P.slugify_path("D:\\AI\\OmniAgent")
    assert P.project_dir(pid).parts[-2:] == ("projects", pid)
    tid = "t_abc123"
    assert P.task_dir(tid).parts[-2:] == ("tasks", tid)
    assert P.task_json(tid).name == "task.json"
    assert P.task_trajectory(tid).name == "trajectory.jsonl"
    assert P.task_world_model(tid).name == "world_model.md"
    assert P.task_collected(tid).name == "collected.json"


def test_ensure_task_dirs_idempotent():
    """知识分层 v2：task 目录只含证据与运行态（tmp），skills 归 project 层。"""
    tid = "t_x"
    P.ensure_task_dirs(tid)
    assert P.task_dir(tid).exists()
    assert P.task_tmp(tid).exists()
    assert not P.task_dir(tid).joinpath("skills").exists()
    P.ensure_task_dirs(tid)  # 再跑不报错
    assert P.task_dir(tid).exists()


def test_redline_no_scenario_words_in_source():
    """源码不得含场景 / 业务词、不得用 app 作分区键。"""
    src = os.path.join(os.path.dirname(__file__), "..", "omni_core", "local", "runtime_paths.py")
    text = open(src, encoding="utf-8").read()
    forbidden = ["game", "app", "App", "APP", "project_name", "scenario"]
    hits = [w for w in forbidden if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", text)]
    assert not hits, f"红线违规词: {hits}"


def test_redline_app_not_used_as_partition_key():
    """分区键只能是 project_id / task_id，禁止具体 app 键控分区（旧架构回潮防护）。

    知识分层 v2 起 ``project_skills(project_id)`` 等 project_* 函数是合法的
    （slug 分组，非 app 分区）；本测试守的是**意图**——分区键不得是 app/场景词。
    """
    src = os.path.join(os.path.dirname(__file__), "..", "omni_core", "local", "runtime_paths.py")
    text = open(src, encoding="utf-8").read()
    assert not re.search(r"project_\w+\(\s*app", text), "project_* 不得以 app 为参"
    # world_model 是任务运行态（plan §1 规则 2：不搬家），不得出现 project 层版本
    assert "project_world_model" not in text
    assert "project_trajectory" not in text
    assert "project_collected" not in text
    assert "PROJECT_OMNI" not in text
    assert "workspace_dir" not in text
    assert "resolve_workspace" not in text


