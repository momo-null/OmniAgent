"""全局单层 .omniagent/ 目录模型（2026-08-01 引入，替代旧两层 + 按名称分区）。

设计（方案 C，参考 WorkBuddy）：
- 唯一持久化根 ``~/.omniagent/``，不按业务名称分区。
- ``projects/<path-slug>/``：仅装会话历史 jsonl（project = 工作目录的 slug 分组）。
- ``tasks/<task_id>/``：独立平铺的一等实体，内含自身全部资产
  （trajectory.jsonl / world_model.md / collected.json / skills/ / task.json）。
- ``skills/`` ``memory/``：全局能力，跨 task / project 共享。

本模块是唯一知道目录布局的地方；其它模块从这里取路径，不写死业务语义
（北极星红线：内核零场景硬编码，路径只接收 project_id(slug) 与 task_id）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

# 用户全局（跨项目）唯一持久化根
_GLOBAL = Path(os.path.expanduser("~")) / ".omniagent"


# ---- 全局基础目录 ----------------------------------------------------------
def global_omni() -> Path:
    """用户全局 ~/.omniagent 目录。"""
    return _GLOBAL


def global_skills() -> Path:
    return _GLOBAL / "skills"


def global_memory() -> Path:
    return _GLOBAL / "memory"


# ---- F4.1 纪律文件（AGENTS.md，尾部重插） -----------------------------------
def global_agents_file() -> Path:
    """全局纪律文件 ``~/.omniagent/AGENTS.md``（跨 task 共享；不存在则零注入）。"""
    return _GLOBAL / "AGENTS.md"


# ---- 全局长期记忆（memory/，跨 task 复用的沉淀资产） -----------------------
def memory_rollouts() -> Path:
    """记忆回放文件根目录 ``memory/rollouts/``。

    全局记忆资产（rollout 蒸馏）删除权统一归 Curator prune 策略管控，
    其它模块（如 delete_task）严禁清理此目录，避免历史经验随任务陪葬。
    """
    return global_memory() / "rollouts"


def memory_rollout_file(task_id: str) -> Path:
    """单任务蒸馏记忆 md 路径（``memory/rollouts/<task_id>.md``）。

    内置路径安全校验：task_id 必须经过通用标识符校验，杜绝路径穿越。
    全局记忆删除权归 Curator prune。
    """
    name = validate_identifier(task_id, "task_id")
    return memory_rollouts() / f"{name}.md"


def memory_master() -> Path:
    """全局长期记忆 ``memory/MEMORY.md``（仅追加、支持人工编辑、带 token 上限管控）。"""
    return global_memory() / "MEMORY.md"


def memory_summary() -> Path:
    """注入专用视图 ``memory/memory_summary.md``（自动截断、可随时再生）。"""
    return global_memory() / "memory_summary.md"


def validate_identifier(value: str, label: str) -> str:
    """验证目录名使用的通用标识符，拒绝路径片段和保留名。

    Args:
        value: 待验证的标识符。
        label: 错误信息使用的字段名。

    Returns:
        去除首尾空白后的安全标识符。

    Raises:
        ValueError: 标识符为空、格式不安全或为 Windows 保留名时抛出。
    """
    normalized = (value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ValueError(f"{label} 必须是 1-80 位字母、数字、下划线或连字符")
    if normalized.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "LPT1"}:
        raise ValueError(f"{label} 不能使用系统保留名")
    return normalized


def _child(root: Path, identifier: str, label: str) -> Path:
    """返回 root 的直接安全子目录。

    Args:
        root: 父目录。
        identifier: 已由外部提供的目录标识符。
        label: 字段名称。

    Returns:
        经过 resolve 校验的安全子目录路径。

    Raises:
        ValueError: 路径不属于 root 直接子目录时抛出。
    """
    name = validate_identifier(identifier, label)
    parent = root.resolve()
    candidate = (parent / name).resolve()
    if candidate.parent != parent:
        raise ValueError(f"{label} 不允许越出存储根目录")
    return candidate


# ---- project（工作目录 slug 分组） ----------------------------------------
def slugify_path(path: str) -> str:
    """绝对路径 → slug（盘符小写、分隔符换 '-'）。

    例：``D:\\AI\\OmniAgent`` → ``d-AI-OmniAgent``。
    无盘符的 POSIX 路径 ``/home/user/x`` → ``home-user-x``。
    """
    p = (path or "").replace("\\", "/").strip().rstrip("/")
    # 盘符 C: → c
    p = re.sub(r"^([a-zA-Z]):", lambda m: m.group(1).lower(), p)
    # 非字母数字（含 / _ . 空格）统一成 '-'（保留大小写，与 WorkBuddy 一致）
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", p).strip("-")
    return slug or "unknown"


def auto_project_id() -> str:
    """无工作目录时自动造 project id：``OmniAgent-<date>-task-N``。

    保证任何 task 都有 project 归属，避免 null 分支。
    """
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d")
    base = f"OmniAgent-{date}-task"
    root = projects_root()
    n = 1
    while (root / f"{base}-{n}").exists():
        n += 1
    pid = f"{base}-{n}"
    (root / pid).mkdir(parents=True, exist_ok=True)  # 占位，保证唯一
    return pid


def projects_root() -> Path:
    return _GLOBAL / "projects"


def project_dir(project_id: str) -> Path:
    return _child(projects_root(), project_id, "project_id")


def session_file(project_id: str, session_id: str) -> Path:
    session = validate_identifier(session_id, "session_id")
    return project_dir(project_id) / f"{session}.jsonl"


# ---- task（独立平铺的一等实体） -------------------------------------------
def tasks_root() -> Path:
    return _GLOBAL / "tasks"


def task_dir(task_id: str) -> Path:
    return _child(tasks_root(), task_id, "task_id")


def task_json(task_id: str) -> Path:
    return task_dir(task_id) / "task.json"


def task_trajectory(task_id: str) -> Path:
    return task_dir(task_id) / "trajectory.jsonl"


def task_world_model(task_id: str) -> Path:
    return task_dir(task_id) / "world_model.md"


def task_collected(task_id: str) -> Path:
    return task_dir(task_id) / "collected.json"


def task_subtasks(task_id: str) -> Path:
    """M6：子任务板（多 agent 共享的领取/状态表）。"""
    return task_dir(task_id) / "subtasks.json"


def task_skills(task_id: str) -> Path:
    return task_dir(task_id) / "skills"


def task_agents_file(task_id: str) -> Path:
    """任务级纪律文件 ``tasks/<task_id>/AGENTS.md``（不存在则零注入）。"""
    return task_dir(task_id) / "AGENTS.md"


# ---- 目录确保（幂等） ------------------------------------------------------
def ensure_global_dirs() -> None:
    """服务启动时建好全局层所有子目录。幂等。"""
    for d in (
        global_skills(),
        global_memory(),
        memory_rollouts(),
        projects_root(),
        tasks_root(),
    ):
        d.mkdir(parents=True, exist_ok=True)


def ensure_task_dirs(task_id: str) -> None:
    """懒创建 task 目录（含 skills 子目录）。幂等。"""
    for d in (task_dir(task_id), task_skills(task_id)):
        d.mkdir(parents=True, exist_ok=True)
