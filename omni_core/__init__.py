"""OmniAgent 内核包（L2 护城河 / 编排层）。

M4 后的职责边界：
- 本包是**通用 agent 内核**：编排（tool_loop）、两次选举/升级策略（orchestration）、
  经验飞轮（local/world_model / curator / trajectory / skill_library）、提示词（brain/prompt）。
- **不含任何设备实现**：`ExecutionBackend` 及其 Host/Emulator 实现类已迁出到
  顶层 `devices/` 包（L1 能力层）；内核只经 `devices.ExecutionModule` 拿到一个
  后端句柄，用于 L2 的 `verify_done` / `text_of`（去场景化契约，§9）。
- **不含任何能力实现**：能力以平级 tool 插件形式挂在 `omni_core/tools/`
  （自研 device/vision/python + 外部 MCP），内核零持有、零按名分支派发。
"""
# 环境句柄由 omni_core.tools.activate_environment 在 ToolLoop 装配时构造
# （按 runtime.backend 单选环境）；本包不再直接 import devices。

__all__ = [
    "ExecutionModule",
]
