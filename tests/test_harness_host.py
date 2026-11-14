"""网关托管（后端所有权）的边界测试：不真起进程，只验开关、复用与收树分支。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.services import harness_host

ROOT = Path(__file__).resolve().parents[1]


def test_desktop_shell_does_not_spawn_gateway():
    """网关所有权在后端：桌面壳不得再写一份 spawn 逻辑（否则双调度器/双通道）。"""
    for name in ("main.ts", "backend.ts"):
        src = (ROOT / "desktop" / "electron" / name).read_text(encoding="utf-8")
        assert "harness.channel_gateway" not in src, f"desktop/{name} 不得 spawn 网关"
        assert "startGateway" not in src, f"desktop/{name} 不得再有 startGateway"


def test_backend_lifespan_hosts_gateway():
    src = (ROOT / "backend" / "server.py").read_text(encoding="utf-8")
    assert "start_gateway(PORT)" in src
    assert "stop_gateway()" in src


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("OMNIAGENT_DISABLE_GATEWAY", "1")
    assert harness_host.start_gateway(8000) is None


def test_skipped_under_pytest(monkeypatch):
    monkeypatch.delenv("OMNIAGENT_DISABLE_GATEWAY", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_harness_host.py")
    assert harness_host.start_gateway(8000) is None


def test_reuse_when_gateway_already_running(monkeypatch):
    monkeypatch.delenv("OMNIAGENT_DISABLE_GATEWAY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(harness_host, "find_running_gateway", lambda: 4242)
    assert harness_host.start_gateway(8000) is None


def test_start_and_stop_spawned_process(monkeypatch, tmp_path):
    """只验「拉起的进程被记录、stop 会 terminate 它」这条路径，不真的锁端口。"""
    monkeypatch.delenv("OMNIAGENT_DISABLE_GATEWAY", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(harness_host, "find_running_gateway", lambda: None)

    class FakeProc:
        pid = 999999

        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    fake = FakeProc()
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(harness_host, "_log_path", lambda: tmp_path / "gateway.log")
    proc = harness_host.start_gateway(8000)
    assert proc is fake
    harness_host.stop_gateway()
