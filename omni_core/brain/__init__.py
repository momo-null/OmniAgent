"""模型客户端层（brain 即主 agent 所用的 LLM 客户端）。

提供 OpenAI 兼容的聊天/工具调用客户端，把"控制流"完全交给模型：
客户端 + Agents SDK 循环（Runner）+ 槽位路由（子 agent 按执行单元注册表取槽）。
"""
