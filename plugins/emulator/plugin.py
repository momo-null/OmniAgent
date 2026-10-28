"""emulator 插件入口：Android（uiautomator2 / ADB）触控 / 按键码 / 层级树 / 截图能力。

与 web / filesystem 完全同级的普通插件——「操作 Android 设备」不是环境，是一个
默认关闭的能力开关。startup 构造后端并 bind 给工具面；关闭时（enabled=false）
loader 不装载本包，注册表里没有任何 Android 工具。

配置：``~/.omniagent/plugins/emulator.yaml``（缺省读 plugin.json 的 ``enabled``）；
兼容旧路径 ``~/.omniagent/environments/emulator.yaml``（一个版本期）。
"""
from __future__ import annotations

from typing import Dict, Any

from omni_core.tools.loader import plugin_config
from plugins.emulator.backend import EmulatorBackend
from plugins.emulator import tools as emulator_tools


def _read_own_config() -> Dict[str, Any]:
    """插件自有配置：优先 plugins/emulator.yaml，空则回退旧 environments/emulator.yaml。"""
    cfg = plugin_config("emulator")
    if cfg:
        return cfg
    try:
        from omni_core.local.runtime_paths import env_config_file

        path = env_config_file("emulator")
        if path.is_file():
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def startup(ctx) -> None:
    """装配：构造 EmulatorBackend 并绑定工具面（每个 ToolLoop 实例刷新一次）。

    设备**不在此连接**：EmulatorBackend.connect 懒连接（首次操作才连），
    设备不在线不影响插件装载与其他工具。
    """
    emulator_tools.bind(EmulatorBackend(_read_own_config()))
