> **设计来源文档**（框架接管 ReAct + 能力 MCP 化）。当前权威来源：
> - 架构与产品：`doc/agent-control-arch-design.html`（架构设计）、`doc/agent-control-product-design.html`（产品设计）
> - 规格：`doc/agent-control-master-spec.md`

# OmniAgent 内核重构设计：框架接管 ReAct + 能力 MCP 化

- 日期：2026-09-12
- 状态：**设计定稿**（框架选型 + 三层栈 + C1–C5 契约）。
- 基线：已包含「去屏幕假设」改动（`execution_base` / `world_model` / `tool_loop` 零感知字段假设，lint 23→0）。

---

## 1. 背景与痛点

当前 OmniAgent 内核已具备通用化骨架（设备抽象 `ExecutionBackend`、模型无关 `BrainClient`、动态 Prompt、两层编排 `tool_loop`、经验飞轮 `WorldModel`/`Curator`/`Trajectory`/`Skill`）。但存在以下结构性痛点，促使本次大重构：

1. **ReAct 单步循环手搓成本高**：`brain/client.py` + `tool_loop._run_inner` 自己实现了 function-call 序列化、工具派发、流式回调、assistant/tool 消息回填。前端「打招呼 + 工具调用 + 流式」折腾很久——这套协议层能力业界框架（OpenAI Agents SDK / LangGraph）已原生提供，重复造轮子。
2. **纯文本能力缺失**：当前 `brain/tools.py` 全是屏幕操作工具，无 Python 执行 / 文件处理等通用计算能力，agent 不能跑脚本。
3. **SoM 写死在内核**：`som_ground` / `som_marks` / `tap_by_mark` 散落在 `vision.py` + `m3.py` + `providers/vision.py`，且硬编码屏幕坐标+OCR。这违反「领域/能力不进内核」的红线（memory ID 85117719）。
4. **两层编排本质=多 agent**：手搓的 `Manager(在线大模型) + Worker(本地4B)` 拓扑，本应用框架的 supervisor/sub-agent（handoff）原语表达，而非自己写状态机。

---

## 2. 目标与原则

### 目标
- ReAct 单步循环、function-call 协议、流式、前端握手 **交给现成框架**，不再手搓。
- SoM、Python 执行、设备控制等**能力外置为 MCP server**，内核零写死。
- 补齐 **Python 执行**通用能力。
- 多 agent 拓扑（Manager/Worker）用框架原语表达。

### 不可动摇的原则（红线）
- **经验飞轮保留**：`WorldModel` / `Curator` / `Trajectory` / `Skill 晋升` 是核心资产，**不外包给框架**，作为框架 lifecycle 外的后处理钩子挂载。
- **模型无关不退化**：内层 4B（qwen3.5-4b-vl / 本地 llama）仍走 `openai-compatible` provider 抽象；框架只做循环驱动 + 协议，不锁模型厂商。
- **内核零场景/零字段假设**：延续 B2 成果，`agents/` 不出现具体感知字段名、不出现具体工具名分支（由 `scripts/review_lint.py` 自动卡）。
- **能力=工具，领域数据走配置/MCP**，不进代码。

### 总约束（P0 框架优先）
- **已引入 LangGraph + OpenAI Agents SDK，凡是框架原生能力一律用框架，不自研等价物。** 包括但不限于：ReAct 循环、function-call、流式、handoff、interrupt（中断/恢复）、运行时消息注入、任务状态管理、`/stop` 停止、人工纠偏注入队列等通用机制，直接走 LangGraph / Agents SDK 原语。
- **自研只保留 SDK 替代不了的两类**：L2 护城河（领域经验飞轮 + 完成判定 + 升级路由）、L1 能力（各 MCP server 的领域实现）。
- 判定标准：某能力若框架已有成熟原语 → 用框架；若需手搓 → 先确认它是否属于 L2/L1，否则违规。

---

## 3. 架构总览（目标态）

