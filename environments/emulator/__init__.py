"""emulator 环境：Android（uiautomator2 / ADB + EasyOCR GPU）。

- 自注册 ``kind="emulator"``；
- 自带工具面 ``environments/emulator/tools.py``（含 Android 专属：层级树 / 控件点击 /
  按键码 / 图像 OCR / 列表采集 / adb 文件与 shell）。
"""
from devices.registry import register_environment
from environments.emulator.backend import EmulatorBackend


def create(config):
    return EmulatorBackend(config)


def bind_tools(backend):
    from environments.emulator import tools
    tools.bind(backend)


register_environment("emulator", create, bind_tools, title="Android 模拟器")
