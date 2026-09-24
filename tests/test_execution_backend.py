"""环境层单测（`devices/` 适配层 + `environments/`）。

设计见 doc/plans/capability-unit-refactor-2026-09-24.md §5.5：
- `devices/` 只剩内核需要的三件契约（`kind` / `text_of` / `verify_done`）+ 注册表 + 句柄；
- 具体环境（host / emulator）在 `environments/` **自注册**，自带 driver 与工具面；
- 内核**不点名任何环境**（按 `runtime.backend` 查表）；
- 环境工具随环境激活而注册（`source="env"`），不激活的不注册。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devices import (
    ExecutionModule,
    create_backend,
    list_environments,
    registered_kinds,
)
from omni_core.tools.base import TOOL_REGISTRY


# === 注册表：环境自报，内核零硬编码 ==========================================

def test_registered_environments_self_report():
    kinds = registered_kinds()
    assert "host" in kinds and "emulator" in kinds
    titles = {e["kind"]: e["title"] for e in list_environments()}
    assert titles["host"] and titles["emulator"]


def test_create_backend_unknown_kind_raises():
    with pytest.raises(KeyError):
        create_backend("nope", {})


def test_create_backend_by_kind():
    assert create_backend("host", {}).kind == "host"


# === 契约面：只有 kind / text_of / verify_done ==============================

def test_execution_module_is_thin_contract():
    em = ExecutionModule("host", create_backend("host", {}))
    assert em.kind == "host"
    assert callable(em.text_of) and callable(em.verify_done)
    # 兼容别名 `backend_kind` 已删（零兼容）
    assert not hasattr(em, "backend_kind")


def test_backends_declare_no_tool_schemas():
    """环境不向内核塞 schema：能力暴露的唯一来源是工具注册表。

    键鼠 / 截图 / OCR 等原语是各环境**内部**实现（供本环境工具调用），
    不再是 `devices/` 共享层的厚接口。
    """
    import devices

    backend = create_backend("host", {})
    assert not hasattr(backend, "tool_schemas")
    # 共享层不再定义厚接口基类
    assert not hasattr(devices, "ExecutionBackend")


# === 环境工具面：随激活而注册 ==============================================

def test_environment_activation_registers_its_tools():
    from omni_core.tools.env_loader import activate_environment

    activate_environment({"runtime": {"backend": "host"}})
    host_tools = {n for n, p in TOOL_REGISTRY.items() if p.source == "env"}
    assert {"press", "click", "observe", "screenshot"} <= host_tools
    # Android 专属工具不属于 host 环境
    assert "press_keycode" not in host_tools
    # 环境工具 unit = 环境 kind
    assert TOOL_REGISTRY["click"].unit == "host"
