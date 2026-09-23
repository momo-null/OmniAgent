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
    assert P.global_memory().parts[-1] == "memory"
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
    tid = "t_x"
    P.ensure_task_dirs(tid)
    assert P.task_skills(tid).exists()
    P.ensure_task_dirs(tid)  # 再跑不报错
    assert P.task_skills(tid).exists()


def test_redline_no_scenario_words_in_source():
    """源码不得含场景 / 业务词、不得用 app 作分区键。"""
    src = os.path.join(os.path.dirname(__file__), "..", "omni_core", "local", "runtime_paths.py")
    text = open(src, encoding="utf-8").read()
    forbidden = ["game", "app", "App", "APP", "project_name", "scenario"]
    hits = [w for w in forbidden if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", text)]
    assert not hits, f"红线违规词: {hits}"


def test_redline_app_not_used_as_partition_key():
    """确保没有 project_*(app) / <type>/<app>/ 这类分区用法。"""
    src = os.path.join(os.path.dirname(__file__), "..", "omni_core", "local", "runtime_paths.py")
    text = open(src, encoding="utf-8").read()
    assert "project_skills" not in text
    assert "project_world_model" not in text
    assert "project_trajectory" not in text
    assert "project_collected" not in text
    assert "PROJECT_OMNI" not in text
    assert "workspace_dir" not in text
    assert "resolve_workspace" not in text


# ---------------------------------------------------------------------------
# §3.2 知识层路径（memory/ 子目录 + task_id 安全校验）
# ---------------------------------------------------------------------------
def test_memory_paths_under_global_memory():
    assert P.memory_rollouts().parts[-2:] == ("memory", "rollouts")
    assert P.memory_master().name == "MEMORY.md"
    assert P.memory_summary().name == "memory_summary.md"


def test_memory_rollout_file_safe_and_resolved():
    tid = "t_safe-123"
    p = P.memory_rollout_file(tid)
    assert p.parts[-2:] == ("rollouts", "t_safe-123.md")
    # 校验后必须是 memory/rollouts 的直接子文件（杜绝穿越）
    assert p.parent == P.memory_rollouts()


def test_memory_rollout_file_rejects_unsafe_id():
    for bad in ("", "../escape", "a/b", "CON"):
        with pytest.raises(ValueError):
            P.memory_rollout_file(bad)


def test_ensure_global_dirs_creates_rollouts():
    P.ensure_global_dirs()
    assert P.memory_rollouts().exists()
    assert P.memory_rollouts().is_dir()
