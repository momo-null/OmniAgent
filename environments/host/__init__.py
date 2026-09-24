"""host 环境：本机（Windows）—— pyautogui 键鼠 + mss 截图 + EasyOCR 观测。

- 自注册 ``kind="host"``；
- 自带工具面 ``environments/host/tools.py``（名字 / 参数 / 描述按本环境自然定义）。
"""
from devices.registry import register_environment
from environments.host.backend import HostBackend


def create(config):
    return HostBackend(config)


def bind_tools(backend):
    from environments.host import tools
    tools.bind(backend)


register_environment("host", create, bind_tools, title="本机（Windows）")
