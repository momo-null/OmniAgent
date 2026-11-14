# 运行管线统一（run pipeline）提案

- 状态：已实施（P1~P3，测试与静态检查通过，待验收）
- 日期：2026-11-14
- 背景：迁移计划完成后复查发现，Web 与渠道两条任务链路在入口之下各维护了一份
  几乎相同的中段管线，已出现行为漂移。本文档给出逐段对照与抽取方案，并记录实施结果。

## 0. 结论先行

- **入口不合并**。两个入口的语义 genuinely 不同（同步流式 vs 异步串行队列），
  合并会让 Web 聊天被渠道消息的 FIFO 队列阻塞，或让渠道背上一堆它用不到的交互态。
- **中段管线收敛成一份**。重复的不是入口，是「组 TaskSpec → 过程记录 → 终态收尾」
  这段装配逻辑。抽成共享装配层后，两边只保留各自的差异面。
- **抽取范围收窄**：只抽无争议的公共段；Web 专属交互态（SSE 推送、打断、补投、
  TAM、daemon paused）全部留在 `chat_runtime`，不进公共层——否则公共层会长满
  `if web`，比现在更难维护。

## 1. 两份现状（逐段对照）

A = `backend/api/routers/chat_runtime.py::_dispatch_chat`（Web）
B = `harness/dispatch.py::_run_task_once`（渠道 / cron）

| 段 | A（Web） | B（dispatch） | 评价 |
|---|---|---|---|
| 大脑配置来源 | `_make_brain_cfg(model)`，支持前端显式选模型 | `model_router.resolve_main_with_engine(brain_src)`，只认 main 槽位 | 同源不同读法；A 支持按次选模型是合理差异，**保留** |
| 任务落库 | 调用方已建 task，入口只查 meta（一个会话一个 task） | 入口内做三形态（复用会话 task / 新建持久 / 临时任务跑完即删） | B 的三形态是渠道语义，**保留在 B** |
| 会话历史重建 | `coalesce_records(read_session(limit))` → `_session_records_to_model_messages` | `coalesce_records(read_session(limit=400))` → `_build_history_items`（12/40 条 + 90 分钟窗 + 静默过滤） | **重复且已漂移**：B 有 IM 杠杆（静默 run 不进历史、时间窗、折叠提示），A 没有；两边 limit 也不同。语义上有理由（IM 高频闲聊 vs Web 主人对话），但「读尾部 + coalesce」的骨架是同一份 |
| 场合自述注入 | `outbound.resolve("web", chat:task_id)` | `outbound.resolve(channel, chat_id)` | 同构，仅键不同。**可抽** |
| context_signal / 进料道标注 | `set_source_kind(source_kind)` + `set_boundary(context_key=f"web:{sid}")` | `set_source_kind(chat/task)` + `set_boundary(context_key=chat_id)`，还多 `set_current_reply` / `set_source_external` / `reset_sent` | B 多出的三个是渠道发送闸所需，**留在 B**；`set_boundary` 骨架**可抽** |
| partial 增量落盘 | `_maybe_flush_partial`：每 20 步、`{steps, partial, run_key}` | `_flush_partial`：每 20 步、同 extra 结构，多带 `context_key/l0_kind/speaker` | **重复**。差异只在 extra 的进料道参数，**可抽**（参数化） |
| thinking 累积 | 流式：`_merge_thinking_step` 按 block_id upsert；另有 SSE push | 流式：`_think_acc` 按 block_id upsert（注释还引用了 A 的 `_merge_thinking_step`） | **重复**：同一个 upsert 语义写了两遍（B 侧当年还修过 O(n²) 快照 bug，A 侧没这个 bug 是因为先修的） |
| 工具调用记录 | 过滤 verify/verify_done，push SSE + append step | 同过滤，append step（无 SSE） | **可抽**：过滤规则一份，推送回调参数化 |
| TaskSpec 组装 | objective=last_user+守则、history=model_messages、task_mode | objective=消息+守则+折叠提示、history=杠杆塑形后、tool_allowlist、attachments | 字段集合同源；组装规则**可抽**成 builder |
| 记忆入队 | `memory_writer.enqueue(pid, brain_cfg, did_work, on_done=debug_push)`；force 轮跳过 | `enqueue`（task）或 `enqueue_throttled`（chat 高频合并）；无 on_done | **结构重复**：A 有「打断跳过 + debug 回调」，B 有「chat 节流」。两者都该在公共封装上留钩子 |
| 终态收尾 | `_extra={steps, meta, final, run_key, artifacts?}` → `_append_assistant(conclusion, extra)`；steps>300 省略 | `_record_final`：`{steps, run_key, silent?, send?}` + 显式模式记账 | **重复**：都是「收口一条带 steps 的 assistant 记录」；A 多 meta/tokens/artifacts，B 多 silent/send 标记。extra 构造**可抽**，标记各自附加 |
| TaskStore 状态机 | finally 统一 `state=done`（daemon paused 除外）+ 补投续跑 | 成功 done / 失败 failed + runs 记录 + `_notify_ui` 事件桥 | 状态语义不同（Web 永远 done；B 有 failed + 通知），**保留各自**，但 `update(state=…)` 的调用点可统一 |
| 结果回发 | SSE outbox（进程内）+ `_append_assistant` | ReplyChannel 回发 / cron broadcast / 失败直推 + 显式模式兜底代发 | 本质差异，**保留** |
| 产物收编 | 不收编（artifacts 列表进 extra 供前端展示） | `artifact_collector.collect()`（语料/日记归位项目目录） | 语义不同，**保留** |