```
┌─ 框架层（ReAct 循环 + function call + 流式 + 多 agent handoff）──────────┐
│   OpenAI Agents SDK 或 LangGraph 接管单步循环                            │
│   前端打招呼 / 工具调用 / 流式 = 框架原生                                  │
├─ 你的护城河（框架外，lifecycle 钩子挂载，不动）────────────────────────┤
│   WorldModel / Curator / Trajectory / Skill 晋升                         │
│   Manager↔Worker 升级拓扑 + 阈值（config.runtime.escalation.* 驱动）      │
├─ agent 外层 tool 插件层（完全平级，LLM 直接调用，内核零持有）─────────┤
│   自研工具（function_tool 隔离派发）:                                    │
│     - 屏幕/鼠标/键盘、视觉识别、yolo detect、SoM、文件 IO … 通用自写      │
│   外部工具（MCP 接入，与自研平级）:                                       │
│     - 开源 MCP / 第三方系统 → 直接接入，纯配置增量                       │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3.5 整体框架定稿

> 本节为框架级最终定义，后续里程碑（M0–M4）均以此为准。

### 三层栈（物理边界）

```
L3 框架层    LangGraph(编排) + OpenAI Agents SDK(单 agent 运行时)
            原生能力: ReAct 循环 / function-call / 流式 / MCP 工具加载 /
                     Manager↔Worker handoff / interrupt(中断恢复) / 运行时消息注入
            不持有任何工具实现，不持有任何领域状态

L2 护城河层  WorldModel / Curator / Trajectory / Skill 晋升 / verify_done / OrchestrationPolicy
            挂载方式: 框架 lifecycle 钩子(每步后处理 / 任务结束)，与 L3 解耦
            自治权:   单步自愈(重试/换工具/回退/跳过) + 自动升级路由

L1 工具插件层  agent 外层 tool（完全平级，LLM 直接调用，内核零持有）
            自研工具: 屏幕/鼠标/键盘、视觉识别、yolo detect、SoM、文件 IO…
                     用开源 function_tool 承载（定义→注册→派发→回填全框架接管）
            外部工具: 开源 MCP / 第三方系统 → 直接接入，与自研平级
            内核只做 tool 的「能力分组配置 + 后处理钩子」，不持有任何实现/派发
```

> 工具=插件，完全平级，由 LLM 自己调用。手搓态下派发逻辑把工具「吸」进内核造成耦合；
> 现用开源 function_tool 隔离四跳，自研工具与外部 MCP 同为外层平级插件，内核不再参与派发。


### 五条边界契约（不可破）

| 契约 | 内容 | 卡点 |
|---|---|---|
| **C1 工具零写死** | 内核不出现任何工具名分支、不持有 schema | review_lint R2 |
| **C2 字段零假设** | 内核不出现屏幕/ocr/active_window 等感知字段 | review_lint R1（延续 B2） |
| **C3 模型无关** | L3 只驱动循环，model 来自 openai-compatible provider | BrainClient 降级为 provider |
| **C4 钩子自治（修订）** | L2 有单步自治权（重试/换工具/回退/跳过/自动 handoff）；约束：不重写协议层、所有干预经 Trajectory 落盘可观测、仅「超阈值仍无法自愈」才升为需人工（HITL 兜底，非常态） | OrchestrationPolicy 只产出决策 |
| **C5 HITL 双通道（框架原生）** | 通道A 硬停止：`/stop` → 框架 cancel 当前 run → 新指令重启一轮；通道B 软注入：运行中 push user 纠偏消息 → 下一步 agent 可见，优先于 L2 自治；优先级：人类注入 > L2 自治决策 > 框架默认策略；实现走 LangGraph 原生 interrupt / 运行时消息注入，**不自研队列** | 通用机制全用框架 |

### 长任务 / 少干预 诉求（驱动 C4/C5）

- 目标：几十步、跨小时的长任务，人工只在「抛锚（超阈值无法自愈）」时介入，日常自愈由 L2 兜。
- 人类干预分两级：
  1. **硬停止**（需求1）：人发 `/stop` 强行终止整个循环 → 输入新指令 → 大模型重新执行。属 L3 框架级 cancel + 重启，L2 不参与。
  2. **软注入**（需求2）：运行中插入人类纠偏（如「先别点这个，去点设置」）→ 循环不中断，下一步 agent 改路继续。属 L3 运行时消息注入，优先级高于 L2 自治。
- 两者均用 LangGraph 原生原语实现（interrupt / 运行时消息队列），不自研。

### 一句话定义

> 框架只做「循环驱动 + 工具路由 + HITL 通道」，领域脑（L2）与能力（L1）全部外挂；内核退化为「配置 + 钩子注册中心」，且一切通用机制优先用框架、不手搓。

---

## 4. 框架选型（**已定：LangGraph + OpenAI Agents SDK 协同，非二选一**）

两者不是互斥，是**分层协作**：
- **OpenAI Agents SDK** = 单 agent 运行时：跑 ReAct 单步循环 + function-call + 流式 + 从 MCP 加载工具。负责第 1/2 跳（工具注册、派发、结果回填）。
- **LangGraph** = 编排层：把 Manager/Worker 多 agent 拓扑、升级路由、状态机、human-in-the-loop 表达成图。负责"两层编排"语义。

底层模型：内层 4B / 在线大模型 / VL 均已是 `openai-compatible` 端点，作为 SDK 的 `Model` provider 接入，**守住模型无关红线**（不锁 OpenAI）。

```
LangGraph(编排: Manager↔Worker handoff / 升级路由)
   └─ 每个节点 = OpenAI Agents SDK Agent(Runner.run: ReAct+FC+流式)
        └─ tools = agent 外层 tool 插件(平级): 自研 function_tool + 外部 MCP
