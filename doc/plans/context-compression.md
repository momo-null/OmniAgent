# 跨轮上下文压缩 · 专项（核验后终态）

## 1. 生效路径：只有一条

- `use_compaction = bool(max_input_tokens > 0 and summarize is not None)`（`sdk_loop.py` L1188）。
- 为真 → 块边界压缩整体让位（`if use_compaction: pass`，L1585-1586）；压缩由 `_CompactionModel`（L258）在**每次模型请求前**执行（L320-334：低于阈值透传，零开销）。
- 阈值 = `maxInputTokens × compress_threshold`；`compress_threshold=0` 时取 `× 0.5`（L1190-1191）。当前配置 `maxInputTokens=450000` → **阈值 225000 估算 token**。

结论：压缩恰恰在检查点**内部**生效。「只在检查点之间生效」是过时结论。病因是**阈值晚**，不是机制断。

## 2. 单一机制、两种实现（2026-10-07 简化后）

块边界的多套压缩分支——token 占比 `ctx_window`、轮数 `compress_after`、旧 `_maybe_compress`——已整体删除。它们与粘性压缩重复，且是此前误判「压缩只在检查点间生效」的根源。现在只剩两条互斥实现：

- **粘性压缩**（`use_compaction`，`maxInputTokens > 0`）：每次模型请求前按 token 阈值把较早历史压成摘要，含 Prune-First 与硬上限兜底；
- **硬滑窗**（`history_keep > 0`）：无条件只留最近 N 轮，供无摘要能力的小上下文 worker（`runtime.escalation.worker_history_keep`）。

已删配置：`brain.long_task.enabled` / `max_turns` / `context_window`（前端入口同步移除）。

## 3. 确认缺陷

| # | 缺陷 | 依据 |
|---|---|---|
| D1 | `load()` 不回读 actions（`in_actions_section` 是死变量）→ 续跑 `self.actions` 恒空 | `world_model.py` L402-405 |
| D1b | state_text **会**从 `collected.json` 回读（L421-424）；仅 md 兜底路径（collected.json 缺失/损坏）才丢 | `world_model.py` L421-424 |

| D3 | state_text 填充面窄：只有声明 `percept="state"` 的工具触发 `update()`（`sdk_bridge.py` L366-368），即 GUI 环境 observe/OCR 类；非 GUI 任务无声明者 → 恒 `(无)` | 实测 4 例中 2 例 |
| D4 | facts 唯一自动写入方是**压缩事件**（`sdk_bridge.py` L119）；`record` 工具写的是 notes 不是 facts（`parts.py` L125-130）；无工具把 add_fact 暴露给模型 → 阈值不到则 facts 必为 0 | 与 3/4 为空的观测吻合 |
| D5 | objective 每 run 被本轮输入覆盖（`graph_runner.py` L283 + `chat_runtime.py` L564）；置空只发生在 >2000 字污染防御分支 | |
| D6 | **MaxTurnsExceeded 时整块历史丢失**：异常路径不回填 items（L1435 在 try 内），下一块用旧 items 重放（L1439-1448）；且 `_CompactionModel.kept_from` 与新 items 可能错位 | `sdk_loop.py` L1435-1448 |
| D7 | **token 估算口径偏低**：`_estimate_tokens` 按字符/4（L1690-1694），中文实际约 1 token/字 → 阈值实际放行 60 万+ 真 token。不修口径，调阈值无效 | `sdk_loop.py` L1690-1694 |



## 4. 成本三源 · 哪条在跑

| 源 | 机制 | 状态 |
|---|---|---|
| ① 工具 schema | mcp-filter | 仓库无实现；当前 1 个 MCP server 但**工具数多** → 用户单独验证、单独处理，不随批次1/2 做 |
| ② 历史重放 | 粘性压缩 | 生效，但阈值晚（22.5 万估算 ≈ 60 万+ 真 token 之前不压） |
| ③ 工具返回体积 | prune | **死代码，无生效机制** |

## 5. 路线（拍板：批次1 → 批次2 → 批次3）

