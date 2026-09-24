"""O2 子进程编码统一单测（§Background ③ Windows GBK/UTF-8 混编）。

验证 `shell_exec` 子进程统一 UTF-8 解码后，中文、emoji、特殊符号不再触发
Unicode 编解码异常。不依赖 GPU / 模型 / 模拟器，纯子进程调用。

（`run_python` 已随执行能力归一删除，命令执行唯一入口是内核 builtin `shell_exec`。）
"""
import sys

import pytest

from omni_core.tools.base import call_tool


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only cmd shell encoding test")
def test_shell_exec_cmd_windows():
    """Windows 环境执行 cmd 命令，子进程 UTF-8 解码、执行成功、输出正常。"""
    res = call_tool("shell_exec", {"command": "echo OmniAgent-OK-编码测试", "shell": "cmd"})
    assert res.get("ok") is True, res
    assert "OmniAgent-OK" in res.get("output", "")
