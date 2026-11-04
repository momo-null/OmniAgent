# 跨轮上下文压缩 · 现行架构

> 本文是上下文压缩的**唯一事实源**：生效路径、两种实现、压缩流水、估算口径、产物落点、
> world_model 状态层、异常路径与配置。代码落点：`omni_core/brain/sdk_loop.py`
> （`_CompactionModel` / `_hard_keep` / `_prune_tool_results` / `_estimate_tokens`）、
> `omni_core/local/loop/sdk_bridge.py`（`_summarize` / `_compress_history`）、
> `omni_core/local/loop/graph_runner.py`（参数装配）、`omni_core/local/world_model.py`（状态层）。
> 未完项见 `doc/plans/Backlog.md`「上下文压缩 · 遗留项」。

## 1. 生效路径：单一开关

- `use_compaction = bool(max_input_tokens > 0 and summarize is not None)`（`sdk_loop.py`）。
- 为真 → 块边界不做任何压缩（`if use_compaction: pass`）；压缩由 `_CompactionModel`
  （Model 适配层）在**每次模型请求前**执行：低于阈值透传、零开销。
- 阈值 = `maxInputTokens × compress_threshold`；`compress_threshold=0` 取 `× 0.5`，
  硬顶取 `× 0.9`。
- `brain.maxInputTokens = 0` 是显式关闭（前端设置页同口径说明：真正的开关是它，不是
  `compress_threshold`）。

## 2. 两种实现（互斥，按模型能力二选一）

| 实现 | 条件 | 行为 |
|---|---|---|
| 粘性压缩 | `use_compaction`（`maxInputTokens > 0` 且模型可摘要） | 每次模型请求前按 token 阈值把较早历史压成摘要（Prune-First + 硬顶兜底） |
| 硬滑窗 | `history_keep > 0` 且未启用粘性压缩 | 块末无条件只留最近 N 轮（`_hard_keep`），供无摘要能力的小上下文 worker（`runtime.escalation.worker_history_keep`，缺省 3） |

## 3. 粘性压缩流水（`_CompactionModel._compact`）

1. **Prune-First**：`_prune_tool_results` 先把超长 `function_call_output` 首尾截断
   （阈值 8192 字符，头 4096 + 尾 1024；只改本次请求副本，不落 SDK items）；
   修剪后已低于阈值（校准口径，见 §4）→ 直接返回，不压。
2. **阈值判断（校准口径）**：`_predict(估算) > threshold` 才继续压缩——有真实
   usage 样本时用校准预测，无样本退化为纯估算（§4）。
3. 切点：从末尾累计到「阈值 × retain_ratio」为止，之前为待压缩区。
4. **粘性摘要**：待压区间无新增 → 复用旧摘要（请求前缀逐字节稳定，前缀缓存友好）；
   有新增 → 增量重压（旧摘要 + 增量）。摘要指令统一为八段结构化检查点
   `SUMMARY_INSTRUCTION`（任务目标 / 已完成动作 / 关键观测与工具结果 / 已生成产物 /
   当前状态 / 未决问题 / 下一步计划 / 风险与约束），经 `_compress_history` 的
   **system prompt** 下发，指令本身不混入待压正文，总长目标 ≤ 原文 40%。
5. 产出单条 `[历史压缩摘要] …` user 消息，`items = [摘要] + 尾部会话`。
6. 兜底：摘要失败 / 无效（新摘要 ≥ 原文 60%，视为没压动）/ 超硬顶（校准口径）→
   `_hard_keep` 纯滑窗。

- SDK 原始 items 不动（只改本次请求入参副本）——会话历史不因压缩增长。
- 每次产出**新**摘要触发一次 `on_compact` 回调（压缩检查点；粘性复用不触发，
  回调异常被吞绝不影响请求）。挂点已备，上层当前未消费（未传 `on_compaction` → 空操作）。

## 4. token 口径：估算基座 + 真实 usage 自校准（2026-11-05）

两层口径：

- **估算基座 `_estimate_tokens`**：依次累加 message 的 `content / name` 与工具调用的
  `arguments / output` 四类字段；`_text_tokens` 按 **CJK 每字记 1、其余每 4 字符记 1**。
  对 ASCII 密集的工具输出系统性低估（4 字符/token vs 实际 ~3）——历史教训：中文语义下
  按字符/4 会把阈值放大约 4 倍，使压缩形同虚设（旧口径 bug，自校准的引入动机之一）。