### 批次1（原 c′）：砍真实成本，全部在 sdk_loop / 配置内，零内核红线

顺序不可颠倒：

0. **先修估算口径（D7）** —— 阈值换算全靠它，不改则第 1 步调出来的数值没有意义；
1. 再调低 `compress_threshold`（纯配置改动）；
2. 把 prune 接进 `_CompactionModel._compact`（直接砍 ③）；
3. 修 MaxTurnsExceeded 的 items 回填（D6）。

验收：长任务 A/B 步数与 token 对比；prune 命中计数 > 0（③ 有生效机制）。

### 批次2（原 a）：状态质量层，非降本项

objective 保护（D5）、actions 回读（D1）、state_text 填充保证（D3）、facts 自动化（D4）。
定位：修续跑/跨轮的**状态质量**；world_model 进模型的两条通道都被 600 字符截着，填充修好也省不出几个 token。它是将来「摘要层替代重放」的前置，不是成本解。

### 批次3（原 b）：mcp-filter，单独处理

当前 MCP server 工具数多，由用户单独验证后再定方案，不与批次1/2 混做。

## 6. 影响面（改前必查）

- `_CompactionModel` / `use_compaction`：`sdk_loop.py` L1188-1198、L1585-1604，及流式回归 `tests/test_stream_turn_blocks.py`；
- `_prune_tool_results`：调用方仅 L1593 / L1599 / L1604（接入 `_compact` 后为新增调用点，需同步 tests）；
- `_estimate_tokens`：L324、L332、L1590、L1595、L1600（改口径影响所有占比判断）；
- `WorldModel`：`load()`（graph_runner L274、helpers L519、tests/test_world_model L156、test_m6_shared_board、scripts/verify_m0_m4_live L97）、`save()`（graph_runner L432/445/454/474）、`summary()`（graph_runner L150/L298、helpers L520）、`snapshot()`（sdk_bridge L348）、`add_fact()`（sdk_bridge L119、merge_progress L524）。

## 7. 状态

批次1 已完成（2026-10-07）：

- 0 估算口径（D7）：`_estimate_tokens` 改 CJK 1 token/字、其余 /4；新增中文用例（`test_t21`）。
- 2 prune 接入（③）：`_CompactionModel` 加 prune 三参数，`_compact` 先截断再判阈值；参数链 `graph_runner → run_subtask_sdk → _assemble_agent_stack` 接通；新增用例（`test_t32`）。
- 3 items 回填（D6）：`_attach_partial_items` 把已跑出的 items 挂到 `MaxTurnsExceeded` 回传，块循环据此回填。
- 附带：删除 `world_model.load()` 的 `in_actions_section` 死变量，改按 `# ` 标题切段；`recent_actions` 随 `collected.json` 落盘并在 load 回读（D1 修复）；新增用例（`test_world_model`）。
- 附带：清理 `_BudgetHintModel` 残留的「stream_events 未接通」旧注释。

- 简化（已完成）：块边界多套压缩分支（`ctx_window` 占比 / `compress_after` 轮数 / `_maybe_compress`）整体删除，参数链与 `enabled` / `max_turns` / `context_window` 配置同步移除；worker 硬滑窗 `history_keep` 保留。相关测试 83 passed、`review_lint --strict` OK。
- 前端同步：Settings 页移除 `enabled` / `max_turns` 入口，新增 `compress_threshold` 配置项，并修正「真正开关是 `maxInputTokens=0`」的说明。
- D2 续跑 hint / 跨轮注入最近动作单行压缩摘要（`_fmt_action`）已落地。

未完成：

- 1 `compress_threshold`：数值待定（口径修正后实际触发点已从 ~90万真 token 降到 ~22.5万，建议先观察一轮再调）。
- 批次2（world_model 状态质量）、批次3（mcp-filter，用户单独验证）。

## 8. 当前压缩与历史摘要流程（端到端）

### 8.1 参数来源

