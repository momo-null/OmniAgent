"""M4 清理与 lint 固化验证。

覆盖：
- 设备实现类已物理迁出内核（omni_core 内零 ExecutionBackend 实现）
- review_lint 自动发现 + 默认零红线
- 旧手搓 ReAct 循环已标记废弃
- 手搓派发入口已从内核删除
"""
import inspect
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _repo_py_files():
    return [
        p.relative_to(ROOT).as_posix()
        for p in ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and ".venv" not in p.parts
    ]


# --- M4a 设备实现迁出内核 ---------------------------------------------------
def test_no_execution_backend_impl_in_kernel():
    core = ROOT / "omni_core"
    for name in ("execution.py", "execution_base.py", "execution_host.py", "execution_emulator.py"):
        assert not (core / name).exists(), f"{name} 应已迁出内核"

    import devices

    for cls in ("ExecutionBackend", "HostBackend", "EmulatorBackend", "ExecutionModule"):
        assert hasattr(devices, cls), f"devices 包应导出 {cls}"


def test_kernel_does_not_reference_device_modules():
    bad = [
        rel
        for rel in _repo_py_files()
        if rel.startswith("omni_core/")
        and "tools/" not in rel
        and "omni_core/execution" in (ROOT / rel).read_text(encoding="utf-8")
    ]
    assert bad == [], f"内核仍引用已迁出的设备模块: {bad}"


# --- M4b lint 固化 ----------------------------------------------------------
def test_review_lint_strict_passes():
    proc = subprocess.run(
        [sys.executable, "scripts/review_lint.py", "--strict"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout


def test_review_lint_discovers_new_kernel_files(tmp_path):
    """自动发现：往内核丢一个含红线的新文件，应被抓到（不依赖手工登记清单）。"""
    probe = ROOT / "omni_core" / "_m4_lint_probe.py"
    probe.write_text(
        "def f(percept):\n    return percept.get('ocr_text')\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [sys.executable, "scripts/review_lint.py", "--strict"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1
        assert "_m4_lint_probe.py" in proc.stdout
    finally:
        probe.unlink(missing_ok=True)


# --- M4c CI -----------------------------------------------------------------
def test_ci_workflow_runs_strict_lint():
    wf = ROOT / ".github" / "workflows" / "redline-lint.yml"
    assert wf.exists(), "review_lint 应已接入 CI"
    body = wf.read_text(encoding="utf-8")
    assert "review_lint.py --strict" in body


# --- M4d/e 协议清理与废弃标记 ----------------------------------------------
def test_legacy_loop_fully_removed():
    """M4/M5：旧手搓 ReAct 循环及其废弃标记已彻底移除（功能由 SDK Runner 取代）。"""
    import omni_core.local.tool_loop as tool_loop

    src = Path(inspect.getfile(tool_loop)).read_text(encoding="utf-8")
    assert "def _run_inner(" not in src
    assert "_warn_legacy_loop" not in src
    assert "def run_task_two_layer(" not in src
    assert "DEPRECATED(M4)" not in src
    assert not hasattr(tool_loop, "_warn_legacy_loop")
    assert not hasattr(tool_loop, "_LEGACY_LOOP_WARNED")


def test_brain_reply_no_dead_raw_field():
    """M4：已被 SDK 覆盖的协议层中，从未被消费的 raw 字段已删除。"""
    from omni_core.brain.llm import BrainReply

    assert not hasattr(BrainReply(), "raw")


def test_kernel_brain_has_no_handrolled_protocol():
    """M4d：内核 brain 层不再有任何手搓 OpenAI 协议实现（httpx / choices 解析）。"""
    brain_dir = ROOT / "omni_core" / "brain"
    offenders = []
    for p in brain_dir.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        body = p.read_text(encoding="utf-8")
        if "import httpx" in body or '["choices"]' in body or "choices\"][0]" in body:
            offenders.append(p.relative_to(ROOT).as_posix())
    assert offenders == [], f"内核仍存在手搓协议代码: {offenders}"
    # 手搓 client 文件已删除，协议改由 SDK Model 提供
    assert not (brain_dir / "client.py").exists()
    assert (brain_dir / "llm.py").exists()


def test_handrolled_dispatch_entrypoints_gone():
    from omni_core.brain import tools as brain_tools

    assert not hasattr(brain_tools, "build_registry")
    assert not hasattr(brain_tools, "dispatch_tool")
