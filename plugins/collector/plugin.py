"""观察采集能力插件（H-6 干净引入 / P5 触发器骨架）。

**不写死任何渠道或平台**：去哪儿读、怎么读由调用方（未来的渠道插件 / 工具）决定，
本插件只暴露一个能力名 ``observation_collector``，把结构化条目送进
``capture_observation()`` 统一入口。删掉本目录即零痕迹（C-1 / C-4）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from omni_core.tools.capability import register_capability, unregister_capability

CAPABILITY = "observation_collector"


def collect(context_key: str, text: str, provenance: Dict[str, Any],
            source_external: Optional[bool] = None) -> str:
    """采集一条见闻。

    Args:
        context_key: 由边界层归一的不透明场合键。
        text: 见闻原文。
        provenance: 结构化出处（platform / surface / actor / ref 等哈希值），
            不含真实账号或 URL 明文；必填，缺出处不入库（H-7）。
        source_external: 边界层归一的结构信号（是否来自外部可达来源）；
            不传则由内核读 run 级信号，插件不推断场合键。
    """
    from omni_core import memory_brain

    return memory_brain.capture_observation(
        context_key, text, provenance=provenance,
        source_external=source_external)


def startup(ctx) -> None:
    register_capability(CAPABILITY, collect)


def shutdown() -> None:
    unregister_capability(CAPABILITY)
