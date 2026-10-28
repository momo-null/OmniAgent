"""host 插件入口：本机（Windows）键鼠 / 截图 / 观察能力。

与 web / filesystem 完全同级的普通插件——「操作电脑」不是环境，是一个默认关闭的
能力开关。startup 构造后端并 bind 给工具面（工具由 ``@function_tool`` 在 import
时注册，loader 统一归 unit/source）；关闭时（enabled=false）loader 不装载本包，
注册表里没有任何本机操作工具。

配置：``~/.omniagent/plugins/host.yaml``（缺省读 plugin.json 的 ``enabled``）；
兼容旧路径 ``~/.omniagent/environments/host.yaml``（一个版本期，命中即迁移语义）。
"""
from __future__ import annotations

from typing import Dict, Any

from omni_core.tools.loader import plugin_config
from plugins.host.backend import HostBackend
from plugins.host import tools as host_tools


def _read_own_config() -> Dict[str, Any]:
    """插件自有配置：优先 plugins/host.yaml，空则回退旧 environments/host.yaml。"""
    cfg = plugin_config("host")
    if cfg:
        return cfg
    try:
        from omni_core.local.runtime_paths import env_config_file

        path = env_config_file("host")
        if path.is_file():
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def startup(ctx) -> None:
    """装配：构造 HostBackend 并绑定工具面（每个 ToolLoop 实例刷新一次）。"""
    host_tools.bind(HostBackend(_read_own_config()))