```

> 注：原稿误写为"(A)/(B) 二选一"，已纠正。框架是一套协同栈，不是单选。

---

## 5. 各模块迁移策略（按已确认归属）

> 四大块归属（用户确认）：
> 1. **Brain 模块（大模型运行时）** → SDK 单 agent 运行时（ReAct/FC/流式）；BrainClient 降级为 model provider。
> 2. **Tool Runtime**（ToolRegistry+Providers）→ SDK 原生 dispatch，删除手搓派发；工具整体迁到 agent 外层 tool 插件层（平级）。
> 3. **VisionRuntime** → VL=model provider；yolo/vision_describe/SoM 作为**自研外层 tool**（function_tool 承载，SoM marks 状态走 α：tool 内自持 `som://last_result`）。
> 4. **Execution Backend**（host/emulator）→ 作为**自研外层 tool**（屏幕/鼠标/键盘等），内核删实现类。
>
> 工具=插件：自研工具与开源/第三方 MCP 同为 agent 外层平级插件，由 LLM 直接调用；MCP 专用于外部工具接入。

### 5.0 工具链路规格（**先弄清楚再动手**）

工具从定义到执行共 4 跳，每跳归属必须明确，避免重新写死进内核：

| 跳 | 职责 | 现状（手搓） | 目标态 |
|---|---|---|---|
| **[1] 定义** | 产出 tool schema | `backend.tool_schemas`（OpenAI function schema 手搓） | 自研 tool 用 SDK `function_tool` 装饰器自动产出；外部 MCP server 自带 schema |
| **[2] 注册** | 把工具交给 agent | `tool_loop` 拼 `tools=...+schemas` | 框架从 agent 外层 tool 插件层加载（function_tool + MCP），内核不持有 |
| **[3] 派发** | agent 调 name → 执行 | `ToolRegistry.dispatch(name, args)` + `providers/*` 的 `if name==` 分支 | 框架原生 dispatch 到外层 tool（自研 function_tool / 外部 MCP），内核零分支 |
| **[4] 回填** | 结果回 agent + 落盘 | `tool_loop` 手搓 assistant/tool 消息回填 | 框架原生回填；落盘由钩子做（护城河） |

**内核在工具链路里只保留两件，其余全交出去：**
- **(a) 能力分组配置**：哪些 tool（自研/外部 MCP）在什么模式下启用（config 驱动，不写死）。
- **(b) 后处理钩子**：每步结果 → `Trajectory.log`；任务结束 → `Curator` + `Skill` 提炼。