## 2. 已发生的漂移（抽取的动机，不是洁癖）

1. **历史塑形**：B 的静默过滤 / 时间窗 / 折叠提示三轮杠杆只存在于 B。A 的 Web
   历史没有静默概念是合理的，但「读尾部 + coalesce + 裁剪」骨架两边各写一份，
   任何一端的窗口规则再演进都要人肉同步。
2. **thinking 快照 upsert**：A 用 `_merge_thinking_step`，B 用 `_think_acc`，
   同一契约写两遍——B 的注释直接承认是照着 A 抄的；A 先修过快照 O(n²) bug，
   这类修复天然只落一边。
3. **partial 落盘**：两边都是「20 步刷一条 partial」，但 B 的 partial 额外带进料道
   标注。将来改 flush 频率或 partial 结构要改两处。
4. **记忆入队**：A 的 force-跳过逻辑 B 没有（B 无打断概念，正常），但 B 的
   chat 节流 A 也没有——如果 Web 语音入口将来高频对话，A 会直接踩 B 已修过的坑。

## 3. 抽取方案（已实施）

新模块落在 `harness/run_pipeline.py`（宿主侧装配层）。不放进 `omni_core/local/`
的原因：`append_outbound_note` 要读 `harness.outbound` 的通道配置，若模块在 omni_core，
内核就会反向依赖 harness，直接违反既有「内核不反向依赖宿主」守卫；harness 本身
依赖 omni_core，是单向的。

```
run_pipeline.py
├─ build_history(records, *, max_items, time_minutes, drop_silent,
│                 now_ts, roles, converter)
│    # A(Web): roles=None + 自定义 converter（保留 system/任务卡/12 轮窗口）
│    # B(渠道): max_items=12/40, time_minutes=90, drop_silent=True, 默认 converter
├─ append_outbound_note(objective, channel, chat_id)   # 场合自述，A/B 同构
├─ class TurnRecorder:                                 # 过程记录三件套
│    ├─ add_thinking(role, content, model, *, block_id=None)  # 流式 upsert / 非流式追加
│    ├─ add_message_delta(block_id, model, content)
│    ├─ add_tool_call(role, name, args, result, model)        # 过滤 verify/verify_done
│    ├─ flush_partial(force=False)                     # 20 步阈值 + run_key + 可选进料道 extra
│    └─ record_final(content, extra_override)          # final 收口
├─ enqueue_memory(pid, brain_cfg, *, source_task, did_work, throttled, on_done)
│    # throttled=True（渠道 chat）走 enqueue_throttled；Web 不节流，on_done 接 debug 面板
└─ set_run_boundary(...) / clear_run_boundary()        # context_signal + source_kind 骨架
```

抽取后的调用方形态：

- `chat_runtime._dispatch_chat`：SSE 推送回调包住 `TurnRecorder`（每个回调先
  `push_chat/push_thinking` 再交给 recorder）；打断 / 补投 / daemon paused / meta
  构造全部留在原地。
