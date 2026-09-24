"""环境适配层 —— 内核唯一需要的契约（见 doc/plans/capability-unit-refactor-2026-09-24.md §5.5）。

内核（`tool_loop` / `WorldModel` / `verify`）**只**通过三个成员使用"环境"：

- ``kind``：环境标识（``"host"`` / ``"emulator"`` …），用于选择、日志、提示；
- ``text_of(percept)``：把环境产出的感知文本化（形状由环境自定）；
- ``verify_done(condition, percept)``：通用完成判定，返回 ``(passed, reason)``。

其余原语（键鼠 / 截图 / 层级树 / tap_by_id …）**不属于内核契约**——它们是各环境自带
工具的内部实现。故此处**不再**定义 ``execute_*`` / ``observe`` / ``tap_by_id`` 之类的厚接口
（host 与 Android 的这些操作语义本就不同，硬套同一接口是假统一）。
"""
from abc import abstractmethod
from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class Environment(Protocol):
    """环境的最小契约（结构型：具体实现 / 测试替身满足即可）。"""

    kind: str

    @abstractmethod
    def text_of(self, percept: Dict[str, Any]) -> str:
        """把环境产出的 percept 转纯文本。"""
        ...

    @abstractmethod
    def verify_done(self, condition: str, percept: Dict[str, Any]) -> tuple:
        """通用完成判定：``(passed: bool, reason: str)``。"""
        ...