| 参数 | 出处 | 默认 |
|---|---|---|
| `brain.maxInputTokens` | 输入预算；>0 才是粘性压缩的开关 | 300000 |
| `brain.long_task.compress_threshold` | 触发占比（× maxInputTokens） | 0 → 取 0.5 |
| `brain.long_task.hard_ceiling` | 硬顶占比 | 0 → 取 0.9 |
| `brain.long_task.retain_ratio` | 压缩后尾部保留比例 | 0.5 |
| `brain.long_task.prune.*` | 工具输出首尾截断 | 8192 / 4096 / 1024 |
| `brain.long_task.compress` | 是否用模型生成摘要（否则截断兜底） | true |
| `runtime.escalation.worker_history_keep` | worker 硬滑窗保留轮数 | 3 |

### 8.2 粘性压缩（主链 · 每次模型请求前）

在 `_CompactionModel`（Model 适配层）执行，`_compact` 顺序：

1. **Prune-First**：`_prune_tool_results` 先把超长 `function_call_output` 首尾截断（只改本次请求副本，不落 SDK items）。
2. 修剪后若已低于阈值 → 直接用修剪结果返回，不压。
3. 否则算切点：从末尾累计到「阈值 × retain_ratio」为止，之前的算待压缩区。
4. **粘性摘要**：无新增内容则复用旧摘要（请求前缀逐字节稳定 → 缓存友好）；有新增则增量重压（旧摘要 + 增量 + 八段结构化指令 `_SUMMARY_SECTIONS`）。
5. 产出单条 `[历史压缩摘要] …` user 消息，`items = [摘要] + 尾部会话`。
6. 兜底：摘要失败 / 无效（新摘要 ≥ 原文 60%）/ 超硬顶 → 退化为 `_hard_keep` 纯滑窗。

**只改本次请求入参副本**，SDK 原始 items 不动——会话历史不因压缩增长。每次产出**新**摘要触发一次 `on_compact` 回调（压缩检查点）。

### 8.3 token 估算口径

`_estimate_tokens` → `_text_tokens`：CJK 每字记 1，其余每 4 字符记 1。字段累加 `content / name / arguments / output` 四类。

### 8.4 压缩产物的两个落点

| 落点 | 内容 | 寿命 |
|---|---|---|
| 请求 items | `[历史压缩摘要]` 单条消息 | 每步随 items 重发（**常驻注入每步重发**即由此实现） |
| world facts | `[压缩提取]` 前缀的关键句（`merge_progress` → `add_fact`） | 跨压缩、跨轮持久，防 JPEG 效应 |

facts 这一路只在压缩真的触发后才产生——任务没跑到阈值时 facts 恒为 0。

### 8.5 硬滑窗（worker）

`use_compaction=False` 且 `history_keep>0` 时：块末 `items = _hard_keep(items, history_keep)`，只留最近 N 个 function_call 之后的会话。用于无摘要能力的小上下文 worker。

### 8.6 world_model 状态层与注入出口

- 每步 `log_action` → ring buffer（默认 8 条），存工具名 / args / result。
- `save()` → `world_model.md`（人读摘要）+ `collected.json`（机器真源，含 `recent_actions` 完整内容、`state_text`、`objective`、`facts_meta`）。
- `load()` → 从 md 解析 facts（按 `# ` 标题切段），从 `collected.json` 恢复 collected / notes / state_text / objective / facts_meta / **recent_actions**。
- 三个注入出口，均取 `world.summary()`（最近动作已压缩为单行，参数 80 / 结果 120 字符）：
  - 续跑：`_resume_hint = world.summary()[:600]`
  - 跨轮：`_format_prev_results` 世界摘要整体截 600
  - 子任务间：L2 用 `world.summary()` 注入下一子任务

### 8.7 异常路径

- `MaxTurnsExceeded`：`_attach_partial_items` 把已跑出的 items 挂到异常上回传，块循环据此回填（否则整块对话记录蒸发）。
- 上下文溢出（`_is_context_overflow`）：`_hard_keep` 硬截历史后重试一次。

### 8.8 当前实测状态

短任务 / 未达阈值的任务**不压缩、全量重放**，world facts 为 0——这是预期行为，不是机制失效。压缩是否触发可用 facts 中是否出现 `[压缩提取]` 前缀判定。
