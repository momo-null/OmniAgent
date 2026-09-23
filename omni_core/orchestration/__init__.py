"""M7：LangGraph 编排层（去分层的通用多 agent 拓扑）。

本包只做「并发拓扑 + 轮次兜底」，不持有任何工具实现、不持有领域状态：
- graph.py : 主 agent 节点 + `Send` 扇出子 agent 节点；默认单 agent 跑完，
             只有主 agent 调 `dispatch` 产出计划时才并行扇出

没有 Manager/Worker 的角色分层——是否拆子 agent 由主 agent 决定（业务决定）。
执行单元（子 agent 内层循环 / 护城河落盘 / Curator）由 ToolLoop 注入，
本层零领域逻辑（设计 doc/plans/multi-agent-redesign-2026-09-13.md §2）。
"""
