"""兼容 shim：注册表与任务上下文已迁入 ``gateway.events.registry``（阶段 1）。

阶段 6 删除本文件；新代码请 ``from gateway.events import registry``。
下划线状态名显式再导出（同一对象），供存量测试引用；monkeypatch 请直接打
registry 模块（shim 上的属性赋值不会影响 registry 的模块级全局）。
"""
from gateway.events.registry import (  # noqa: F401
    _CURRENT_REPLY,
    _LOCK,
    _REGISTRY,
    _SENT,
    _SOURCE_EXTERNAL,
    _SOURCE_KIND,
    all_channels,
    build,
    get_adapter,
    get_current_reply,
    get_source_external,
    get_source_kind,
    note_sent,
    register,
    reset_sent,
    sent_count,
    set_current_reply,
    set_source_external,
    set_source_kind,
    take_sent,
)
