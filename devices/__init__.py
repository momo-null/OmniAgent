"""devices —— L1 设备能力层（屏幕/键鼠/模拟器驱动）。

物理位置说明（设计 §7-M3/M4）：
本包已**从内核 `omni_core/` 迁出**。`ExecutionBackend` 及其两个实现类
（Host / Emulator）属于「能力实现」，不是通用 agent 内核：

    L3 框架层        LangGraph / OpenAI Agents SDK（循环驱动）
    L2 护城河        omni_core/local/*（WorldModel / Curator / verify_done …）
    L1 能力层        omni_core/tools/*（tool 插件）+ devices/*（设备驱动）

能力怎么暴露给 LLM：由 `omni_core/tools/device_tool.py` 的 `function_tool`
插件声明 schema；本包只负责**执行**，不向内核反向注入任何工具清单。

内核与设备的唯一契约（去场景化 §9，属 L2 护城河）：
    backend.text_of(percept)            -> 感知文本化（后端自定形状）
    backend.verify_done(cond, percept)  -> 完成判定（后端自定语义）
内核永不读取 percept 内部字段。
"""
from devices.base import ExecutionBackend, ExecutionModuleProtocol
from devices.factory import ExecutionModule, create_backend
from devices.host import HostBackend
from devices.emulator import EmulatorBackend

__all__ = [
    "ExecutionBackend",
    "ExecutionModuleProtocol",
    "ExecutionModule",
    "create_backend",
    "HostBackend",
    "EmulatorBackend",
]
