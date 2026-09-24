"""O2 子进程编码统一单测（§Background ③ Windows GBK/UTF-8 混编）。

验证 `run_python` / `shell_exec` 子进程统一 UTF-8 解码后，
中文、emoji、特殊符号不再触发 Unicode 编解码异常。
不依赖 GPU / 模型 / 模拟器，纯子进程调用。
"""
import sys

import pytest

from omni_core.tools.base import call_tool
from omni_core.tools.loader import load_plugins


@pytest.fixture(autouse=True)
def _load_official_plugins():
    """P2：shell_exec 已迁为官方插件（plugins/shell），按名调用前需先装载。"""
    load_plugins({})


def test_run_python_utf8_chinese_emoji():
    """用例A：含中文 + emoji 代码，修复前 Windows 默认 GBK 会抛 UnicodeEncodeError。"""
    code = (
        's = "中文测试 🌸 hello 世界"\n'
        'print(s)'
    )
    res = call_tool("run_python", {"code": code})
    assert res.get("ok") is True, res
    assert "中文" in res["stdout"], res
    assert "🌸" in res["stdout"], res
    assert "世界" in res["stdout"], res


def test_run_python_ascii_compat():
    """用例B：纯 ASCII 代码基础兼容性，输出无误。"""
    res = call_tool("run_python", {"code": "print(2 ** 10)"})
    assert res.get("ok") is True, res
    assert res["stdout"].strip() == "1024", res


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only cmd shell encoding test")
def test_shell_exec_cmd_windows():
    """用例C：Windows 环境执行 cmd 命令，子进程 UTF-8 解码、执行成功、输出正常。"""
    res = call_tool("shell_exec", {"command": "echo OmniAgent-OK-编码测试", "shell": "cmd"})
    assert res.get("ok") is True, res
    assert "OmniAgent-OK" in res.get("output", "")
