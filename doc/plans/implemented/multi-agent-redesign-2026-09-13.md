> **设计来源文档**（多 agent 去分层 / 框架原生 / 本地模型工具化）。当前权威来源：
> - 架构与产品：`doc/agent-control-arch-design.html`（架构设计）、`doc/agent-control-product-design.html`（产品设计）
> - 规格：`doc/agent-control-master-spec.md`

# OmniAgent 多 agent 架构重设计（去分层 / 框架原生 / 本地模型工具化）

- 分支：`refactor/agent-core-framework`
- 日期：2026-09-13
- 状态：**设计稿（待实现）**
- 上游：`refactor-agent-core-framework-design-2026-09-12.md`（M0–M5 已落地，本文修订其 §2/§3.5/§4/§6）

---

## 0. 决策摘要

| 编号 | 决策 | 内容 |
|---|---|---|
| D1 | 去分层 | 代码里不再有「两层 / Manager / Worker」的固定分层。是否拆子 agent **由主 agent 决定**（业务决定），不是代码写死的拓扑。 |
| D2 | 默认单 agent | 默认单 agent 跑完整任务；只有复杂任务（技能决策、长耗时并行读取/远程调用）才由主 agent 分发给并行子 agent。 |
| D3 | 并行方式 | LangGraph `Send` 扇出（方案 a）。 |
| D4 | 本地模型 | 本地模型以 **tool** 形态注入，带明确边界声明（含"不适合"场景），由主 LLM 决定是否派发推理/视觉。 |
| D5 | 三态废弃 | `runtime.mode: dual / online_only / local_only` **取消**。这里只是多模型接入：主模型可配在线（deepseek/glm…）也可配本地；本地模型另立一份配置专供 tool 使用，带 `vision` 开关。 |
| D6 | 软注入保真 | 必须保留"不打断当前 turn 的注入"语义（超长任务刚需），走 LangGraph `update_state` + checkpointer，不自研队列。 |
| D7 | **A2** | 长任务节奏控制本版只做两项：**T2 预算感知提示**（新增）+ **T1 压缩产物归黑板**（改造）。T3 并行检查点、T1 的 token 级阈值留到后续里程碑。 |
| D8 | **ii** | 共享黑板 = **WorldModel**（agent 间共享认知）；子任务"领取/状态机"= **TaskStore**（已有锁 + 原子写 + 白名单 update）。 |

---

## 1. 设计原则（7 条，约束后面所有取舍）

1. **P0 框架优先**：ReAct 循环、function-call、流式、并行、中断/恢复、运行时注入一律用框架原语，不自研等价物。
2. **拓扑不写死**：代码里没有"两层"这个名词，只有「通用 agent 执行体 + 由主 agent 产出的派发计划」。
3. **内核零领域**：内核不出现任何 app/游戏/业务词表、不出现具体工具名分支、不出现感知字段名（红线 memory 85117719）。
4. **能力=工具，边界=配置**：本地模型的能力与"不适合"清单是配置数据，不是代码常量；内核只提供通用渲染模板。
5. **共享认知外置**：agent 之间唯一的公共状态是共享黑板（WorldModel），不做 agent 间直连调用/消息总线。
6. **人类 > 自治 > 框架默认**：软注入优先于 L2 自治；硬停止立即生效。
7. **可观测**：一切干预（压缩、预算提示、派发、升级、注入）都落 Trajectory，可回放。

---

## 2. 目标架构

### 2.1 执行体：通用 agent 注册表（配置驱动）

不再定义 `Manager` / `Worker` 类。只有一个通用执行体：

```
run_agent(agent_id, objective, done_when, tools, budget) -> AgentResult
```

`agent_id` 指向 `runtime.agents.<id>` 配置（model 引用、启用的 tool 分组、步数预算、是否可被派发）。
图上节点名不叫 manager/worker，叫 `agent:<id>`——**分层消失，只剩"哪个 agent 在跑"**。

### 2.2 图：两类节点 + Send 扇出

```
            ┌──────────────┐
   入口 ───▶ │ agent:main   │  主 agent：默认直接做完；需要时产出 DispatchPlan
            └──────┬───────┘
                   │ 条件边（读 DispatchPlan）
        ┌──────────┴──────────┐
        │ 无派发 → END        │ 有派发 → Send("agent:sub", item) × N（并行）
        │                     ▼
        │              ┌─────────────┐
        │              │ agent:sub   │ ... N 个并发（写共享黑板）
        │              └──────┬──────┘
        │                     ▼  join（归约：结果 + 黑板摘要回灌主 agent）
        └──────────────▶ agent:main（下一轮，max_rounds 上限）
```

