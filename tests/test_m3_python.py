"""M3 Python 执行工具单测（§5.5 纯计算能力补齐）。

子进程沙箱 + 超时 + 输出截断；不依赖 GPU / 模型 / 模拟器。
"""
from omni_core.tools.base import TOOL_REGISTRY, call_tool
from omni_core.tools.python_tool import configure


def test_run_python_registered_in_python_group():
    assert "run_python" in TOOL_REGISTRY
    assert TOOL_REGISTRY["run_python"].group == "python"


def test_run_python_captures_stdout():
    res = call_tool("run_python", {"code": "print(1+1)"})
    assert res["ok"] is True
    assert res["returncode"] == 0
    assert res["stdout"].strip() == "2"


def test_run_python_captures_stderr_on_error():
    res = call_tool("run_python", {"code": "raise ValueError('boom')"})
    assert res["ok"] is False
    assert res["returncode"] != 0
    assert "ValueError" in res["stderr"]


def test_run_python_timeout():
    res = call_tool("run_python", {"code": "import time; time.sleep(30)", "timeout_sec": 2})
    assert res["ok"] is False
    assert res.get("timeout") is True


def test_run_python_empty_code():
    res = call_tool("run_python", {"code": "   "})
    assert res["ok"] is False
    assert "code" in res["error"]


def test_run_python_truncates_huge_output():
    configure({"timeout_sec": 15, "max_output": 200})
    try:
        res = call_tool("run_python", {"code": "print('x' * 5000)"})
        assert res["ok"] is True
        assert len(res["stdout"]) <= 200 + len("...[截断 4800 字符]") + 10
        assert "截断" in res["stdout"]
    finally:
        configure({"timeout_sec": 15, "max_output": 4000})


def test_run_python_isolated_subprocess():
    """子进程执行：主进程命名空间不被污染（不共享 globals）。"""
    res = call_tool("run_python", {"code": "print(__name__)"})
    assert res["stdout"].strip() == "__main__"
