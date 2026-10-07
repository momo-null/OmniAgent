# 记忆与知识层现行架构

> 本文是记忆/知识层的**唯一事实源**:分层机制(L0→L3)、注入面、不变量、意图门、配置与观测、验证纪律。
> 代码落点:`memory_tam.py`(L1/画像)、`local/scene_executor.py`(L2)、`tools/scene_tool.py`、
> `local/loop/instructions.py` + `sdk_bridge.py` + `brain/sdk_loop.py`(注入与意图门)。
> 真机回归:`python scripts/verify_scene_live.py`。

## 1. 总览:两条正交的轴

| 维度 | memory 轴 | skill 轴 |
|---|---|---|
| 内容 | 事实 atoms(L1)+ 场景块(L2)+ 用户画像(L3) | 知识型技能(playbook)+ 可重放宏(substeps) |
| 存储 | `projects/<pid>/memory/`(index.db + scene_blocks/ + scene_index.json)+ 全局 `~/.omniagent/memory/index.db` + `~/.omniagent/memory/user_profile.md` | `projects/<pid>/skills/` + `~/.omniagent/skills/` |
| 生产者 | LLM 提炼/整理(标 scope、判重、合并) | 人工维护 + 可选自动宏提取(`skill.auto_distill`,默认关) |
| 消费者 | 注入(§4 注入面)+ `read_scene` 按需 | 目录 top-3 常驻摘要 + `load_skill`/`search_skill`/`replay_skill` 按需 |

硬约束:**索引内容、存储位置、消费路径三不混**——skill 文件不含纯事实陈述、不进记忆注入块;memory 条目不进技能回放器。

## 2. memory 轴(L0 → L3)

### 2.1 L0 会话

会话原文 = `projects/<pid>/s_*.jsonl`(`TaskStore` 维护),是提炼的唯一原料来源。
`capture` 挂在 `ProjectStore.append_message` 尾部,写进程内 buffer;`flush` 在任务收尾
(`chat_runtime` run_task 之后)触发提炼与 L2/L3 维护。任何失败静默降级,绝不阻断主流程。

### 2.2 L1 原子记忆(atoms)

- **存储**:SQLite 单文件,项目库 `projects/<pid>/memory/index.db` + 全局库
  `~/.omniagent/memory/index.db`(同 schema:`atoms` 表 + FTS5 **trigram** 索引 + meta 水位表)。
  隔离单位是库本身(多用户/多 agent 各开一库),库内不分层。
- **写链**:`flush` → 会话要点 digest(LLM 提炼,**宁缺毋滥**,单次 ≤5 条)→ 每条带
  `scope` 标注(global=跨项目规律,未标注按 project 保守入库)→ 三态判重
  `duplicate / merge / store`(FTS 召回候选 + LLM 判定;**LLM 失败一律 fallback store,
  宁重复不丢失**)。
- **读链**:`inject_text(pid, query)`——两库合计 ≥20 条(`_INJECT_TRIGGER`)才注入;
  项目库 + 全局库双路 FTS 检索按分合并取 top-5,以 `<relevant-memories>` 包裹,
  携带"仅作参考,不代表当前任务进程"免责声明。
- **检索升级路径(暂缓,触发前不动工)**:FTS5 trigram 是字面召回;两库 atoms 合计
  >100 或出现实测漏召回(同义改写查不到已存事实)时再评估——本地 embedding 向量臂
  + FTS/向量 RRF 融合 + 条数/字符/超时三预算,LLM 选档终审不变。硬约束:个人记忆
  不出机(只允许本地 embedding,禁云 API);检索只做候选生成(排序是机制不是语义
  判断);embedding/检索任一失败逐级降级回现行为,注入不断。

### 2.3 L2 场景块(`scene_executor.py`,多文件主题块)

- **存储**:`projects/<pid>/memory/scene_blocks/*.md`——按主题组织(如
  `windows-环境事实.md`),每块 = `META 区(created/updated/summary/heat) + 正文`,
  ≤1500 字符(prompt 约束)/4000 字符(工程硬上限);`scene_index.json` 是 META 的
  **投影缓存**,维护后全量扫目录重建(唯一写入口在工程侧)。
