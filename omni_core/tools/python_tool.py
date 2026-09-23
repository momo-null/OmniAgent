"""Python 执行自研外层 tool 插件（平级，由 LLM 直接调用）。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.5）：
补齐 agent 的「纯文本/计算」能力缺口——agent 可直接跑脚本做计算、数据处理、
文件整理等，无需一步一步点界面。

安全边界（工程兜底，非安全沙箱）：
- 每次执行起一个独立子进程（``python -I -c <code>``），不污染主进程；
- 带墙钟超时，超时即杀；
- stdout / stderr 全程捕获并按上限截断，避免刷爆上下文。

限流参数由 config.runtime.python_exec 驱动（``configure`` 注入），不写死在工具里。
"""
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from omni_core.tools.base import function_tool


# 由 ToolLoop 按 config 注入（缺省即内置安全值）
_LIMITS = {
    "timeout_sec": 10.0,
    "max_output": 4000,
}


def configure(cfg: Optional[dict]) -> None:
    """按 config.runtime.python_exec 注入限流参数。"""
    cfg = cfg or {}
    try:
        _LIMITS["timeout_sec"] = float(cfg.get("timeout_sec", _LIMITS["timeout_sec"]))
    except (TypeError, ValueError):
        pass
    try:
        _LIMITS["max_output"] = int(cfg.get("max_output", _LIMITS["max_output"]))
    except (TypeError, ValueError):
        pass


def _clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[截断 {len(text) - limit} 字符]"


@function_tool(
    description="执行一段 Python 代码并返回其输出（独立子进程、带超时）。"
                "用于计算、数据处理、文本/文件整理等纯计算任务；代码里用 print() 输出结果。",
    group="python",
)
def run_python(code: str, timeout_sec: float = 0) -> Dict[str, Any]:
    """在子进程里执行 Python 代码，返回 stdout / stderr / 退出码。

    Args:
        code: 要执行的 Python 代码（用 print() 输出需要的结果）
        timeout_sec: 超时秒数，缺省取配置值
    """
    code = code or ""
    if not code.strip():
        return {"ok": False, "error": "code 为空"}

    timeout = float(timeout_sec or 0) or _LIMITS["timeout_sec"]
    timeout = max(1.0, min(timeout, 300.0))
    limit = _LIMITS["max_output"]

    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=None,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（>{timeout}s）", "timeout": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    stdout = _clip((proc.stdout or "").rstrip(), limit)
    stderr = _clip((proc.stderr or "").rstrip(), limit)
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "output": stdout or stderr,
    }