要点：
- **主 agent 决定**：派发意图来自主 agent 的结构化产出（`output_type=DispatchPlan`），不是编排层的 `if` 分支。
- **扇出由框架**：条件边把 `DispatchPlan.items` 映射为 `Send(...)`，LangGraph 负责并发调度与 join。
- **归约只有一件事**：把每个子 agent 的结果 + 关键事实写进共享黑板；主 agent 下一轮从黑板读，不读子 agent 的私有历史。
- **`max_rounds` 兜底**：达到上限还没完成 → 交回上层（失败/升级），防无限派发。

### 2.3 实现硬约束（并行不是免费的）

1. **必须异步调用**：`Send` 扇出的节点只有在 `graph.ainvoke()` + `async def` 节点下才真并发；沿用同步 `invoke` 会退化成串行（当前 `_worker` 是同步函数，必须改）。
2. **共享 event loop**：子 agent 内 `Runner.run` 走 `omni_core/async_bridge.run_async`，节点本身也要 `async def`，不要在异步节点里再套 `run_async`（会死锁）。
3. **并发度**：`runtime.dispatch.max_parallel` 限制单轮派发条数。

> **M7 落地说明（与设计稿的两处偏差）**
> 1. **派发意图的表达方式**：设计稿写的是「主 agent 结构化产出 `output_type=DispatchPlan`」，
>    落地改用 **`dispatch` 元工具**（与 task_done/verify/escalate 同级，只给主 agent）。
>    理由：主 agent 是执行者，派发发生在循环中途；用元工具 + `StopAtTools` 可"一调即停"
>    把控制权交回编排层，不必等本轮跑完，也不需要引入 output_type 与 pydantic 模型。
>    语义等价（都满足「由主 agent 决定、框架负责扇出」）。
> 2. **子 agent 仍持有独立 `WorldModel`**：见 §3 说明，节点结束时带来源 flush 到共享黑板。
>
> 顺带：M7 删除了 `orchestration/policy.py`（升级路由不再是编排层的职责，
> 升级由子 agent 自报 + 主 agent 决断），`tests/test_m2_orchestration.py` 重写为新图测试。

---

## 3. 共享黑板 = WorldModel（D8-ii，改造清单）

复用 `WorldModel` 是对的（`facts/notes/collected` 语义就是公开世界事实），但它现在是 **per-run 私有对象**，必须做 5 项改造才能当共享板：

| # | 现状 | 改造 |
|---|---|---|
| 1 | 生命周期：`run_task` / `run_task_two_layer` / `_run_subtask` 各自 `new WorldModel()`（`tool_loop.py:906/975/1119`），父子靠 `_run_subtask` 里 `parent_world.update() + merge_collection()` 单向回传（`:1134-1137`） | 改为按 `task_id` 的**共享注册表**：同 task 内所有 agent 取同一实例；删除单向 merge |
| 2 | 并发：`facts` 是裸 `list`，无锁（`world_model.py:38`） | 加 `RLock`（抄 `task_store.py:37` 现成模式） |
| 3 | 溯源：扁平 `List[str]`，分不清谁写的 | `add_fact(text, source=agent_id)`，内部 `{text, source, ts}`；冲突 last-write-wins 但保留来源 |
| 4 | 视图：`summary()`（`world_model.py:77`）把全部 facts/notes/actions 拼一段 | 新增 `view(scope, k)`：全局事实 + 本分支事实 + 最近 k 步动作。**每个 agent 只看自己的视图**，否则上下文爆炸且全是噪声 |
| 5 | 落盘：`save()` 整文件覆盖 `world_model.md` | 原子写（`.tmp` + `os.replace`，`task_store.py:150` 已有）+ 按 agent 分片合并 |

**写入时机**（唯一收口点）：每个 agent 节点结束时统一 flush，不在 agent run 中途写——中途写会与并行兄弟交错且无法归因。

> **M6 落地说明**：M7 并行化之前，子 agent 仍持有独立 `WorldModel` 实例（避免 percept / 动作 ring buffer 在并发下互串），在节点结束时 `merge_collection(sub, source=...)` flush 到共享黑板并带来源——与上面「写入唯一收口点」一致。M7 改为直接使用共享实例 + `view(scope)`。