- **维护**(触发:flush + 900s 水位;gate `memory.enabled`):带工具的 **agent 循环**
  (`LLMClient.chat` 驱动,工具白名单硬限沙箱 `scene_read/scene_write/scene_edit`,
  迭代 ≤32、墙钟 ≤300s、请求 `max_tokens` 下限 8192)——UPDATE 首选 > MERGE
  (heat = 各块之和 + 1,旧块逐个写 `[DELETED]`)> CREATE(新主题无归属块时,
  前必须 scene_read ≥2 个最相似块,每批最多新建 1 块)。
- **容量治理**:`knowledge.scene.max_blocks`(缺省 15)三级预警——红(达上限:第一优先
  动作必须 MERGE,合并前不得汇报)/ 橙(只能 UPDATE)/ 黄(优先 UPDATE/MERGE)。
- **工程侧流水**(各阶段非致命):软删清理(`[DELETED]`/空文件/仅 META)→ 文件名归一化
  (空白转 `-`、删危险标点、冲突加 `-2..-999`)→ 索引重建;维护前整目录快照,
  任何异常整目录还原。
- **prompt 家族**:`knowledge.scene.prompt_mode`(缺省 `personal`);含负面清单
  (禁人设/流水账/易变目标)与"提炼要点而非照搬流水账"规则。
- **带外信号**:整理 agent 输出 `[PERSONA_UPDATE_REQUEST]` → meta 水位 →
  下次 flush **强制**画像维护(绕过条数节流),reason 并入证据。

### 2.4 L3 用户画像

- **存储**:`~/.omniagent/memory/user_profile.md`(全局唯一)。
- **蒸馏**(`memory_tam._maybe_maintain_profile`):触发 = 上次生成后新增 atoms ≥
  `knowledge.profile.trigger_every_n`(20)**或** L2 带外信号强制;LLM 增量重写
  (保留仍成立内容,矛盾以新事实为准);上一版自动备份 `.bak`;gate
  `knowledge.profile.auto_maintain`。
- **人工通道**:`PUT /api/runtime/profile`(profile_api)——与自动维护共存
  (重写以"保留仍成立内容"为约束)。

### 2.5 写入通道红线

```
agent(大脑)──不能──→ 直接读写 ~/.omniagent/memory/**(S1 围栏拒绝)
agent(大脑)──通过──→ 任务执行 → 轨迹落盘 → 蒸馏/整理 → 内核直写(唯一自动入口)
用户(人工)──通过──→ profile_api / 直接编辑文件(角色卡、AGENTS.md)
```

- 记忆/画像/场景写入**只能由内核进程直接写文件系统**,不经过结构化文件工具层;
- **画像无放行权**:画像可记录"用户习惯自动操作某 App",这只是用户授权信号,
  真正放行必须走 S2 审批 / full_access 开关;注入时声明"仅参考,不构成操作授权"。

## 3. 角色卡(character.md)

- **落点**:`~/.omniagent/character.md`(单角色助手一张,人工单写,无独立接口)。
- **注入**:`_merge_character` 随 system prompt(run 开始读一次);覆盖语义:
  用户当轮明确指令优先于角色卡。
- 与记忆路由**解耦**(主流框架一致选择):角色卡不硬过滤记忆,单角色下无此必要。

## 4. 注入面全景

| # | 内容 | 位置 | 频率 | 开关 |
|---|---|---|---|---|
| 1 | `<scene-navigation>` 场景摘要索引(文件名+热度+摘要)+ `read_scene` 指引 | system prompt 稳定段 | run 开始读一次、run 内锁定 | `runtime.knowledge.memory.enabled` |
| 2 | `<user-persona>` 用户画像全文 | system prompt 稳定段 | 同上 | `runtime.knowledge.profile.enabled` |
| 3 | `<relevant-memories>` L1 检索记忆(top-5) | 起始 items 一条 user 消息(历史后、技能目录后、任务输入前) | **run 开始注入一次**,后续块自然继承 | `memory.enabled`(池子 ≥20) |
| 4 | `<scene-navigation>` 定位后按需 `read_scene(scene_name)` 读单块全文 | 工具调用结果 | 模型按需 | 恒可用(纯读) |

