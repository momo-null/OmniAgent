"""OmniAgent 内核包（L2 框架外挂件 / 编排层）。

职责边界：
- 本包是**通用 agent 内核**：编排（tool_loop）、两次选举/升级策略（orchestration）、
  经验飞轮（local/world_model / trajectory / skill_library）、提示词（brain/prompt）。
- **不含任何设备实现、没有任何"环境"概念**：操作电脑 / Android 是普通能力插件
  （plugins/host、plugins/emulator，默认关闭），与 web / filesystem 同级；
  内核只经工具注册表与 capability registry 与它们打交道。
- **不含任何能力实现**：能力以平级 tool 插件形式挂在 `omni_core/tools/`
  （自研 + 外部 MCP），内核零持有、零按名分支派发。
"""
