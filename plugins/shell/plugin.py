"""Shell 执行官方插件（P2 自 omni_core/tools/shell_tool.py 迁出，函数体零改动）。

设计（与 python 工具同族）：补齐 agent 在宿主机跑命令的能力——脚本执行、系统操作、
自动化处理。用户点名的 cmd / powershell / bash 通过一个 `shell` 参数覆盖，避免工具列表
膨胀（工具过多会劣化 LLM 选型）。

安全边界（工程兜底，**非安全沙箱**；与 `run_python` 同类风险——python 工具本就能
`os.system` 调系统命令，shell 只是更直接）：
- 每次执行起独立子进程，不污染主进程；
- 带墙钟超时，超时即杀；
- stdout / stderr 全程捕获并按上限截断，避免刷爆上下文。

默认不启用（`group="shell"`），需 `config.runtime.tools.groups` 显式包含才暴露给模型。
"""
import subprocess
from typing import Any, Dict, Optional

from omni_core.tools.base import function_tool


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


def startup(ctx) -> None:
    """内核装配后按旧配置键 config.runtime.shell_exec 注入限流参数。

    迁移前该调用写在 ToolLoop.__init__ 里（内核点名 configure_shell）；
    现在由插件自己在 startup 时读取同一配置键，**用户配置零改动**。

    Args:
        ctx: PluginContext（读 ctx.config["runtime"]["shell_exec"]）。
    """
    cfg = getattr(ctx, "config", None)
    runtime = (cfg.get("runtime") or {}) if isinstance(cfg, dict) else {}
    configure(runtime.get("shell_exec") or {})


def _clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[截断 {len(text) - limit} 字符]"


@function_tool(
    description="在宿主机执行一条 shell 命令并返回其输出（独立子进程、带超时）。"
                "shell 可选 cmd / powershell / bash，覆盖系统命令、脚本运行、自动化处理。"
                "注意：此工具会在你的机器上执行命令，仅在明确开启时使用；用 echo/print 输出结果。",
    group="shell",
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
        cwd: 工作目录（留空=当前目录）
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

    try:
        proc = subprocess.run(
            _SHELLS[shell] + [command],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
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
    }