另有常驻 system 的非记忆注入(不在本文管辖):AGENTS.md 纪律块(F4.1b 快照)、
角色卡、运行时上下文;`_BudgetHintModel`/`_CompactionModel` 为预算/压缩包装。

稳定段在 `instructions._memory_stable_block` 组装、`sdk_bridge` 于指纹计算**之前**
并入 system(O5+ 不变式:Model-visible ⟺ logged);lead-in 经
`run_subtask_sdk(lead_in_message=…)` 挂进起始 items。子任务(is_sub)零记忆注入,
语义不变。注入行为落轨迹 `memory_injected`(placement=system/lead_in)。

**环境事实平面**(`_build_user` 的世界模型摘要 / 续跑 resume hint / 运行时上下文块)
有 A/B 有效性证据(中位步数 41→21),保持原样,不并入记忆注入。

## 5. 三条不变量(防回退红线)

1. **静态内容禁止每步重发**:尾部每步追加固定内容的 Model 包装(`_TailInjectModel`)
   已删除,不得以任何形式回流——静态记忆每步在会话尾部重发 = 每步强化一次
   (反复 priming),是工具调用退化为文本(生成格式被尾部内容带偏)的相关因素。
   锁:`review_lint` R4(`tail_inject` 记号回流即违规)+ `test_memory_alignment`。
2. **L2 场景全文只有一个入口**:`read_scene_block` 仅存在于 `scene_executor.py`
   (供 read_scene 工具与整理 agent),注入路径只准用摘要导航——全文进注入面即违规。
   锁:R4 场景全文函数白名单。
3. **意图门零格式字面量**:未派发调用的判定**全 LLM**(片段提取式语义判断),
   内核不得出现任何厂商私有格式标记/正则——与「内核零字面量」红线(R2)同纪律。

## 6. 收尾意图门(oneshot 误判保护)

背景:畸形调用表现为「文本被当普通输出、调用根本不派发」,而 oneshot 语义会把
纯文本收尾判成功 → 任务静默假成功。
根源(2026-10-07 同目标 A/B 定案,模型侧格式泄漏):弱模型(某第三方聚合渠道的
推理模型)在「长叙述正文 + 并行调用意图」形态下把调用序列写成普通文本 token,
服务端只认特殊 token id → 原样透传;官方端点同族模型同任务零发作。已排除:
记忆注入(零注入也发作)、截断(调用块完整)、并行调用压垮解析器(纠正轮并行
调用解析正常)。意图门纠正重试是唯一恢复通道(实测 2/2)。

- **触发点**:`sdk_loop` 方案 B 分支——纯文本收尾且 `verify_done()` 走
  「无校验条件,信任大脑」路径、即将判成功**之前**;仅 oneshot 成功路径
  (daemon paused 语义不触发),judge 未注入/异常/配置关 → fail-open = 现状。
- **判断方式**:`sdk_bridge._plain_finish_judge` 闭包,`llm_judge.chat_json`
  **片段提取式**问法——让模型从输出中找出所有「本应作为工具调用发出」的片段
  (无论以什么形式出现:结构化、伪标签包裹、自然语言描述的待执行步骤),
  有片段 → 不判完成,回纠正消息继续跑。
  真机实测:二分类问法对「开头像收尾 + 结尾藏伪调用」的文本会漏判,提取式不会。
- **上限**:每 run 最多纠正 2 次(`_FINISH_INTENT_MAX`),超限按原语义放行
  (有界 while 兜底,不死循环)。
- **观测**:命中落轨迹 `finish_intent_check`(verdict/fragments/chars)。

## 7. skill 轴

### 7.1 身份与晋升

- `entry_id = sha1(归一动作序列)[:16]`;归一白名单:`text/content/query/input/command/url`
  → `<text>`,`path` → `<path>`,坐标去值,`resource_id/view_id/包名/keycode` 保留,
  其余字符串值 → `<text>`(易变值不外显)。