- **真实 usage 自校准（`_CompactionModel._observe` / `_predict`）**：每次模型响应的
  `usage.input_tokens` 是**绝对真值**（含 system / tools schema 全额上下文）。
  k = 真实/估算 按 EMA 收敛（首样本直接锚定；比率出 `[0.2, 8]` 视为离群拒收，
  防端点切换/异常响应污染）。**阈值与硬顶判断走 `k × 估算` 的校准预测**。
  逐次重锚定：误差只在「上次真值以来的增量」一段，不跨调用累积——长任务
  （几小时几百次调用）恰是收敛最好的场景；压缩触发后上下文骤降，下一次响应自动重锚。
- **降级链**：端点不回 usage（流式未开 `brain.request.include_usage` / 端点不支持）
  → k 恒 0 → 完全退化为纯估算，与历史行为一致（不会更差）。第 1 次请求发出前
  是唯一无真值窗口（彼时上下文最小，纯估算误差无谓）。
- **仍走纯估算的判断**：切点保留比例（retain_ratio）等相对量判断——只需比例关系，
  不需绝对精度。

## 5. 压缩产物的两个落点

| 落点 | 内容 | 寿命 |
|---|---|---|
| 请求 items | `[历史压缩摘要]` 单条消息 | 每步随 items 重发（**常驻注入每步重发**即由此实现） |
| world facts | `[压缩提取]` 前缀的关键句（`_summarize` → `_compress_history` → `add_fact`） | 跨压缩、跨轮持久，防 JPEG 效应 |

facts 只在压缩真实触发后产生——任务没跑到阈值时 facts 为 0，这是预期行为。
判定：facts 中出现 `[压缩提取]` 前缀 = 压缩已触发过。

## 6. world_model 状态层与注入出口

- 每步 `log_action` → ring buffer（缺省 8 条：工具名 / args / result）；`save()` 落
  `world_model.md`（人读摘要）+ `collected.json`（机器真源，含 `recent_actions` 完整内容、
  `state_text`、`objective`、`facts_meta`）；`load()` 按 `# ` 标题切段解析 facts、
  从 collected.json 恢复其余字段（空值不覆盖）。
- 三个注入出口，均取 `world.summary()`（动作压缩为单行：参数 80 / 结果 120 字符，
  摘要整体截 600 字符）：续跑 resume hint / 跨轮 `_format_prev_results` / 子任务间注入下一子任务。

## 7. 异常路径

- `MaxTurnsExceeded`：`_attach_partial_items` 把本块已跑出的 items 挂在异常上回传，
  块循环据此回填后续跑（不回填会让下一块拿旧 items 重放，整块对话记录蒸发）。
- 上下文溢出（`_is_context_overflow`）：`_hard_keep` 硬截历史后重试一次
  （`overflow_retried` 防死循环）。

## 8. 配置总表

| 参数 | 出处 | 缺省 |
|---|---|---|
| `brain.maxInputTokens` | config.py / 设置页 | 300000（>0 启用粘性压缩；0 = 显式关闭） |
| `brain.long_task.compress_threshold` | 运行时回退（config 无键） | 0 → 取 0.5 |
| `brain.long_task.hard_ceiling` | 运行时回退 | 0 → 取 0.9 |
| `brain.long_task.retain_ratio` | config.py | 0.5 |
| `brain.long_task.prune.threshold_chars / head_chars / tail_chars` | config.py | 8192 / 4096 / 1024 |
| `brain.long_task.compress` | bridge（缺省 true） | true（关闭时摘要为占位文本、不写 facts） |
| `brain.request.include_usage` | bridge（缺省 true） | true（流式请求随单 usage——真实用量/截断检测/自校准的数据源；个别拒收该参数的端点可关） |
| `runtime.escalation.worker_history_keep` | config.py | 3（worker 硬滑窗保留轮数） |

## 9. 观测与验收

- 压缩是否触发：world facts 中是否出现 `[压缩提取]` 前缀（§5）。
- prune 生效：命中计数 > 0（`tests/test_t22_prune.py`）。
- 真实用量观测：每次 LLM 调用的 `tokens_in / tokens_out / tokens_cached` 随 `llm_timing`
  落轨迹（SSE `trajectory_append` 实时推前端）；run 收尾按「主链+子 agent」汇总进
  result → Chat 页 meta（完成行 + 上下文圆环消费）。端点不回 usage 时为 0。
- 相关测试：`test_t21_estimate_tokens.py`（估算口径）/ `test_t22_prune.py`（Prune-First）/
  `test_t32_compaction_model.py`（粘性压缩全链 + 摘要指令统一 + **真实 usage 自校准**）/
  `test_world_model.py`（状态层持久化）/ `test_stream_turn_blocks.py`（流式回归）。
- 真机验收口径：长任务 A/B 步数与 token 对比。