---

## 4. 任务状态机 = TaskStore（D8-ii）

`TaskStore`（`task_store.py:34`）已有 `RLock` + 原子写 + 白名单 `update()`，承载子任务"领取/状态"：

- 新增条目结构：`subtask {id, parent_task_id, desc, done_when, state, owner_agent_id, attempts, result_ref}`
- 状态：`pending → running → done | failed | skipped`
- 领取语义：`claim(subtask_id, agent_id)` 原子 CAS（`pending` 才允许改 `running`），防止两个子 agent 抢同一个；`claim_next()` 原子取下一条可领的
- **M6 落地**：实现为 `SubtaskStore`（`task_store.py`），落 `tasks/<task_id>/subtasks.json`，自带 `RLock` + 原子写，与 `TaskStore` 同模块同风格
- **WorldModel 只管世界认知**（看到了什么、采到什么、已知事实），TaskStore 只管"谁在做什么、做到哪"——两者同属一个 `task_id`，对上层就是一块共享板。

---

## 5. 长任务节奏控制（D7 = A2 范围）

### T2 预算感知提示（新增，本版必做）

- **问题**：现在模型完全不知道自己还剩多少步。`run_subtask_sdk` 只在 `state.steps < max_steps` 外循环，跑到 `MaxTurnsExceeded` 直接判 `budget_exhausted` 失败（`sdk_loop.py:201-205`），或外循环自然结束（`:245-247`）。全程没有一处告诉模型"你已用 18/20 步"。超长任务第一失败模式不是做错，是**做了一半被掐断**，且模型没机会 `escalate` 交回已完成部分。
- **做法**：在 **Model 适配层**注入（`_BudgetHintModel`）：步数越过阈值时，把提示追加进下一次 LLM 输入。
  > 为什么不用「块与块之间注入」：本版 SDK 的 `Runner.run` 在 `max_turns` 用尽时直接抛
  > `MaxTurnsExceeded` 且拿不回历史（`stream_events` 未接通，见 M5 遗留）；为了插提示而
  > 人为切块会把已跑的若干轮历史丢掉。走 Model 层则不切块、不打断、历史零丢失。
- **措辞红线**：只陈述事实 + 给出可选动作，不做价值判断。模板形如"已用 X/Y 步；若判断无法在剩余预算内完成，可调 `escalate` 交回已完成部分"。**禁止**"请尽快完成/请加速"——会诱导模型跳过 verify、降质收尾。把选择权留给模型。
- **配置**：`runtime.long_task.budget_hint_ratio`（默认 0.75），每条 agent 只提示一次。

### T1 压缩产物归黑板（改造，本版必做）

- **问题**：`_maybe_compress()`（`sdk_loop.py:250`）的产物是一条 `[历史压缩摘要]` user 消息，**只活在当前 run 的 items 里**，子 agent 结束即消失；并行子 agent 各压各的，兄弟分支互相看不见对方已压缩掉的上下文。
- **做法**：压缩时除了替换 items，额外把摘要 `add_fact(摘要, source=agent_id)` 写进共享黑板。这样：
  - 兄弟分支通过 `view()` 能读到其他分支压缩掉的上下文；
  - 主 agent 下一轮天然拿到浓缩后的全局进展，不依赖子 agent 的私有历史。
- **不在本版做**：token 级压缩阈值（现在按 function_call 轮数，与真实 token 无关）、每 N 步强制压缩（需先把分块跑通为 streamed）。

### 明确不做（本版）

- **T3 并行检查点**：等新骨架落地后再定版本号/合并策略，现在写必然返工。
- **T4 重规划归属**：这条不是"不做"，是**必改**——见 §2.2，重规划从 `orchestration/graph.py` 的 `_route`/`_reflect` 移到"主 agent 自己决定"。

---

## 6. 本地模型 = 工具（D4/D5）

### 6.1 取消三态 mode

`runtime.mode` 只是"多模型接入"的一种笨表达，删除：
- 主模型要本地 → `llm.providers` 里把主模型指向本地端点即可，无需 `local_only`。
- 不用本地 → 不启用本地 tool 即可，无需 `online_only`。
- 连带清理：`tool_loop.py:163-205/248-260`（mode 解析与 `executor_is_planner` 兼任逻辑）、`backend/api/router_settings.py:97-101` 校验、前端 `web/src/pages/Settings/index.tsx` 三态下拉。