**错误传播**：tool 执行失败 → 框架捕获 → 作为 tool result 回 agent（agent 自主重试）；致命错误（外部 MCP server 挂） → 框架事件 → `OrchestrationPolicy` 升级/中止。内核不在链路中拦截具体错误。

**schema 格式转换**：现有 `tool_schemas` 是 OpenAI function schema；外部 MCP 用 JSON Schema。框架（Agents SDK / LangGraph-MCP-adapter）原生处理 MCP→FC 转换，**内核不再做转换**。自研 tool 由 `function_tool` 直接定义，无需转换。

### 5.1 Brain 模块（交 SDK，BrainClient 降级为 model provider）
- `brain/client.py` 的 OpenAI 兼容 HTTP 调用**保留**，作为 SDK 的 `Model` provider（在线大模型 + 本地 4B 共用）。
- `tool_loop._run_inner` 的手搓 ReAct/FC/流式**删除**，由 SDK `Runner.run` 接管。
- **保留**：编排语义（何时升级、阈值、history 裁剪）在 `OrchestrationPolicy`（LangGraph 节点逻辑）表达。

### 5.2 Tool Runtime（交 SDK 原生 dispatch）
- `ToolRegistry` + `providers/*` 的 `if name==` 分支**删除**。
- 设备类工具随 Execution Backend 进 `device-mcp`；vision/m3 类进 `vision-mcp`；python 进 `python-exec-mcp`。
- 内核零工具名分支（review_lint R2 在内核层自然清零）。

### 5.3 护城河（完全保留，挂钩子）
- `WorldModel` / `Curator` / `Trajectory` / `Skill`：**原文件不动**。
- 挂载点（在 [4] 回填之后）：框架一步完成 → 回调 `trajectory.log(state, action, result)`；任务结束 → `Curator.run_once()`；成功轨迹 → `SkillLibrary` 提炼晋升。
- 钩子在 `OrchestrationPolicy` 注册，与框架循环解耦。

### 5.4 VisionRuntime（拆分处理，yolo 修正）
> 修正：yoyo = **yolo（目标检测/简单 OCR）**，是视觉**工具**不是推理模型。
- **VL 作为推理模型**（本地 VLM 看图）：归 Brain 的 model provider，和 4B 平级，交给框架当 `model`。
- **vision_describe / yolo / SoM** 等：**自研外层 tool**（function_tool 承载），与开源 MCP 平级，由 LLM 直接调用。
  - **yolo / 简单 OCR**（`detect(image)` → bbox 列表）、**vision_describe**（VLM 理解截图→文本）作为外层 tool 直接暴露（工具=插件，平级可调）。
  - **SoM**（som_ground/som_marks/tap_by_mark）：高层外层 tool，**内部调用 yolo 检测结果再编号**产 marks。
  - **α 决策（已定）**：marks 跨步状态由 tool 内自持 `som://last_result`（resource/state），agent 经其读，内核零感知。
- 内核删除 `vision.py`/`m3.py`/`providers/vision.py` 全部 SoM/yolo 实现（迁到外层 tool 插件）。

### 5.5 Python 执行（新增能力，补纯文本缺口）
- 新增自研外层 tool `run_python(code)`（沙箱、超时、stdout 捕获），与 MCP 平级，无需手搓派发。
- 直接补齐 agent 跑脚本能力。

### 5.6 设备控制（Host/Emulator → 自研外层 tool）
- `execution_host.py` / `execution_emulator.py` 实现**平移**为自研外层 tool（屏幕/鼠标/键盘，如 type/click/observe/launch_app...），与开源 MCP 平级。
- B2 做的 `text_of`/`verify_done` 抽象 → 外层 tool 内 `get_state_text` + `verify` 语义（去字段假设成果被继承）。
- 内核删除 `ExecutionBackend` 实现类，只保留"tool 列表 + 能力分组配置"。

