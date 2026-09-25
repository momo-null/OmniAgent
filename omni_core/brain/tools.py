"""大脑可调用的内核**元工具** schema（OpenAI function JSON）。

M3 之后（设计 §5.0 / §7-M3）：具体能力（设备 / 视觉 / 文件 / 联网 / shell …）已作为
平级工具注册在 ``omni_core.tools``（环境自带 / ``plugins/`` 插件 / 内核 builtin），
由 LLM 直接调用；内核零持有、零按名分支派发。

本模块**只**保留内核元工具的 schema 常量（plan / verify / escalate / task_done / record），
由 tool_loop 门控处理，不属于任何后端能力。

历史遗留的 ``TOOL_SCHEMAS`` / ``EMULATOR_TOOL_SCHEMAS`` / ``M3_TOOL_SCHEMAS``
（硬编码的一整套设备工具 schema）已删除——能力暴露的唯一来源是工具注册表
（``omni_core.tools.base.TOOL_REGISTRY``）。
"""

# 通用『采集记录』工具 schema（纯世界模型写入，无设备动作，由 tool_loop 拦截处理）
RECORD_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "record",
        "description": "把一条文本发现持久化到世界模型的『备注』区（跨翻页/子任务不丢），用于采集/统计类任务"
                       "暂存结论（如『采集完成：共 12 条』）。返回已记录条数。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要记录的文本"},
            },
            "required": ["text"],
        },
    },
}


# ---------------------------------------------------------------------------
# 元工具 schema（worker 专属 + 收尾）
# ---------------------------------------------------------------------------
# 仅在需要时由 tool_loop 附加到工具清单（单大脑 run_task 不加 escalate/verify）：
#  - escalate：本地执行器判断搞不定时，把现场交回在线大脑。
#  - verify：  每个动作后校验当前环境是否达成条件；连续失败触发升级。
ESCALATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "escalate",
        "description": "当你（本地执行器）判断当前子任务自己搞不定时，调用此工具把现场交回在线大脑重新决策。"
                       "例如：连续校验失败、遇到未知界面、无法匹配目标、上下文将溢出。",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "为什么需要升级，简述卡点"}
            },
            "required": ["reason"],
        },
    },
}

VERIFY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "verify",
        "description": "校验当前环境是否已达成某条件（环境自定的命中语义）。需要确认子任务进展时调用；"
                       "连续失败会触发升级，把子任务交回在线大脑。",
        "parameters": {
            "type": "object",
            "properties": {
                "condition": {
                    "type": "string",
                    "description": "期望达成的条件文本，例如『登录成功』；留空则用任务完成条件",
                }
            },
        },
    },
}

# 复用的 task_done schema（两层反思阶段让在线大脑也能 task_done 终止）
TASK_DONE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "task_done",
        "description": "声明任务完成或主动停止，并说明原因",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "完成原因或卡住说明"}
            },
            "required": ["reason"],
        },
    },
}

# worker 专属元工具集合（escalate + verify），由 tool_loop 在两层内层循环附加
WORKER_EXTRA_SCHEMAS = [ESCALATE_TOOL_SCHEMA, VERIFY_TOOL_SCHEMA]


# ---------------------------------------------------------------------------
# 派发：M3 起已移至 agent 外层 tool 插件层（omni_core.tools）
# ---------------------------------------------------------------------------
# 原 build_registry / dispatch_tool（Provider 聚合 + `if name==` 手搓派发）已删除：
# 工具=插件，环境自带 / 插件 / builtin 在 omni_core.tools 平级注册、按名派发，
# 内核零持有、零分支（设计 §5.0 / §7-M3）。本模块仅保留内核元工具的 schema 常量。