### 6.2 本地模型以 tool 暴露

- 注册在 L1 插件层（lint 豁免区），分组 `local_model`，由 `runtime.tools.groups` 控制是否暴露，与 vision/device/python/mcp **完全平级**。
- 工具面向主 LLM 的描述 = **通用模板 + 配置声明的边界**，代码里零领域词：

```yaml
llm:
  providers:
    default:            # 主模型（在线/本地均可）
      provider: openai-compatible
      base_url: https://...
      model: demo-model
      api_key_env: OMNI_BRAIN_API_KEY
      capabilities: {vision: false}
    local-4b:
      provider: openai-compatible
      base_url: http://127.0.0.1:8085
      model: qwen3.5-4b-vl
      capabilities: {vision: true}      # 决定 local_vision 是否注册
  local_as_tool:
    enabled: true
    provider: local-4b
    tools: [infer, vision]              # 按 capabilities 过滤
    boundary:
      good_for:  ["短指令解析", "看图定位", "低成本重复推理"]
      not_for:   ["长链规划", "大上下文汇总", "需要最新知识的判断"]
    budget: {max_calls_per_task: 200, timeout_sec: 30}
```

- 主 LLM 自行决定是否派发：成本敏感/短平快 → `local_infer`；复杂规划 → 自己来。
- **M9 落地偏差（视觉部分）**：设计稿写 `tools: [infer, vision]`，落地只加了 `local_infer`。
  原因：既有的 `vision_describe`（group=vision）本来就是把当前截图送**同一个本地 VLM**，
  再注册一个 `local_vision` 会是同义工具，反而让模型困惑。改为：`capabilities.vision`
  为真时在 `local_infer` 描述里声明「本地模型可看图，看图请用 vision_describe」。
- 配置回退：未显式配置 `llm.local_as_tool` 时，沿用 `runtime.executor` 的端点与 enabled。
- `not_for` 这类负向边界同样来自配置，不进代码（红线 3/4）。

---

## 7. HITL：软注入保真 + 硬停止

| 通道 | 语义 | 实现 |
|---|---|---|
| **硬停止** | `/stop` 立即终止 | SDK `cancel()` / LangGraph `interrupt()`，当前 turn 打断，新指令重启一轮 |
| **软注入** | 运行中插入人类纠偏，**循环不中断**，下一步可见 | LangGraph `update_state()` 带 checkpointer 写入，**不打断当前 turn** |

> **M8 落地说明（一处实测偏差）**：只写 `update_state` 不够——本版 Pregel 在一次
> `ainvoke` 期间沿用自己的内存态，外部写入的新 checkpoint 不会被这次正在跑的调用读到
> （已实测：主 agent 看不到）。因此 `ToolLoop.inject_message` 双写：
> ① `update_state` 进 checkpoint（持久 + 可观测 + 恢复通道）；② 运行期投递队列，
> 主 agent 每轮开始时取走。**待定**：若后续升级 LangGraph 后 ① 能直接生效，可删掉 ②。

保真要求（超长任务刚需）：
1. 软注入**必须**等当前 LLM turn + 当前工具调用走完再生效，不能中途截断导致工具结果丢失；
2. 注入内容优先级最高（人类 > L2 自治 > 框架默认）；
3. 注入后不重置历史、不重置预算；
4. 因此**必须给 graph 挂 checkpointer**（当前 `graph.py:248` 是 `g.compile()` 无 checkpointer，软注入与崩溃恢复都没有载体）。Checkpointer 选 `SqliteSaver`/`MemorySaver`，`thread_id = task_id`。

---

## 8. 配置 schema 变更（草案）

```yaml
runtime:
  agents:
    main:
      enabled: true
      model: default              # 引用 llm.providers
      tools: [vision, device, python, local_model, mcp]
      max_steps: 40
      dispatchable: [worker]      # 可派发的子 agent（空 = 强制单 agent）
    worker:
      enabled: false
      model: local-4b
      tools: [vision, device, python]
      max_steps: 20
  dispatch:
    enabled: true
    max_parallel: 4
    max_rounds: 3
  long_task:
    budget_hint_ratio: 0.75
    compress_after: 16
    compress_to_world: true       # T1 产物写回共享黑板
runtime.mode:  (删除)
```

兼容：旧 `brain.*` / `runtime.executor.*` / `runtime.vision.*` 保留读取并映射到 `llm.providers`，一个里程碑后删除。

---

