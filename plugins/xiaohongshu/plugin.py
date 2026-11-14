"""小红书只读采集插件（工具源 = 外部 MCP）。

工具面不在本模块注册：manifest 的 ``tools`` 声明 ``source: mcp`` + ``server`` +
``include`` 白名单，宿主只登记「这些工具由哪个 server 提供」，注册交给 MCP 层
（见 ``omni_core/tools/loader.py``）。零写操作由白名单表达：只有 list/search/detail
三个只读工具名，发布/评论类工具不可能出现在清单里。

归一逻辑的唯一实现在 ``plugins/xiaohongshu/normalize.py``（MCP 的 transform 钩子直接
复用同一份），本模块不重复实现。
"""
from __future__ import annotations

__all__: list = []