---

## 6. 多 agent 拓扑（Manager/Worker）

- **Manager（在线大模型）**：规划 + 反思 + 升级决策。框架表达为 `Agent(name="manager", handoffs=[worker])`。
- **Worker（本地 4B）**：内层 ReAct。框架表达为 `Agent(name="worker", tools=[自研外层 tool + 外部 MCP])`。
- 升级触发：Worker 连续 verify 失败 / 墙钟超时 / `no_confidence` → 经 `OrchestrationPolicy` 调 `manager.handoff`，由 Manager 反思重规划。
- 阈值全部来自 `config.runtime.escalation.*`（沿用现有 `_DEFAULT_ESCALATION` 结构）。

---

## 7. 迁移里程碑（M0→M4 顺序推进，每步可独立验证）

> 迁移顺序：M0 作为第一步，五步顺序推进，不做"跳过 M0"取舍。

1. **M0 能力外置 POC**：把 SoM/yolo（自研外层 tool）+ Python 执行（自研外层 tool）从内核剥离，内核经框架 `function_tool` 调通，验证「工具=插件、平级外置」范式（不动框架）。→ 消痛点 #2/#3。
2. **M1 框架接管单步 ReAct（POC）**：选 (A)/(B) 后，新建适配层用框架跑通「前端打招呼 + function call + 流式 + 一个外层 tool」，护城河钩子挂上。→ 消痛点 #1。
3. **M2 多 agent 拓扑**：LangGraph 表达 Manager/Worker 图 + handoff，升级策略接 `OrchestrationPolicy`。
4. **M3 设备能力 tool 化** ✅ **已完成（2026-09-12）**：新增 `omni_core/tools/device_tool.py`（键鼠/感知/设备原语 + template_match/wait_for/drag，group=`device`）、`python_tool.py`（`run_python`，group=`python`）、`mcp_bridge.py`（外部 MCP 接入，group=`mcp`）；删除手搓派发（`brain/providers/*` 整个包 + `build_registry` / `dispatch_tool`），`tool_loop` 改为 `build_plugin_registry()` + `call_tool` 按名派发，并注入 `bind_execution_module`；外部 MCP 纯配置（`runtime.mcp.servers`），与自研工具同表同路径平级。
   - **与原文的偏差（待 M4 收口）**：`execution_host.py` / `execution_emulator.py` 两个后端驱动**未物理搬出 `omni_core/`**——它们现在是「只被插件层调用的 L1 设备驱动」，内核已不持有、不派发；物理迁出与 README/HTML 同步留到 M4，避免一次性打断 `backend/` 服务与真机脚本。
5. **M4 清理与 lint 固化** ✅ **已完成（2026-09-12）**：
   - **设备实现物理迁出内核**：新增顶层 `devices/` 包（`base.py`/`host.py`/`emulator.py`/`factory.py`/`observer.py`），删除 `omni_core/execution*.py` 四个文件；后端不再声明 `tool_schemas`（能力暴露唯一来源 = tool 插件层），切断「设备 → 内核」反向依赖。
   - **lint 固化**：`review_lint.py` 改为**自动发现** `omni_core/**/*.py`（L1 插件层 `omni_core/tools/**`、`local/vision.py` 与 `devices/` 豁免），R3 改指向 `devices/base.py`；新增内核文件默认受约束，无需手工登记。
   - **接 CI**：`.github/workflows/redline-lint.yml`（纯标准库、秒级；pytest 因 torch/easyocr/pyautogui 原生依赖过重未纳入）。
   - **删除被 SDK 覆盖的协议代码（已完整落地）**：`omni_core/brain/client.py` **整文件删除**，手搓协议层（httpx 请求、payload 组装、`tool_calls` JSON 解析、HTTP 错误处理）改由 Agents SDK 的 `Model` 实现承载；新增 `omni_core/brain/llm.py`，保持 `LLMClient.chat(...) -> BrainReply` 契约不变，只做两件框架集成必需的事——OpenAI chat 消息 <-> SDK Responses input items 的类型映射、同步<->异步线程桥。同时删除从未被消费的 `BrainReply.raw`。
   - **顺带修复 M3 引入的下发回归**：`task_done` / `record` 是内核元工具，M3 把设备 schema 迁到插件层后失去了下发通道（模型不知道可以收尾/记录），已在 `_run_inner` 组装工具清单时显式补回。
   - **`_run_inner` 标记废弃**：源码 `DEPRECATED(M4)` 标记 + 进程级一次性 `DeprecationWarning`，指向 `sdk_agent.run_task_sdk` + `orchestration.graph`；`_assistant_msg`（手搓 FC 序列化）同步标注 LEGACY。