## 9. 里程碑与验收

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M6 共享黑板** ✅ 已完成（2026-09-13） | WorldModel 五项改造（`WorldModel.shared()` 注册表 / RLock / `add_fact(source=)`+`fact_meta` / `view(scope)` / 原子写）+ `SubtaskStore` 子任务板（`subtasks.json`，`claim`/`claim_next` CAS） | 两个并发写者不丢事实、可溯源；`view(scope)` 只看到本分支 + 全局；`tests/test_m6_shared_board.py` 14 项全过 |
| **M7 去分层多 agent** ✅ 已完成（2026-09-13） | `orchestration/graph.py` 重写为「主 agent 节点 + `Send` 扇出子 agent」；主 agent 多了 `dispatch` 元工具（L2，与 task_done/verify/escalate 同级）；删 `manager`/`worker`/`_route`/`_reflect`/`_finalize_end` 的硬编码分层与 `_ENUM_KEYS` 目标词表；`policy.py`（升级路由）删除 | 主 agent 不派发 → 单 agent 跑完（`test_single_agent_no_dispatch_runs_once`）；派发 → 子 agent 真并发（`test_dispatch_fans_out_in_parallel` 断言峰值并发 ≥2 且 wallclock 明显小于串行） |
| **M8 长任务与 HITL** ✅ 已完成（2026-09-13） | T2 预算提示（`_BudgetHintModel` 在 Model 适配层注入）+ T1 压缩产物带 source 归黑板 + `MemorySaver` checkpointer + 软注入（`ToolLoop.inject_message` / `POST /api/runtime/inject`）+ 硬停止（cancel 图 Task） | 注入不打断当前 turn、历史与预算不丢（`test_soft_injection_reaches_main_without_interrupting`）；预算提示只陈述事实且只提示一次；硬停止返回"用户主动停止"而非异常（`test_hard_stop_cancels_running_graph`） |
| **M9 本地模型工具化 + 去三态** ✅ 已完成（2026-09-13） | 新增 `omni_core/tools/local_model_tool.py`（`local_infer`，group=`local_model`，描述 = 通用模板 + 配置边界）；`llm.local_as_tool.{enabled,capabilities,boundary}` 配置；删除 `runtime.mode` 三态（内核 / 后端校验 / 前端 / config） | 未启用时不注册工具；边界只来自配置；内核与后端校验里不再有 online_only/local_only |
| **M10 统一入口** ✅ 已完成（2026-09-13） | `run_task` = 唯一入口（走编排图）；`_run_two_layer_inner` 改名 `_run_graph`；`run_task_two_layer` 降级为转发别名（带 DeprecationWarning）；后端 `/chat`、scripts、测试全部改用 `run_task`；主 agent 补上原「单大脑」路径的长任务压缩 | 只有一个入口（`test_m10_single_entry_point`）；旧单大脑路径的能力（长任务压缩）不丢 |

全程卡 `scripts/review_lint.py --strict` 零红线 + 现有 291 测试不退化。

---

## 10. 风险与回滚

- **并行退化**：若 `ainvoke` 未打通，`Send` 会静默串行（功能对、性能不对）。M7 必须有并发断言测试。
- **黑板膨胀**：`view()` 不做裁剪会让每个 agent 的 prompt 越长越大 → 全局事实需要上限 + 老化（老事实降权/淘汰）。
- **派发失控**：主 agent 可能过度拆分 → `max_parallel` + `max_rounds` + Trajectory 可观测三道闸。
- **checkpointer 序列化**：`OmniState` 里若混入不可序列化对象（backend 句柄、锁）会编译/恢复失败 → 状态里只放纯数据，运行时对象走注册表按 id 取。
- **回滚**：每个里程碑独立 commit；M6 只改 WorldModel/TaskStore 属局部改动，可单独 revert。

---

## 11. 顺带要清的历史债

1. **`orchestration/graph.py:212` `_ENUM_KEYS`**：内核里出现中文目标词表（统计/所有/列表/采集/枚举…）判断"是否假成功"。这是内核领域硬编码，且与"完成判定由主 agent + `backend.verify_done` 负责"直接冲突，M7 一并删除。
2. **`graph.py:248` 无 checkpointer**：软注入与崩溃恢复都没有载体，M8 必配。
3. **`tool_loop._run_inner`**：M4 已标废弃，M10 随统一入口彻底删除（`_assistant_msg` 手搓 FC 序列化一并删）。
