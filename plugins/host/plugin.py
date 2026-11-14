"""host 插件入口：本机（Windows）键鼠 / 截图 / 观察能力。

与 web / filesystem 完全同级的普通插件——「操作电脑」不是环境，是一个默认关闭的
能力开关。startup 构造后端并 bind 给工具面（工具由 ``@function_tool`` 在 import
时注册，loader 统一归 unit/source）；关闭时（enabled=false）loader 不装载本包，
注册表里没有任何本机操作工具。

配置：``~/.omniagent/plugins/host.yaml``（唯一事实源；缺省读 plugin.json 的 ``enabled``）。
"""
from __future__ import annotations

from typing import Dict, Any

from omni_core.tools.loader import plugin_config
from plugins.host.backend import HostBackend
from plugins.host import tools as host_tools


def _read_own_config() -> Dict[str, Any]:
    """插件自有配置 ``~/.omniagent/plugins/host.yaml``（缺失返回空 dict）。"""
    return plugin_config("host")


def startup(ctx) -> None:
    """装配：构造 HostBackend 并绑定工具面（每个 ToolLoop 实例刷新一次）。"""
    host_tools.bind(HostBackend(_read_own_config()))
