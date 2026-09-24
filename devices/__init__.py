"""``devices`` —— **环境适配层**（内核唯一需要的环境契约 + 注册 + 句柄）。

职责（见 doc/plans/capability-unit-refactor-2026-09-24.md §5.5）：
- ``base.Environment``：内核唯一契约（``kind`` / ``text_of`` / ``verify_done``）；
- ``registry``：按 ``kind`` 注册 / 构造环境（不点名任何具体环境）；
- ``module.ExecutionModule``：内核持有的当前环境句柄。

具体环境实现（host / emulator …）在顶层 ``environments/`` 包里，各自自包含
（driver + 自带工具面）；**本包不 import 它们**（懒发现）。
"""
from devices.base import Environment
from devices.registry import (
    register_environment,
    create_backend,
    registered_kinds,
    list_environments,
    bind_tools,
)
from devices.module import ExecutionModule

__all__ = [
    "Environment",
    "register_environment",
    "create_backend",
    "registered_kinds",
    "list_environments",
    "bind_tools",
    "ExecutionModule",
]