---

## 8. 风险与回滚

- **模型 provider 适配风险**：需验证 SDK 的 `Model` 抽象能包住本地 4B（openai-compatible）。不可行则退 LangGraph 原生 model（已协同，影响小）。
- **护城河耦合风险**：确保 WorldModel/Curator 等**不被改写**，仅加挂载钩子；回滚只需撤销钩子注册。
- **外部 MCP 性能**：外部 MCP server 走进程间/网络调用有延迟；自研外层 tool 同进程无此问题。POC 阶段压测外部 MCP，必要时选同进程内嵌式 MCP。
- **分支策略**：本分支独立，主干 `master` 不受影响；每里程碑单独 commit，便于逐段 review。

---

## 9. 验收标准

- [x] `review_lint.py --strict` 在 `omni_core/` 零红线（延续 B2；M3 新增 `PLUGIN_FILES` 豁免 L1 能力层）。
- [x] `omni_core/` 不含任何设备实现、不含 `ExecutionBackend` 实现类（M4 已物理迁出到顶层 `devices/`；`local/observer.py` 一并迁出）。SoM/VLM 实现也已按 §5.4 迁到 `omni_core/tools/vision_runtime.py`（L1），内核层无豁免残留。
- [x] 手搓 OpenAI 协议层已删除（`brain/client.py` 整文件删除，改走 Agents SDK `Model`；`tests/test_m4d_sdk_transport.py` 用本地 OpenAI 兼容 server 端到端验证主链路）。
- [ ] 前端「打招呼 + 工具调用 + 流式」由框架原生驱动，无手搓 function-call 序列化、无 `ToolRegistry.dispatch`。（M1 POC 已通；手搓 `ToolRegistry` 已于 M3 删除；`_assistant_msg` 手搓 FC 序列化仍随已废弃的 `_run_inner` 存在，待框架主路径切换后删除）
- [x] 自研工具（视觉/设备/Python）与外部 MCP 在 agent 外层 tool 插件层平级，由 LLM 直接调用；内核零派发分支。
- [x] 新增 Python 执行能力，agent 可跑脚本（`run_python`，子进程沙箱 + 超时）。
- [ ] Manager/Worker 多 agent 拓扑可运行（LangGraph），升级策略由 config 驱动。（M2 已建图，端到端待 M4 收口）
- [x] SoM marks 状态自持于 tool 内（`som://last_result`），内核零感知：`omni_core/tools/vision_tool.py` 持有 `_last_marks`，`VisionRuntime` 不再持有状态（改为 `tap_mark(mark)` 接收具体 mark）；新增 `som_last_result` 工具供 agent 重读。
- [x] 外部 MCP（开源/第三方）可直接接入，纯配置增量，与自研工具平级（`runtime.mcp.servers`，端到端 stdio 已验证）。
- [x] WorldModel/Curator/Trajectory/Skill 行为不变（现有相关测试全过；M5 后 289 passed）。
- [x] `review_lint.py` 接 CI（`.github/workflows/redline-lint.yml`，`--strict` 非零即红）。
- [x] 旧 `tool_loop._run_inner` 标记废弃（`DEPRECATED(M4)` + 运行时 `DeprecationWarning`）。

