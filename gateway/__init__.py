"""gateway —— 宿主层（harness）：通用消息通道网关 + 定时任务调度器。

归类（接入设计 v1.1）：
- 通道适配器（收消息/回发）与 cron 调度是**驱动 agent 的外部事件源**，与
  scripts/run_agent_autonomous.py 同层，不是插件、更不改内核（omni_core 零改动）。
- 发送能力在 plugins/messaging/（工具按行为命名，通道参数化）。
- dispatch 完全通道无关：只依赖 base.py 的 ReplyChannel 协议。
"""
