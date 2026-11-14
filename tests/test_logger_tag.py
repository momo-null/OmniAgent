"""utils.logger 每进程日志文件（2026-11-14）：tag 后缀 / pid 兜底 / 仓库根锚定。

背景：backend 与网关曾共享 ``temp/omniagent.log``——先到者握住句柄，后到者
轮转改名永远 PermissionError 且 rollover 失败的记录整条丢弃（实证丢日志 7 天）。
修复 = 每进程独占一份（OMNI_LOG_TAG 自报家门，未标识进程按 pid 隔离）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logger import _REPO_ROOT, _resolve_log_file  # noqa: E402


def test_tag_suffix_and_repo_anchor(monkeypatch):
    monkeypatch.setenv("OMNI_LOG_TAG", "backend")
    p = _resolve_log_file("./temp/omniagent.log")
    assert p.endswith("omniagent-backend.log")
    assert os.path.isabs(p)
    assert os.path.dirname(p) == os.path.join(_REPO_ROOT, "temp")


def test_pid_fallback(monkeypatch):
    monkeypatch.delenv("OMNI_LOG_TAG", raising=False)
    p = _resolve_log_file("./temp/omniagent.log")
    assert p.endswith(f"omniagent-p{os.getpid()}.log")


def test_custom_base_keeps_suffix(monkeypatch):
    monkeypatch.setenv("OMNI_LOG_TAG", "harness")
    assert _resolve_log_file("D:/somewhere/my.log").endswith("my-harness.log")
    assert _resolve_log_file("/var/log/omni.log").endswith("omni-harness.log")
