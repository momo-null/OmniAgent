"""执行能力归一（§5）：shell 是内核 builtin、默认常开，且是唯一命令执行入口。

设计：
- `run_python` 已删除（python 改由 shell 承载：写脚本 → `python <脚本>`）；
- `shell_exec` 属内核 core，**不经任何插件 / 环境装载**即可用（默认常开）；
- 保留名保护：插件不得占用 `shell_exec`（否则可顶替 / 关掉唯一执行入口）。
"""
import importlib
from pathlib import Path

import pytest

import omni_core.tools  # noqa: F401  确保 builtin 已注册
from omni_core.tools import loader
from omni_core.tools.base import TOOL_REGISTRY, schemas

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_shell_exec_is_core_builtin():
    """只 import 内核 tools，shell_exec 就在册（不依赖任何插件 / 环境）。"""
    plugin = TOOL_REGISTRY["shell_exec"]
    assert plugin.source == "core"
    assert plugin.unit == "core"


def test_shell_exec_in_schema_export():
    assert "shell_exec" in {s["function"]["name"] for s in schemas()}


def test_shell_description_guides_to_script_not_inline():
    """描述是模型唯一的引导入口：必须导向「写脚本再执行」，并劝阻内联。"""
    desc = (TOOL_REGISTRY["shell_exec"].schema or {}).get("function", {}).get("description", "")
    assert "write_file" in desc and ".py" in desc
    assert "python -c" in desc       # 明确劝阻内联（不如写脚本可复用）
    assert "当前任务目录" in desc     # cwd / 相对路径基准的说明


def test_run_python_removed():
    assert "run_python" not in TOOL_REGISTRY
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("omni_core.tools.python_tool")


def test_shell_plugin_package_deleted():
    """shell 已归内核：`plugins/shell/` 不再有可装载入口（回不去插件形态）。"""
    pkg = REPO_ROOT / "plugins" / "shell"
    assert not (pkg / "plugin.py").exists()
    assert not (pkg / "plugin.json").exists()


def test_shell_exec_is_reserved_name():
    """保留名保护：插件不得占用（否则可顶替内核唯一执行入口）。"""
    assert "shell_exec" in loader.RESERVED_TOOL_NAMES
    assert "run_python" not in loader.RESERVED_TOOL_NAMES
