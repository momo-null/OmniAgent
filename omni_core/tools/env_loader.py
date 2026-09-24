"""环境装载器：按 ``config.runtime.backend`` 激活环境并注册其自带工具。

设计：
- 环境**单选**：激活的那个才注册它的工具；换后端需重启（重装）。
- 环境自有配置读 ``~/.omniagent/environments/<kind>.yaml``（不进 core config）。
- 内核与 ``devices/`` 都不点名任何具体环境（``devices.registry`` 懒发现 environments 包）。

返回内核持有的 ``ExecutionModule``（只暴露 ``kind`` / ``text_of`` / ``verify_done``）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from devices import registry
from devices.module import ExecutionModule


def read_env_config(kind: str) -> Dict[str, Any]:
    """读环境自有配置 ``~/.omniagent/environments/<kind>.yaml``（缺失返回空 dict）。"""
    try:
        from omni_core.local.runtime_paths import env_config_file
        path = env_config_file(kind)
    except Exception:
        return {}
    if not path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def active_kind(cfg: Optional[dict] = None) -> str:
    """当前选中的环境 kind（``runtime.backend``，缺省 host）。"""
    return str(((cfg or {}).get("runtime") or {}).get("backend") or "host")


def activate_environment(cfg: Optional[dict] = None) -> ExecutionModule:
    """激活当前环境：构造后端 + 导入其工具 + 绑定，返回内核句柄。

    环境未注册 / 构造失败会抛异常，由调用方决定降级策略。
    """
    cfg = cfg or {}
    kind = active_kind(cfg)
    backend = registry.create_backend(kind, read_env_config(kind))
    try:
        registry.bind_tools(kind, backend)
    except Exception:
        # 工具装载失败不应让整个 run 起不来；环境本身仍可用于 text_of / verify_done。
        pass
    return ExecutionModule(kind, backend, platform=str(getattr(backend, "platform", "") or ""))
