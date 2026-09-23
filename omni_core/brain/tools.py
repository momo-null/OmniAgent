"""大脑可调用的工具 schema（OpenAI function JSON）+ 内核元工具常量。

M3 之后（设计 §5.0 / §7-M3）：
- 具体能力（设备/视觉/Python/外部 MCP）**不再在此派发**——它们已作为平级插件
  注册在 agent 外层 tool 插件层 `omni_core.tools`，由框架/LLM 直接调用。
- 本模块只保留两类内容：
  1) 内核元工具 schema（plan / verify / escalate / task_done / record），
     由 tool_loop 门控处理，不属于任何后端能力；
  2) 供 prompt 与后端适配器引用的 schema 常量（host / emulator 能力清单）。
"""
import time

# ---------------------------------------------------------------------------
# 工具 schema（OpenAI function JSON）
# ---------------------------------------------------------------------------
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "press",
            "description": "按下并释放单个键，如 enter / space / esc / a / win",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "键名，例如 enter、space、win、a"}
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hotkey",
            "description": "组合键，keys 为逗号分隔的字符串，如 'ctrl,c' 或 'win'",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string", "description": "逗号分隔的键，例如 'ctrl,c' 或 'win'"}
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "输入一段文本或字符串，例如数学表达式 '123+456='",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要输入的文本"}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_screen_text",
            "description": "读取当前屏幕上的文字（从系统控件层级树提取，快；GPU 渲染的界面无文字则返回空）。"
                           "若为空且需要界面内文字，换用 ocr_screenshot。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "observe",
            "description": "取得当前屏幕摘要：活动窗口标题 + 层级树 OCR 文字。不含图像 OCR，GPU 渲染界面文字换 ocr_screenshot。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ocr_screenshot",
            "description": "对当前屏幕截图做图像 OCR（EasyOCR GPU），识别 GPU 渲染的文字（自绘/自定义 UI）。"
                           "比 observe/read_screen_text 慢（~0.7s），但能读到层级树外的文字。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "等待若干毫秒，用于等待动画或程序加载",
            "parameters": {
                "type": "object",
                "properties": {
                    "ms": {"type": "integer", "description": "等待的毫秒数，例如 500"}
                },
                "required": ["ms"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "点击归一化坐标(0~1)。MVP 计算器场景通常不需要鼠标点击",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "横坐标 0~1"},
                    "y": {"type": "number", "description": "纵坐标 0~1"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
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
    },
]

# ---------------------------------------------------------------------------
# Emulator 后端工具 schema（超集）
# ---------------------------------------------------------------------------
# 在 HostBackend 的 TOOL_SCHEMAS 基础上，增加模拟器专属工具：
#  - get_ui_tree：结构化 UI 层级（dump_hierarchy，文本化决策，不靠像素）
#  - tap_by_id：按控件 resource-id 可靠点击（比坐标更稳）
#  - launch_app：启动指定包名应用
#  - press_keycode：按 Android keycode（如 3=HOME, 4=BACK）
#  - screenshot：截图像素（喂本地 VLM / 大脑 vision）
# click 语义在 emulator 下为「屏幕归一化坐标 tap」，与 host 的鼠标点击同名不同设备。
EMULATOR_TOOL_SCHEMAS = TOOL_SCHEMAS + [
    {
        "type": "function",
        "function": {
            "name": "get_ui_tree",
            "description": "获取当前界面的结构化 UI 层级（控件文本/坐标/resource-id），用于文本化决策，不依赖截图",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tap_by_id",
            "description": "按控件的 resource-id 点击（如 'com.android.calculator2:id/digit_7'），比坐标更可靠",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string", "description": "控件的 Android resource-id"}
                },
                "required": ["resource_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_app",
            "description": "启动指定包名的应用，如 'com.android.calculator2'",
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string", "description": "应用包名"}
                },
                "required": ["package"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_keycode",
            "description": "按下 Android 按键码，如 3=HOME, 4=BACK, 66=ENTER, 24=音量+",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "integer", "description": "Android keycode 整数"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "截取当前屏幕图像并返回路径，用于需要视觉判断时（喂本地 VLM 或回传大脑）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tap_text",
            "description": "OCR 定位屏幕上的指定文字并点击其中心。对『带文字的按钮/标签』这是最可靠"
                           "的点击方式（EasyOCR 精确边界框，无 VLM 坐标漂移），优先于 som_marks/裸 click。"
                           "找不到时返回当前屏幕可见文字列表供参考。仅适用于有文字的目标；纯图标才用 som_marks。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要点击的文字，如 '确定'（精确匹配优先，支持子串）"}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "collect_list",
            "description": "一次性采集当前列表/页面上『所有条目』的文本，内部自动截图→OCR→"
                           "上滑翻页→去重，直到连续两屏无新条目。返回全部条目的纯文本列表（条目即屏幕上的文字）。"
                           "用于『统计/列出某列表所有项』类任务（如统计某界面全部条目）——比逐屏手记 OCR 可靠得多，"
                           "一次调用即可抓全整列表。结果会持久化到世界模型，整任务成功以『实际采集到数据』为准。",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_pages": {"type": "integer", "description": "最多翻页次数，默认 40"},
                },
            },
        },
    },
]

# 通用『采集记录』工具 schema（host / emulator 共用，纯世界模型写入，无设备动作）
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

# record 同时进入 host / emulator 两套 schema（纯世界模型写入，无设备动作，由 tool_loop 拦截处理）
TOOL_SCHEMAS.append(RECORD_TOOL_SCHEMA)
EMULATOR_TOOL_SCHEMAS.append(RECORD_TOOL_SCHEMA)

