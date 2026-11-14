"""兼容 shim（阶段 2 过渡）：实现已搬到 ``plugins.xiaohongshu.channel``。阶段 6 删除本文件。"""
import sys as _sys

from plugins.xiaohongshu.channel import *  # noqa: F401,F403

_sys.modules[__name__] = _sys.modules["plugins.xiaohongshu.channel"]
