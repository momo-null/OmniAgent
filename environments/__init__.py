"""环境实现包：每个子目录一个环境（自注册 ``kind`` + 自带工具面）。

新增环境 = 加一个 ``environments/<kind>/`` 目录，并在此 import 一行；删除环境反之
（删目录 + 删这行）。内核与 ``devices/`` 适配层都不点名任何具体环境。
"""
from environments import host, emulator  # noqa: F401  （触发各自注册）

__all__ = ["host", "emulator"]
