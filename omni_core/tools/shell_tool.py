"""Shell 执行工具（内核 core · ① 执行原语）。

设计（见 doc/plans/capability-unit-refactor-2026-09-24.md §3 / §5）：
- core 内部三分：**① 执行原语**（本模块 = `shell_exec`）/ ② 编排能力（`skill` + 元工具）/
  ③ 接入机制（`local_model` / `mcp` / 模型路由）。宿主命令执行属 ①，与设备层的键鼠面并列
  （键鼠面归**环境**），**归内核 core、常开**；
- 它是唯一的命令执行入口：**执行 Python 请先写成 .py 脚本再运行**（不内联 `python -c`）；
- 命令的工作目录、相对路径基准默认 = 当前任务临时目录（`tasks/<task_id>/tmp/`）。

安全边界（工程兜底，**非安全沙箱**）：
- 每次执行起独立子进程，不污染主进程；
- 带墙钟超时，超时即杀；
- stdout / stderr 全程捕获并按上限截断，避免刷爆上下文。
（危险动作的人审批由安全线 S2 / full_access 承担，本工具不做内容级拦截。）

限流参数由 ``config.runtime.shell_exec`` 驱动（``configure`` 注入），不写死在工具里。
"""
import subprocess
from typing import Any, Dict, Optional

from omni_core.tools.base import function_tool
from omni_core.tools.workspace import task_tmp_dir


_LIMITS = {
    "timeout_sec": 30.0,
    "max_output": 8000,
}

_SHELLS = {
    "cmd": ["cmd.exe", "/c"],
    "powershell": ["powershell.exe", "-NoProfile", "-Command"],
    "bash": ["bash", "-c"],
}


def configure(cfg: Optional[dict]) -> None:
    """按 config.runtime.shell_exec 注入限流参数。"""
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
    name="shell_exec",
    description="在宿主机执行命令（cmd / powershell / bash），返回输出。"
                "需要写 Python 时：先用 write_file 把代码写成 .py 脚本（落在当前任务目录），"
                "再用本工具运行 `python <脚本名>`；不要把大段 Python 内联进命令行（不要用 python -c）。"
                "命令的工作目录、以及相对路径基准，默认 = 当前任务目录。"
                "注意：此工具会在你的机器上真实执行命令。",
    unit="core",
)
def shell_exec(
    command: str,
    shell: str = "powershell",
    cwd: str = "",
    timeout_sec: float = 0,
) -> Dict[str, Any]:
    """在宿主机执行 shell 命令。

    Args:
        command: 要执行的命令字符串
        shell: 后端 shell：cmd / powershell / bash（默认 powershell）
        cwd: 工作目录（留空 = 当前任务临时目录）
        timeout_sec: 超时秒数，缺省取配置值
    """
    command = command or ""
    if not command.strip():
        return {"ok": False, "error": "command 为空"}
    if shell not in _SHELLS:
        return {"ok": False, "error": f"不支持的 shell: {shell}，可选 {list(_SHELLS)}"}

    timeout = float(timeout_sec or 0) or _LIMITS["timeout_sec"]
    timeout = max(1.0, min(timeout, 300.0))
    limit = _LIMITS["max_output"]
    workdir = cwd or str(task_tmp_dir())

    try:
        proc = subprocess.run(
            _SHELLS[shell] + [command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=workdir,
            shell=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（>{timeout}s）", "timeout": True}
    except FileNotFoundError:
        return {"ok": False, "error": f"未找到 shell 可执行文件: {_SHELLS[shell][0]}"}
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
        "cwd": workdir,
    }
