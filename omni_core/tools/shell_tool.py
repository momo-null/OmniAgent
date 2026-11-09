"""Shell 执行工具（内核 core · ① 执行原语）。

设计：
- core 内部三分：**① 执行原语**（本模块 = `shell_exec`）/ ② 编排能力（`skill` + 元工具）/
  ③ 接入机制（`local_model` / `mcp` / 模型路由）。宿主命令执行属 ①，与设备层的键鼠面并列
  （键鼠面归**环境**），**归内核 core、常开**；
- **常开 = 无开关，不是无条件**：本模块按环境自门决定注册与否（`available_shells`）。
  「常开」指的是没有 per-tool 开关可关它（U2 定案：工具清单只读），而一个环境里根本没有
  shell 可执行文件时，这个能力**不存在**——不注册、不进 agent 清单。这样 Android 沙箱
  （无 cmd / powershell / bash）不会再出现「调了才报未找到 shell 可执行文件」的白耗一步，
  内核与宿主也不需要任何平台分支：启停仍由工具自己按环境决定，与插件按 ``enabled``、
  MCP 按 mcp.json 同构；
- 它是唯一的命令执行入口：**执行 Python 请先写成 .py 脚本再运行**（不内联 `python -c`）；
- 命令的工作目录、相对路径基准默认 = 当前任务临时目录（`tasks/<task_id>/tmp/`）。

安全边界（工程兜底，**非安全沙箱**）：
- 每次执行起独立子进程，不污染主进程；
- 带墙钟超时，超时即杀；
- stdout / stderr 全程捕获并按上限截断，避免刷爆上下文。
（危险动作的人审批由安全线 S2 / full_access 承担，本工具不做内容级拦截。）

限流参数由 ``config.runtime.shell_exec`` 驱动（``configure`` 注入），不写死在工具里。
"""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.tools.base import function_tool
from omni_core.tools.workspace import task_tmp_dir


def _child_env() -> Dict[str, str]:
    """子进程环境：把仓库根加进 PYTHONPATH。

    任务脚本（含 project 目录里的项目脚本）因此可以 ``import omni_core``——
    这是通用能力，不为某个项目单开分支。
    """
    env = dict(os.environ)
    try:
        root = str(Path(__file__).resolve().parents[2])
        old = env.get("PYTHONPATH", "")
        if root not in old.split(os.pathsep):
            env["PYTHONPATH"] = root + (os.pathsep + old if old else "")
    except Exception:
        pass
    return env


_LIMITS = {
    "timeout_sec": 30.0,
    "max_output": 8000,
}

_SHELLS = {
    "cmd": ["cmd.exe", "/c"],
    "powershell": ["powershell.exe", "-NoProfile", "-Command"],
    "bash": ["bash", "-c"],
}

#: 缺省 shell 的偏好序（只在**本环境可用**的那些里挑）
_SHELL_PREFERENCE = ("powershell", "cmd", "bash")


def available_shells() -> List[str]:
    """本环境实际解析得到的 shell 名（按 ``_SHELLS`` 声明序，空表 = 没有执行能力）。"""
    return [name for name, argv in _SHELLS.items() if shutil.which(argv[0])]


def default_shell() -> str:
    """按偏好序取一个本环境可用的 shell 作为参数缺省；全不可用时回 powershell。"""
    avail = set(available_shells())
    for name in _SHELL_PREFERENCE:
        if name in avail:
            return name
    return "powershell"


#: 环境自门结论（import 时一次判定）：一个 shell 都探不到 → 不注册 shell_exec
_AVAILABLE_SHELLS = available_shells()


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


_DESCRIPTION = (
    "在宿主机执行命令（本环境可用：{shells}），返回输出。"
    "需要写 Python 时：先用 write_file 把代码写成 .py 脚本（落在当前任务目录），"
    "再用本工具运行 `python <脚本名>`；不要把大段 Python 内联进命令行（不要用 python -c）。"
    "命令的工作目录、以及相对路径基准，默认 = 当前任务目录。"
    "注意：此工具会在你的机器上真实执行命令。"
).format(shells=" / ".join(_AVAILABLE_SHELLS))


def shell_exec(
    command: str,
    shell: str = default_shell(),
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
            env=_child_env(),
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


# ── 环境自门：注册与否由本模块按环境自决 ────────────────────────────────────
# 探得到 shell 才把上面的函数注册为内核执行原语（与插件按 ``enabled``、MCP 按
# mcp.json 同构：启停在注册前决定，注册表不做过滤）。一个都探不到（Android 沙箱、
# 无 shell 的精简容器）时它只是模块级裸函数——``from omni_core.tools.shell_tool
# import shell_exec`` 照常成立，但注册表 / agent 清单里没有这个工具，也就不会再
# 出现「调了才发现没有 shell」的白耗一步。内核与宿主都不需要平台分支。
if _AVAILABLE_SHELLS:
    shell_exec = function_tool(
        name="shell_exec",
        description=_DESCRIPTION,
        unit="core",
        risk="exec",
    )(shell_exec)