**M5（工具与 MCP 全量交给 SDK）** ✅ 已完成：
- [x] 自研 `function_tool` 换成 SDK `agents.tool.function_tool`（schema 由签名+docstring 产出）。
- [x] 自研 `mcp_bridge.py` 删除，外部 MCP 改用 SDK `MCPServerStdio` / `MCPServerStreamableHttp`，
      由 `Agent(mcp_servers=[...])` + Runner 负责连接/发现/派发（端到端已验证）。
- [x] 主链路（`run_task` / `_run_subtask`）改由 `Agent + Runner.run` 驱动（新增 `omni_core/brain/sdk_loop.py`），
      `_run_inner` 仅留作废弃对照，主路径不再调用。
- [x] L2 通过框架原生机制挂载：`StopAtTools`（task_done/verify/escalate 收尾）、`RunHooks`（世界模型/轨迹后处理）、
      分块 `max_turns`（预算与升级检查点）；完成判定仍走 `backend.verify_done`（去场景化 §9 不变）。
- [x] 工具异常按 §5.0「错误传播」回给 agent 自主重试，不再中断循环。

**M5 遗留（需决策）**：长任务历史压缩现在只在 L2 检查点之间生效（历史主由框架
`to_input_list()` 托管）；若要恢复「每 N 步必压缩」，需要把分块跑通为 streamed 模式
（当前 `Runner.run_streamed` 的 `stream_events()` 在本版本返回 coroutine，未接通）。

---

## 10. 决策记录与剩余待定

### 决策

1. **M0 是否先做 → 做，且 M0→M4 顺序推进，不做"跳过 M0"取舍**。
   - 五里程碑按 §7 顺序走：M0 能力外置 POC → M1 框架接管 ReAct → M2 多 agent → M3 设备能力 tool 化 + 外部 MCP 接入 → M4 清理 lint。
2. **device/视觉等基础能力 → 自研外层 tool（function_tool 承载），非内部 MCP server**。
   - 理由：屏幕/鼠标/识别是 agent 的"手眼"，与文件 IO 同类，属基础执行环境；工具=插件、完全平级，由 LLM 直接调用。开源 `function_tool` 隔离四跳，内核不再被派发逻辑吸耦合。MCP 专用于**外部工具接入**（开源/第三方）。
3. **yolo / 视觉工具 → 作为自研外层 tool 直接暴露（与 SoM 平级），工具=插件原则下默认可调**。
   - 理由：既已定"工具=插件、LLM 自己调"，yolo `detect` 即外层 tool 之一，无需内核级硬性隐藏；SoM 仍是高层 tool，内部调 yolo。是否限制暴露交给插件层配置，不进内核红线。

### 剩余待定（仅实现细节，不影响架构）

- 自研外层 tool 的**目录组织**（如 `tools/` 插件包）与「外部 MCP 接入」的 config schema，留到 M0/M3 实现时定（当前架构已定，不阻塞）。

> 已定项：
> - 框架 = LangGraph + Agents SDK 协同（§4）
> - 工具=插件：自研外层 tool 与开源/第三方 MCP **完全平级**，由 LLM 直接调用；内核零持有、零派发
> - 四大块归属（§5 引言）
> - SoM marks 状态走 α（tool 内自持 `som://last_result`）
> - **整体框架三层栈 + C1–C5 契约 + 长任务少干预 + HITL 双通道（§3.5）**
> - **P0 框架优先总约束：通用机制全用框架原语，不自研（§2）**

---

## 11. 产品设计 HTML 重做蓝图（当前态，不保留历史）

> 决策：旧 `doc/agent-control-arch-2026-07-26.html` 保持为「手搓态档案」**暂不改**；
> 待 M0–M4 落地后重做 HTML。本节约等于重做时的**图集定稿**，遵循「只展现当前态、不保留历史」原则。
> 核心改动只有一条：**工具脱离内核，成为 agent 外层平级插件（自研 tool 与开源 MCP 同级），由 LLM 直接调用**；护城河（WorldModel/Curator/Trajectory/Skill/verify_done）与全部能力（SoM/Python/device）原样保留，仅搬家到外层 tool 插件层（MCP 专用于外部接入）。

