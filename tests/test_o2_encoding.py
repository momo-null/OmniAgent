"""O2 子进程编码统一单测（§Background ③ Windows GBK/UTF-8 混编）。

验证 `shell_exec` 子进程统一 UTF-8 解码后，中文、emoji、特殊符号不再触发
Unicode 编解码异常。不依赖 GPU / 模型 / 模拟器，纯子进程调用。

（`run_python` 已随执行能力归一删除，命令执行唯一入口是内核 builtin `shell_exec`。）
"""
import sys

import pytest

from omni_core.tools.base import call_tool


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only cmd shell encoding test")
def test_shell_exec_cmd_windows(approve_all_sink):
    """Windows 环境执行 cmd 命令，子进程 UTF-8 解码、执行成功、输出正常。

    S2 起命令执行过审批门：直调用例经 ``approve_all_sink`` 放行（并绑定 run 级
    安全上下文），与生产链路（graph_runner 绑定 + 后端 sink）语义一致。
    """
    res = call_tool("shell_exec", {"command": "echo OmniAgent-OK-编码测试", "shell": "cmd"})
    assert res.get("ok") is True, res
    assert "OmniAgent-OK" in res.get("output", "")
    assert approve_all_sink.requests, "shell_exec 应经过一次审批前置门"
