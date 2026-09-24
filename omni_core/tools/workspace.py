"""当前任务的临时工作目录（ContextVar）。

设计：
- agent 自写的脚本 / 截图等**临时产物**默认落 ``tasks/<task_id>/tmp/``，**不进项目目录**；
- 用 ContextVar（而非模块全局）绑定，保证 M6 并发子任务各自独立、不串；
- run 起始由 ``graph_runner`` 绑定 task_id，任务终态由 finish 路径清理 ``tmp/``。

工具（filesystem 相对路径、shell cwd、环境截图默认路径）统一从这里取基准。
"""
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Optional, Union

from omni_core.local.runtime_paths import task_tmp

_current_task: ContextVar[Optional[str]] = ContextVar("omni_task_id", default=None)


def set_task(task_id: Optional[str]) -> None:
    """绑定当前任务的 task_id（None 表示解绑）。"""
    _current_task.set(str(task_id) if task_id else None)


def current_task() -> Optional[str]:
    """当前绑定的 task_id（未绑定返回 None）。"""
    return _current_task.get()


def task_tmp_dir() -> Path:
    """当前任务的临时目录；未绑定任务时回退当前工作目录（不炸）。"""
    tid = _current_task.get()
    if tid:
        p = task_tmp(tid)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return p
    return Path.cwd()


def resolve_path(path: Union[str, Path]) -> Path:
    """相对路径解析到当前任务临时目录；绝对路径原样返回。"""
    p = Path(path).expanduser()
    return p if p.is_absolute() else task_tmp_dir() / p