- `dispatch._run_task_once`：`set_current_reply` / `set_source_external` /
  `reset_sent` / 显式模式记账 / 兜底代发 / 回发全部留在原地。

**不进公共层的清单**（明确画出去，防止边界蔓延）：
SSE 任何推流、force 打断与 drain_human_injections 补投、daemon paused、
`manager`（RuntimeManager 句柄）、`_debug_push`、TaskStore 状态机差异、
ReplyChannel 回发、失败直推、产物收编、三形态任务落库、`resolve_main_with_engine`
与 `_make_brain_cfg` 各自的模型解析。

## 4. 实施分期

| 期 | 内容 | 风险 |
|---|---|---|
| P1 | `build_history` + `append_outbound_note`（最纯的两段，B 现调用点直换） | 低：B 侧行为不变，A 侧只是骨架换实现 |
| P2 | `TurnRecorder`（thinking upsert / partial / final 三件套） | 中：A 的 SSE 回调与 recorder 交织，需要回调顺序测试 |
| P3 | `enqueue_memory` + `set_run_boundary` | 低 |
| 每期 | 全量 pytest + 现有守卫 + Web/桌面手工冒烟（各一轮对话 + 一条 IM 消息） | — |

验收判据：
1. 抽取后两侧行为零变化——用「同一会话在 Web 与渠道各跑一轮」的过程记录 diff
   （steps 结构、partial 条数、final extra 字段）做回归基线；
2. `grep` 确认 `_merge_thinking_step` / `_think_acc` / `_maybe_flush_partial` /
   `_flush_partial` 四个旧实现只剩公共层一份；
3. 守卫测试补一条：两入口不得各自重新实现历史塑形与 partial 落盘（用现有
   `tests/test_plugin_events_guards.py` 同款文本扫描即可）。

## 5. 备选：不抽，只写「镜像变更」守卫

若不想动这段（当前两份各自稳定），最低成本方案是加一条守卫测试：改动任一侧的
历史塑形 / partial / thinking upsert 时强制提示同步另一侧（文本扫描 + PR 检查表）。
缺点：守卫只能提醒，不能消除漂移本身；且 B 侧注释里已经出现了照抄 A 的字样，
说明维护者已经需要靠记忆同步。

## 6. 建议

按 P1→P3 分期抽。P1 收益最大风险最小（历史塑形是漂移最严重的一段）；
P2 是唯一有实质风险的一步，放在有完整回归基线之后。

## 7. 实施结果（2026-11-14）

分支 `feat/run-pipeline-unification`。

| 期 | 落点 | 结果 |
|---|---|---|
| P1 | `harness/run_pipeline.py::build_history` / `append_outbound_note` | dispatch 的 `_build_history_items` 改为薄包装（12/40、90 分钟窗、silent 过滤参数不变）；Web 历史改走共享骨架（`roles=None` 保留 system，converter 保留任务卡/12 轮窗口）；两端场合自述统一 |
| P2 | `harness/run_pipeline.py::TurnRecorder` + `merge_*` | 两端删除各自的 `_flush_partial` / `_maybe_flush_partial` / `_think_acc` / `_append_assistant`；thinking/message 按 block_id 快照 upsert；partial 20 步、final extra 覆盖语义保留；渠道 partial 的 `context_key/l0_kind/speaker` 经 `append_extra` 透传 |
| P3 | `harness/run_pipeline.py::enqueue_memory` / `set_run_boundary` / `clear_run_boundary` | Web 不节流 + debug `on_done`、渠道 chat 节流保留；两端 `source_kind + context_signal` 写入与 finally 清理收敛；`sent_count` 仍传函数对象，避免行为漂移 |

**明确未动**（按 §2 清单）：SSE 推流、force 打断/补投、daemon paused、manager、
ReplyChannel 回发、失败直推、产物收编、三形态任务落库、两种模型解析。

**验证**
- 定向回归：`test_gateway_dispatch.py`、`test_memory_read_bundle.py`、
  `test_stream_turn_blocks.py`、`test_harness_host.py`、`test_plugin_events_guards.py`、
  `test_bilibili_page_server.py`、新增 `test_run_pipeline.py` 全通过。
- 全量 `pytest -q` 退出码 0；`scripts/review_lint.py --strict` 通过。
- Web `tsc --noEmit` / `npm run build`、桌面 `npm run build` 见提交记录。
