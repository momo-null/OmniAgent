"""临时产物目录（§6）：默认落 `tasks/<task_id>/tmp/`，不进项目目录；任务终态自动清理。

设计：
- `ContextVar` 绑定（并发子任务各自独立，不串）；
- 工具默认解析到它（`shell_exec` 的 cwd、相对路径基准）；
- 终态只删 `tmp/`，**绝不碰同目录持久资产**（task.json / trajectory / world_model / skills…）。
"""
import os
from pathlib import Path

from omni_core.local import runtime_paths as RP
from omni_core.tools.base import call_tool
from omni_core.tools.workspace import current_task, resolve_path, set_task, task_tmp_dir

REPO_ROOT = Path(__file__).resolve().parents[1]
_SHELL = "cmd" if os.name == "nt" else "bash"


# --- 目录与路径解析 ---------------------------------------------------------

def test_task_tmp_path_and_ensure_dirs():
    RP.ensure_task_dirs("t_tmp_a")
    assert RP.task_tmp("t_tmp_a") == RP.task_dir("t_tmp_a") / "tmp"
    assert RP.task_tmp("t_tmp_a").is_dir()


def test_resolve_path_relative_goes_to_task_tmp():
    set_task("t_tmp_b")
    try:
        assert current_task() == "t_tmp_b"
        assert resolve_path("script.py") == RP.task_tmp("t_tmp_b") / "script.py"
        # 绝对路径原样返回（S1 的路径围栏才是硬边界，这里只定缺省）
        abs_p = REPO_ROOT / "x.txt"
        assert resolve_path(str(abs_p)) == abs_p
    finally:
        set_task(None)


def test_unbound_falls_back_to_cwd_without_raising():
    set_task(None)
    assert current_task() is None
    assert task_tmp_dir() == Path.cwd()
    assert resolve_path("x.txt") == Path.cwd() / "x.txt"


def test_contextvar_isolated_between_tasks():
    """并发子任务各自独立：重绑只影响当前上下文（用 ContextVar 而非模块全局的理由）。"""
    set_task("t_a")
    a = task_tmp_dir()
    set_task("t_b")
    b = task_tmp_dir()
    assert a != b
    assert a.parent.name == "t_a" and b.parent.name == "t_b"
    set_task(None)


# --- 工具默认落点 -----------------------------------------------------------

def test_shell_exec_default_cwd_is_task_tmp():
    """命令默认工作目录 = 当前任务 tmp：产物**不进项目目录**。"""
    set_task("t_tmp_cwd")
    try:
        RP.ensure_task_dirs("t_tmp_cwd")
        res = call_tool("shell_exec", {"command": "echo probe > probe_marker.txt", "shell": _SHELL})
        assert res.get("ok") is True, res
        assert (RP.task_tmp("t_tmp_cwd") / "probe_marker.txt").is_file(), "应落在 task tmp"
        assert not (REPO_ROOT / "probe_marker.txt").exists(), "产物不该落项目目录"
    finally:
        set_task(None)


# --- 终态清理 ---------------------------------------------------------------

def test_cleanup_removes_only_tmp_keeps_persistent_assets():
    """清理只删 `tmp/`；持久资产（task.json / trajectory / world_model / skills）必须完好。"""
    from omni_core.local.loop import ToolLoop

    tid = "t_tmp_clean"
    RP.ensure_task_dirs(tid)
    (RP.task_tmp(tid) / "scratch.py").write_text("print(1)", encoding="utf-8")
    RP.task_json(tid).write_text("{}", encoding="utf-8")
    RP.task_trajectory(tid).write_text("{}\n", encoding="utf-8")
    RP.task_world_model(tid).write_text("# wm", encoding="utf-8")

    ToolLoop._cleanup_task_tmp(tid)

    assert not RP.task_tmp(tid).exists(), "tmp/ 应被清掉"
    assert RP.task_json(tid).is_file()
    assert RP.task_trajectory(tid).is_file()
    assert RP.task_world_model(tid).is_file()
    assert RP.task_skills(tid).is_dir()


def test_cleanup_is_safe_on_missing_task_or_empty_id():
    from omni_core.local.loop import ToolLoop

    ToolLoop._cleanup_task_tmp("t_tmp_never_existed")  # 不抛
    ToolLoop._cleanup_task_tmp("")                     # 空 id 直接返回，不抛
