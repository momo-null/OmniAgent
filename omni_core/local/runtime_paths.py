"""全局单层 .omniagent/ 目录模型（2026-08-01 引入，替代旧两层 + 按名称分区）。

设计（方案 C，参考 WorkBuddy；2026-10-02 知识分层 v2 起为 task/project/global 三层）：
- 唯一持久化根 ``~/.omniagent/``，不按业务名称分区。
- ``projects/<pid>/``：会话历史 jsonl + **project 级知识资产**（skills/、memory/）。
  ``pid`` = 工作目录 slug 或用户自定义 slug；无归属的 task 缺省落 ``default``
  项目（``auto_project_id`` 已废除——按会话造项目会让知识碎片化）。
- ``tasks/<task_id>/``：独立平铺的一等实体，只装**证据与运行态**
  （trajectory.jsonl / world_model.md / collected.json / task.json / tmp）。
- ``skills/`` ``memory/``：全局层（跨 project 精选资产）。

本模块是唯一知道目录布局的地方；其它模块从这里取路径，不写死业务语义
（北极星红线：内核零场景硬编码，路径只接收 project_id(slug) 与 task_id）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")

#: 无显式归属的 task / 会话缺省落这个项目（逻辑归属恒定存在，目录懒创建）
DEFAULT_PROJECT_ID = "default"

# 用户全局（跨项目）唯一持久化根
_GLOBAL = Path(os.path.expanduser("~")) / ".omniagent"


# ---- 全局基础目录 ----------------------------------------------------------
def global_omni() -> Path:
    """用户全局 ~/.omniagent 目录。"""
    return _GLOBAL


def global_skills() -> Path:
    return _GLOBAL / "skills"


# ---- F4.1 纪律文件（AGENTS.md，尾部重插） -----------------------------------
def global_agents_file() -> Path:
    """全局纪律文件 ``~/.omniagent/AGENTS.md``（跨 task 共享；不存在则零注入）。"""
    return _GLOBAL / "AGENTS.md"


def user_profile() -> Path:
    """全局用户画像 ``memory/user_profile.md``（跨 task 共享，P0 一等实体）。"""
    return _GLOBAL / "memory" / "user_profile.md"


def global_memory_db() -> Path:
    """全局记忆库 ``memory/index.db``（跨 project 共库；与项目记忆库同 schema）。

    存放跨项目仍然成立的记忆（用户偏好、通用工具规律）；真正的隔离单位是
    库本身（多 agent/多用户各开一个库），库内不分层。
    """
    return _GLOBAL / "memory" / "index.db"


def character_card() -> Path:
    """单角色助手角色卡 ``character.md``（人格设定，随 system prompt 注入）。"""
    return _GLOBAL / "character.md"


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


# ---- project（slug 分组：会话历史 + project 级知识资产） --------------------
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


def projects_root() -> Path:
    return _GLOBAL / "projects"


def project_dir(project_id: str) -> Path:
    return _child(projects_root(), project_id, "project_id")


def project_skills(project_id: str) -> Path:
    """project 级技能 ``projects/<pid>/skills/``（结构层，跨 task 复用）。"""
    return _child(projects_root(), project_id, "project_id") / "skills"


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


def task_tmp(task_id: str) -> Path:
    """任务临时产物目录 ``tasks/<task_id>/tmp/``（脚本 / 截图等；任务终态由系统清理）。"""
    return task_dir(task_id) / "tmp"


def task_agents_file(task_id: str) -> Path:
    """任务级纪律文件 ``tasks/<task_id>/AGENTS.md``（不存在则零注入）。"""
    return task_dir(task_id) / "AGENTS.md"


def project_agents_file(project_id: str) -> Path:
    """项目级纪律文件 ``projects/<pid>/AGENTS.md``（不存在则零注入）。

    知识分层 v2：与 project memory / skills 同层同语义——用户单写、跨该项目
    全部任务共享；归属按 ``TaskStore.project_of(task_id)`` 解析。
    """
    return project_dir(project_id) / "AGENTS.md"


# ---- 插件 / 环境 自持配置（用户全局，按名字一文件） --------------------------
def plugins_root() -> Path:
    """插件自有配置根 ``~/.omniagent/plugins/``（每个插件一个 ``<name>.yaml``）。"""
    return _GLOBAL / "plugins"


def plugin_config_file(name: str) -> Path:
    """插件自有配置文件 ``plugins/<name>.yaml``（内置路径安全校验）。"""
    return _child(plugins_root(), name, "plugin_name").with_suffix(".yaml")


def environments_root() -> Path:
    """环境自有配置根 ``~/.omniagent/environments/``。"""
    return _GLOBAL / "environments"


def env_config_file(kind: str) -> Path:
    """环境自有配置文件 ``environments/<kind>.yaml``（内置路径安全校验）。"""
    return _child(environments_root(), kind, "env_kind").with_suffix(".yaml")


# ---- 目录确保（幂等） ------------------------------------------------------
def ensure_global_dirs() -> None:
    """服务启动时建好全局层所有子目录。幂等。"""
    for d in (
        global_skills(),
        projects_root(),
        tasks_root(),
        plugins_root(),
        environments_root(),
    ):
        d.mkdir(parents=True, exist_ok=True)


def ensure_task_dirs(task_id: str) -> None:
    """懒创建 task 目录（含 tmp 子目录）。幂等。

    知识分层 v2：skills 归 project 层（``project_skills``），task 目录只留证据与运行态。
    """
    for d in (task_dir(task_id), task_tmp(task_id)):
        d.mkdir(parents=True, exist_ok=True)
