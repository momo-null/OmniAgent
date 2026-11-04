"""shell_exec 环境自门（无可用 shell 的环境 = 没有这个工具）。

语义（见 omni_core/tools/shell_tool.py 模块头）：core 工具没有 per-tool 开关
（U2 定案），但「常开」指的是**无开关可关**，不是**无条件存在**。本环境解析不到
任何 shell 可执行文件时，本模块不注册 `shell_exec`——注册表 / agent 清单里没有它，
Android 沙箱因此不会再「先调再报未找到 shell 可执行文件」。

覆盖：有 shell → 在册且可派发 / 无 shell → 不在册、直调按未知工具拒、模块属性仍在
（`omni_core.tools` 的 import 不破）/ 缺省 shell 只在可用者里挑 / 描述只列可用 shell。
"""
import importlib

import pytest

import omni_core.tools.shell_tool as shell_tool
from omni_core.tools.base import TOOL_REGISTRY, call_tool, schemas

_NAME = "shell_exec"


@pytest.fixture(autouse=True)
def _restore_pristine_module():
    """用例只摆弄 reload 出来的替身模块，结束时一律按真实环境重载回原状。"""
    yield
    TOOL_REGISTRY.pop(_NAME, None)
    importlib.reload(shell_tool)


def _reload_without_shell(monkeypatch, available=("bash",)):
    """模拟「进程首次 import 本模块」：先摘掉在册项，再把探测替换成 ``available`` 重载。

    reload 只重跑模块体、不会撤销上一次的注册（真实进程里 import 只发生一次），
    所以必须先 pop 再 reload，「不在册」的断言才不是假绿。
    """
    def fake_which(exe, *a, **k):
        return f"/fake/bin/{exe}" if any(str(exe).startswith(name) for name in available) else None

    TOOL_REGISTRY.pop(_NAME, None)
    monkeypatch.setattr(shell_tool.shutil, "which", fake_which)
    return importlib.reload(shell_tool)


def test_registered_when_a_shell_exists(monkeypatch):
    mod = _reload_without_shell(monkeypatch, available=("bash",))
    assert mod.available_shells() == ["bash"]
    assert _NAME in TOOL_REGISTRY
    assert _NAME in {s["function"]["name"] for s in schemas()}


def test_absent_when_no_shell_exists(monkeypatch):
    mod = _reload_without_shell(monkeypatch, available=())
    assert mod.available_shells() == []
    # 不在册：agent 看不到它（schema 清单来自注册表视图）
    assert _NAME not in TOOL_REGISTRY
    assert _NAME not in {s["function"]["name"] for s in schemas()}
    # 直调也不可达（技能硬重放 / 脚本旁路都绕不过注册表）
    assert call_tool(_NAME, {"command": "echo 1"})["error"].startswith("unknown tool")
    # 模块属性仍是裸函数：omni_core.tools 的 `from shell_tool import shell_exec` 不破
    assert callable(mod.shell_exec)


def test_default_shell_only_picks_available(monkeypatch):
    """只有 bash 的环境不要把缺省写成 powershell（Windows 偏好序仍优先 powershell）。"""
    assert _reload_without_shell(monkeypatch, available=("bash",)).default_shell() == "bash"
    both = _reload_without_shell(monkeypatch, available=("bash", "powershell"))
    assert both.default_shell() == "powershell"
    assert _reload_without_shell(monkeypatch, available=()).default_shell() == "powershell"


def test_description_lists_only_available_shells(monkeypatch):
    mod = _reload_without_shell(monkeypatch, available=("bash",))
    desc = (TOOL_REGISTRY[_NAME].schema or {}).get("function", {}).get("description", "")
    assert "本环境可用：bash" in desc and "powershell" not in desc


def test_pristine_import_matches_this_machine():
    """真实环境自检：本机探得到 shell 才在册（Windows/Linux/macOS 三态一致）。"""
    TOOL_REGISTRY.pop(_NAME, None)
    fresh = importlib.reload(shell_tool)
    assert (_NAME in TOOL_REGISTRY) is bool(fresh.available_shells())
    if fresh.available_shells():
        assert TOOL_REGISTRY[_NAME].unit == "core"
        assert TOOL_REGISTRY[_NAME].meta.get("risk") == "exec"