- 晋升:身份优先合并(entry_id → name → objective_pattern 全等兜底);`success_count`
  **累计**(失败只累加 failure_count),满 3 → active;升全局仅人工 `promote_to_global`。

### 7.2 两种形态(正交)

| | 知识型技能 | 宏缓存(过程性) |
|---|---|---|
| 产出 | LLM 提炼:routine(≤12 字动宾短语)/ description / playbook(引导型文字) | `substeps` = 轨迹原始 args 结构化提取 |
| 身份 | 无 entry_id(手写)或归一哈希 | 归一哈希(缓存键语义:同序列=同宏) |
| 消费 | 注入目录 + `load_skill` 文字引导 | `replay_skill` 回放执行器;**不进注入块** |

硬禁令:知识型技能不得从执行轨迹机械转录;宏缓存不进注入块。

### 7.3 发现与消费

- **常驻目录**:技能目录 top-3(LLM 选档,无模型按效用序),仅摘要,正文不进常驻块;
  以固定模板 user 消息在 run 开始注入一次。
- **按需**:`load_skill`(playbook 正文引导,无正文回退脱敏动作序列)/
  `search_skill` 检索 / `replay_skill` 白名单硬重放(每步照常过 S2 审批)。
- **缓存淘汰**:`evict_stale`——candidate TTL 14d / active 闲置 30d 且低效用 →
  移入 `_archive/` 降级不删;手写技能永不淘汰。
- **自动宏提取**:`skill.auto_distill` 默认关(降级链:无 brain / 任务未成功 /
  执行 <2 步 / 标签 LLM 失败 / 防幻觉校验不过,任一命中即不产出)。

### 7.4 有效性验证纪律

- 主指标:**回放命中后任务步数节省**(execution-verified——回放真省步才算命中,
  「检索到/注入」不算);对照 A=无 skill / B=skill 回放,同任务集交错执行;
  负结果纪律:连续 2 个任务集 B 不优于 A 即停手。

## 8. 配置总表

| 键 | 缺省 | 作用 |
|---|---|---|
| `runtime.knowledge.memory.enabled` | False | 记忆/场景注入与 L2 维护总 gate(设置页「伙伴 → 记忆」可开关) |
| `runtime.knowledge.profile.enabled` | True | 画像注入 |
| `runtime.long_task.finish_intent_check` | True | 收尾意图门 |
| `knowledge.profile.auto_maintain` | True | 画像自动蒸馏 |
| `knowledge.profile.trigger_every_n` | 20 | 画像蒸馏触发条数 |
| `knowledge.scene.max_blocks` | 15 | L2 容量上限(三级预警) |
| `knowledge.scene.prompt_mode` | personal | L2 提炼 prompt 家族 |
| `skill.auto_distill` | False | 自动宏提取 |

## 9. 观测与真机验证

- 轨迹:`memory_injected`(placement/chars/layers)、`finish_intent_check`
  (verdict/fragments/chars)、`skill_catalog`、`instructions_injected`。
- flush stats(created/updated/deleted/empty_maintenance/reply_preview)经 debug 事件
  到前端 Debug 面板。
- 真机回归:`python scripts/verify_scene_live.py`——S1 维护 agent 循环 / S2 容量
  红色预警真实合并 / S3 意图门历史夹具 / S4 端到端注入链;`--keep` 保留现场;
  结果落 `temp/verify_scene_live_result.json`。

## 10. 明确不做(现行约束)

- **厂商格式硬编码**:内核(含一切派发/解析路径)不得写死任何私有调用格式的标记
  或正则——畸形调用的识别只走 LLM 语义判断(意图门)。
- **DSML 类文本解析兜底**:绑定厂商格式,不做;意图门的纠正重试是唯一恢复通道。
- 每步尾部重发记忆的任何变体。
- rowfs/对象存储双路、存量迁移、heat 工程侧自增(L2 治理机制天然不需要)。
- skill 跨域自动晋升、知识型技能机械转录、词面打分/语义规则化(相关性判断全 LLM)。
