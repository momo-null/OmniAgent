"""M4a.4 Agent 显式状态机枚举。

供前端 SSE / 进度流（Master Spec §8）与 Debug；避免未来 if/else 失控（评审 Review-009）。
纯追踪层：不改变任何已有行为，仅在循环各阶段置 ``ToolLoop.state`` 并记录转移序列。
"""
from enum import Enum


class AgentState(str, Enum):
    INIT = "INIT"            # 实例就绪，尚未开始任务
    OBSERVING = "OBSERVING"  # 观测屏幕（observe）
    PLANNING = "PLANNING"    # 大脑规划 / 反思（plan / reflect）
    EXECUTING = "EXECUTING"  # 决策 + 派发工具（brain.chat → dispatch）
    VERIFYING = "VERIFYING"  # 显式校验阶段（M4a.3）
    DONE = "DONE"            # 任务成功
    FAILED = "FAILED"        # 任务失败
    PAUSED = "PAUSED"        # daemon 模式纯文本汇报后暂停，等待用户消息 / wake 端点续跑

    @property
    def is_terminal(self) -> bool:
        return self in (AgentState.DONE, AgentState.FAILED, AgentState.PAUSED)


# 合法的相邻转移（用于测试/断言，不强制约束运行期行为）。
LEGAL_TRANSITIONS = {
    AgentState.INIT: {AgentState.PLANNING, AgentState.OBSERVING, AgentState.EXECUTING},
    AgentState.OBSERVING: {AgentState.EXECUTING, AgentState.PLANNING, AgentState.VERIFYING, AgentState.DONE, AgentState.FAILED},
    AgentState.PLANNING: {AgentState.OBSERVING, AgentState.EXECUTING, AgentState.DONE, AgentState.FAILED},
    AgentState.EXECUTING: {AgentState.EXECUTING, AgentState.OBSERVING, AgentState.VERIFYING, AgentState.PLANNING, AgentState.DONE, AgentState.FAILED},
    AgentState.VERIFYING: {AgentState.OBSERVING, AgentState.EXECUTING, AgentState.DONE, AgentState.FAILED},
    AgentState.DONE: set(),
    AgentState.FAILED: set(),
    AgentState.PAUSED: set(),
}