# ---------------------------------------------------------------------------
# M2 / M2.5 视觉通道工具 schema（本地 VLM + SoM）
# ---------------------------------------------------------------------------
# 仅在 runtime.vision.enabled 时由 tool_loop 附加到基座 schema 之后。
# 工具对称陈列，agent 自主选用（不强制顺序）：
#  - vision_describe：送当前截图 + 问题给本地视觉模型，返回文本理解
#  - som_ground：    对当前 UI 树做结构化 Set-of-Marks，返回编号 marks
#                     （mark id -> resource-id），适合原生 App。
#  - som_marks：     视觉 Set-of-Marks，把截图送本地视觉模型让其识别可交互区域并编号，
#                     返回 {id, 归一化坐标, 标签}，适合无结构化层级的界面（自绘/Unity 渲染）。
#  - tap_by_mark：   按 SoM mark id 点击（结构化 mark 映射 resource-id；视觉 mark 映射坐标）。
VISION_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "vision_describe",
            "description": "把当前屏幕截图送给本地视觉模型做理解，返回文本回答。"
                           "用于需要『看』的场景：识别图标含义、判断界面状态、定位目标元素。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "对截图提出的问题，例如『当前在哪一步？''右上角的按钮是什么？'''"}
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "som_ground",
            "description": "对当前界面做 Set-of-Marks 标注：把可交互元素编号，返回 marks 列表"
                           "（每项含 mark id、resource-id、文本、坐标）。先调它拿到编号，再用 tap_by_mark 点击。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tap_by_mark",
            "description": "按 SoM mark id 点击对应控件。结构化 mark（som_ground）映射到 resource-id；"
                           "视觉 mark（som_marks）映射到归一化坐标。比盲猜坐标更可靠",
            "parameters": {
                "type": "object",
                "properties": {
                    "mark_id": {"type": "integer", "description": "som_ground / som_marks 返回的 marks 中的 id"}
                },
                "required": ["mark_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "som_marks",
            "description": "视觉 Set-of-Marks：把当前截图送本地视觉模型，让它识别画面中"
                           "可交互/关键区域并编号标注，返回 marks 列表（每项含 id、归一化坐标、标签）。"
                           "无结构化 UI 树的界面（自绘/Unity 渲染，som_ground 拿不到元素）必须用此工具。"
                           "先调它拿到编号，再用 tap_by_mark 点击",
            "parameters": {
                "type": "object",
                "properties": {
                    "ask": {
                        "type": "string",
                        "description": "可选，提示视觉模型重点关注什么，例如『列出所有可点击的按钮及其位置』",
                    }
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# M3 通用工具 schema（场景无关能力扩展）
# ---------------------------------------------------------------------------
# 由 M3ToolProvider 暴露，agent 自主选用：
#  - template_match：模板图像匹配定位（按外观点，不依赖文字/UI 树），自定义控件通用
#  - wait_for：      轮询等待指定文字出现（等加载/弹窗/按钮）
#  - drag：          归一化坐标拖拽（滑动列表/拖动元素/摇杆）
M3_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "template_match",
            "description": "在屏幕上用模板图像匹配定位元素：给定一张小图(template_path)，"
                           "在当前截图里找最相似位置，返回归一化中心坐标(0~1)与匹配分数。"
                           "适合『按图标/按钮外观点击』而不依赖文字或 UI 树（自定义控件通用）。"
                           "找到后可接 click(归一化坐标)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "template_path": {"type": "string", "description": "模板图像路径（要找的小图）"},
                    "screenshot_path": {"type": "string", "description": "可选，指定截图路径；不填则用当前屏幕截图"},
                    "threshold": {"type": "number", "description": "匹配分数阈值(0~1)，默认 0.8；低于则判定未找到"}
                },
                "required": ["template_path"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for",
            "description": "轮询等待屏幕上出现指定文字（OCR/UI 树），出现或超时后返回。"
                           "用于『等加载/等弹窗/等某按钮出现』再继续。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "等待出现的文字"},
                    "timeout_ms": {"type": "integer", "description": "最长等待毫秒，默认 5000"},
                    "interval_ms": {"type": "integer", "description": "轮询间隔毫秒，默认 300"}
                },
                "required": ["text"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drag",
            "description": "从一点拖到另一点（归一化坐标 0~1）。用于滑动列表、拖动元素等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_x": {"type": "number", "description": "起点横坐标 0~1"},
                    "from_y": {"type": "number", "description": "起点纵坐标 0~1"},
                    "to_x": {"type": "number", "description": "终点横坐标 0~1"},
                    "to_y": {"type": "number", "description": "终点纵坐标 0~1"}
                },
                "required": ["from_x", "from_y", "to_x", "to_y"]
            },
        },
    },
]


# ---------------------------------------------------------------------------
# M3b 两层编排：worker（本地模型）专属元工具 schema
# ---------------------------------------------------------------------------
# 仅在两层模式下，由 tool_loop 附加到 worker 的工具清单（单大脑 run_task 不加）。
#  - escalate：本地执行器判断搞不定时，把现场交回在线大脑（manager）。
#  - verify：  每个动作后校验当前屏幕是否达成条件（OCR 文字命中）；连续失败触发升级。
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
        "description": "校验当前屏幕是否已达成某条件（OCR 文字命中）。每个动作后调用以确认进展；"
                       "连续失败会触发升级，把子任务交回在线大脑。",
        "parameters": {
            "type": "object",
            "properties": {
                "condition": {
                    "type": "string",
                    "description": "期望在当前屏幕出现的文字，例如『登录成功』；留空则用任务完成条件",
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
# 工具=插件，自研 tool 与外部 MCP 在 omni_core.tools 平级注册、按名派发，
# 内核零持有、零分支（设计 §5.0 / §7-M3）。本模块仅保留内核元工具的 schema
# 常量（plan/verify/escalate/task_done/record 等由 tool_loop 门控处理）。