### 11.1 新图集（N1–N10，共 10 张，替代旧 S1–S13）

| 新图 | 主题 | 旧图对应 | 处理 |
|---|---|---|---|
| **N1** | 总体分层（L3 框架 / L2 护城河 / L1 能力+MCP） | S1 | **重写**：三层栈替代 Brain→Provider→Backend 纵向链 |
| **N2** | 内核 + MCP 工具调用解耦 | S2 | **重写**：`registry.dispatch` → 框架从 MCP server 加载 tool，内核零持有 |
| **N3** | 单次任务 ReAct 闭环（框架原生 + Verify 钩子） | S3 | **改写**：循环 owner = SDK；Verify = 后处理钩子挂 L2 |
| **N4** | Vision SoM 通道（MCP 化） | S5 | 保留（对齐 §5.4：本地 VLM=工具，SoM marks 走 `som://last_result`） |
| **N5** | 自升级 Meta-loop | S6 | 保留（护城河核心） |
| **N6** | 两层编排 + Escalation（LangGraph handoff） | S9 | **改写**：handoff 表达为框架原语；阈值仍 config 驱动 |
| **N7** | 上下文分层管理（大模型上下文压缩 vs worker 硬滑窗） | S10 | 保留（owner 变框架 lifecycle） |
| **N8** | 超长任务持久化 + checkpoint | S11 | 保留（护城河支点，长任务少干预的落地） |
| **N9** | 全局目录与 Project / Task 模型 | S12 | 保留（资产落盘，与框架无关） |
| **N10** | 前端设计（SSE / stop / 插话 = HITL 双通道） | S13 | 保留（对接 §3.5 C5；`/stop`+插话已存在，迁框架 interrupt 接管） |

### 11.2 精简掉的历史图（不再出现）

- 旧 **S4** 经典 Loop vs X3 扩展对比 → 纯历史叙事，删。
- 旧 **S7** Agent 状态机枚举 → 状态机改由框架驱动，不再单独成图（前端 SSE 事件改在 N10 提及）。
- 旧 **S8** M4 路线图 → 历史档案，删。

### 11.3 重做时的叙述原则

- Part I 设计理念（P0–P4）中涉及「手搓 ReAct / Provider 派发 / ToolRegistry」的措辞改为「框架接管 / MCP 工具 / 钩子挂载」。
- 红线与北极星不变（内核零场景硬编码、能力==工具）。
- 不追加演进史段落；直接就地呈现框架态。

---

## 12. 旧 HTML 对照（**已执行**：`doc/agent-control-arch-2026-09-12.html` 已按 N1–N10 更新为落地态）

| 旧图 | 新图 | 动作 | 备注 |
|---|---|---|---|
| S1 总体分层 | N1 | 重画 | Provider 层→MCP server 层 |
| S2 内核+工具解耦 | N2 | 重画 | dispatch→框架原生 |
| S3 ReAct 闭环 | N3 | 改写 | 循环 owner 改 SDK |
| S4 经典 Loop 对比 | — | 删除 | 历史 |
| S5 Vision SoM | N4 | 保留 | 对齐 §5.4 |
| S6 Meta-loop | N5 | 保留 | 护城河 |
| S7 状态机 | — | 删除 | 框架驱动 |
| S8 M4 路线图 | — | 删除 | 历史 |
| S9 两层编排 | N6 | 改写 | handoff 框架化 |
| S10 上下文分层 | N7 | 保留 | |
| S11 持久化 | N8 | 保留 | 护城河支点 |
| S12 目录模型 | N9 | 保留 | |
| S13 前端 | N10 | 保留 | 对接 C5 |

---

## 13. 后继设计（架构修订，另起文档）

M0–M5 落地后，架构层有修订：**取消代码内分层、改为框架原生的通用多 agent（默认单 agent，主 agent 决定派发）、本地模型工具化、三态 `runtime.mode` 废弃、软注入保真**。
设计见：`doc/plans/multi-agent-redesign-2026-09-13.md`（本文 §2/§3.5/§4/§6 以后者为准）。
