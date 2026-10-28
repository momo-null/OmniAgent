# 记忆与知识层现行架构

> 本文是记忆/知识层的**唯一事实源**:分层机制(L0→L3)、注入面、不变量、意图门、配置与观测、验证纪律。
> 代码落点:`omni_core/memory_brain.py`(L0 缓冲 + L1 单库 + L3 画像 + 巩固周期)、
> `omni_core/local/scene_executor.py`(L2 机制与触发薄壳)、`tools/scene_tool.py`(read_scene)、
> `tools/memory_tool.py`(agent 自管工具)、`local/loop/instructions.py` + `sdk_bridge.py` +
> `brain/sdk_loop.py`(注入与意图门)。
> 真机回归:`python scripts/verify_memory_recall.py`、`python scripts/verify_scene_live.py`。
>
> **2026-10-28 架构清理**：L1 已收敛为单库 `~/.omniagent/memory/brain.db`；旧双库
> （`projects/<pid>/memory/index.db` + 全局库）与旧 L1 代码**整段删除**，仓库内不存在
> 双方案分支或兼容开关。历史设计过程见 `doc/plans/ltm-brain-design.md`。

## 1. 总览:两条正交的轴

| 维度 | memory 轴 | skill 轴 |
|---|---|---|
| 内容 | 事实 atoms(L1)+ 场景块(L2)+ 用户画像(L3) | 知识型技能(playbook)+ 可重放宏(substeps) |
| 存储 | `~/.omniagent/memory/brain.db`(L1 单库) + `projects/<pid>/memory/scene_blocks/` + `scene_index.json`(L2) + `~/.omniagent/memory/user_profile.md`(L3) | `projects/<pid>/skills/` + `~/.omniagent/skills/` |
| 生产者 | LLM 提炼(价值判 + 一致性四态) / 场景整理 agent / 巩固周期(去重·抽象·画像重写) | 人工维护 + 可选自动宏提取(`skill.auto_distill`,默认关) |
| 消费者 | 注入(§4 注入面)+ `read_scene` 按需 + agent 自管工具 | 目录 top-3 常驻摘要 + `load_skill`/`search_skill`/`replay_skill` 按需 |

硬约束:**索引内容、存储位置、消费路径三不混**——skill 文件不含纯事实陈述、不进记忆注入块;memory 条目不进技能回放器。

## 2. memory 轴(L0 → L3)

### 2.1 L0 会话

会话原文 = `projects/<pid>/s_*.jsonl`(`TaskStore` 维护),是提炼的唯一原料来源。
`memory_brain.capture` 挂在 `ProjectStore.append_message` 尾部,写进程内 buffer;
任务收尾(`chat_runtime` run_task 之后)`flush_brain` 取走 buffer、提炼、落库,并触发 L2 维护
与巩固周期。任何失败静默降级,绝不阻断主流程。

### 2.2 L1 原子记忆(`brain.db` 单库)

- **存储**:`~/.omniagent/memory/brain.db`(SQLite;`atoms` + `atoms_fts`(FTS5,**simple** 分词,
  缺扩展时进程级降级 `trigram`)+ `idx_atoms_state` + `meta` 水位表)。
  **project 不再是分区键**,只作 `origin` 溯源标签;人格维度是 `persona` 列
  (`''` = 中性/共享,按激活角色卡过滤)。

| 列 | 语义 |
|---|---|
| `kind` | fact / preference / rule / episode —— 决定衰减速率 |
| `importance` | 值不值得长期保留(LLM 写入时打分,基本不变) |
| `salience` | 此刻多"热"(公式重算) |
| `access_count` / `last_access` | 召回强化簿记 |
| `state` | active / dormant / archived / tombstone |
| `superseded_by` | 被更正时指向新条(审计链) |

- **写链**:`flush_brain` → 会话要点 digest(LLM 提炼,**宁缺毋滥**,单次 ≤5 条) →
  **第一段判**(价值/一次性过滤 + `kind` + `importance`) → **第二段判**(一致性四态
  `novel / duplicate / merge / conflict`;候选 = 各自 top-3 的并集,一次批量 LLM) → 落库 + FTS 同步。
  LLM 失败一律保守保留(宁重复不丢失);`duplicate` 只强化旧条,`conflict` 旧条 tombstone 并入库新条。
- **读链**:`search_brain(query, persona)`——`_match_expr` 切词(simple 模式按 2 字滑窗短语 OR,
  trigram 模式 3-gram OR + 2 字词 LIKE 兜底)→ `state IN ('active')` → 融合排序:
  ```
  relevance = 按本次候选集排名归一(首位 1.0、末位 0.5)
  salience  = importance × exp(-days / (S(kind) × (1+ln(1+access))))
  recency   = exp(-days / 180)
  final     = 0.55*relevance + 0.30*salience + 0.15*recency
  ```
  `final ≥ inject_floor(0.30)` 才进注入面,`top_k = 8`;**没有 `_INJECT_TRIGGER=20` 冷启动门槛**
  (按"有没有命中"决定,不按"库够不够大")。命中即强化(`access_count+1`、`last_access=now`)。
  `inject_text_brain` 以 `<relevant-memories>` 包裹,携带"仅作参考,不代表当前任务进程"免责声明。
- **遗忘(降级不是删除)**:`active → dormant`(salience 低 + 零访问 + 龄 >30d)→
  `archived`(更低 + 龄 >90d)→ `tombstone`(被取代 / 人工遗忘,保留内容与原因可审计)。
  降级只在巩固期执行,且 `forgetting_enabled` 门控(缺省 dry-run 只统计)。
- **agent 自管工具**:`memory_remember / memory_recall / memory_update / memory_forget /
  memory_overview`——agent 只有行为权,落盘仍由内核代理(S1 红线)。
- **检索升级路径(暂缓,触发前不动工)**:当前是词法单通道;单库 atoms >100 或实测同义漏召回
  时再评估——本地 embedding 向量臂 + FTS/向量 RRF 融合 + 条数/字符/超时三预算。
  硬约束:个人记忆不出机(只允许本地 embedding,禁云 API);检索只做候选生成
  (排序是机制不是语义判断);任一环节失败逐级降级,注入不断。

### 2.3 L2 场景块(`scene_executor.py`,多文件主题块)

- **存储**:`projects/<pid>/memory/scene_blocks/*.md`——按主题组织(如
  `windows-环境事实.md`),每块 = `META 区(created/updated/summary/heat) + 正文`,
  ≤1500 字符(prompt 约束)/4000 字符(工程硬上限);`scene_index.json` 是 META 的
  **投影缓存**,维护后全量扫目录重建(唯一写入口在工程侧),并额外承载 L2 水位
  `last_maintain_ts`(**不依赖任何记忆库**)。
- **触发**:`scene_executor.maybe_maintain`(由 `flush_brain` 收尾调用)——gate
  `memory.enabled`、900s 水位(空维护不推进)、证据 = 会话要点 + 世界状态;
  机制全部在 `maintain_scenes`。
- **维护机制**(TAM scene-extractor 移植):带工具的 **agent 循环**
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
- **注入形态(M-1)**:导航段只给**文件名清单**,零摘要、零热度——摘要内容改由 L1 检索
  按需进面,避免无关场景摘要常驻污染对话;全文只经 `read_scene` 工具读取。
- **带外信号**:整理 agent 输出 `[PERSONA_UPDATE_REQUEST]` → 写入 brain `meta`
  (`persona_update_request`)→ 下次巩固**强制**画像重写(绕过条数节流),reason 并入证据。

### 2.4 L3 用户画像

- **存储**:`~/.omniagent/memory/user_profile.md`(全局唯一一份,不按 persona 拆文件)。
- **蒸馏**(`memory_brain.maintain_profile`,巩固周期第 7 步):触发 = 上次水位后新增
  active atoms ≥ `knowledge.profile.trigger_every_n`(20)**或** L2 带外信号强制;
  LLM 增量重写(保留仍成立内容,矛盾以新事实为准,assistant 的应对策略/拒绝规则/边界设定
  一律不入画像);上一版自动备份 `.bak`;watermark 落 brain `meta.last_profile_ts`;
  gate `knowledge.profile.auto_maintain`。LLM 失败不动现文件。
- **人工通道**:`PUT /api/runtime/profile`(profile_api)——与自动维护共存
  (重写以"保留仍成立内容"为约束)。

### 2.5 巩固周期(consolidation,"睡觉时整理")

`flush_brain` 收尾后调 `maybe_consolidate`:后台线程、不阻塞收尾、派发闸防重复。

- **触发**:`flush_count ≥ consolidation_every`(缺省 20)或距上次巩固 >1 天;空库不跑。
- **步骤**(全部有界,异常静默):整库备份到 `memory/archive/` → 有界去重 + 矛盾检测
  (LLM 四态)→ `episode ≥3 同主题` 抽象为 1 条 `rule` → 全部 active 重算 salience →
  状态降级(`forgetting_enabled` 门控)→ **画像重写(§2.4)** → 统计落 `meta.last_consolidation_stats`。
- **意义**:库不靠条数增长,靠抽象层级上升(episode → rule → persona)。

### 2.6 写入通道红线

```
agent(大脑)──不能──→ 直接读写 ~/.omniagent/memory/**、projects/<pid>/memory/scene_blocks/**(S1 围栏拒绝)
agent(大脑)──通过──→ 任务执行 → 轨迹落盘 → 蒸馏/整理 → 内核直写(唯一自动入口)
agent(大脑)──通过──→ memory_remember/recall/update/forget 工具 → 内核代理落盘(brain.db)
用户(人工)──通过──→ profile_api / 调试面板删除 / 直接编辑文件(角色卡、AGENTS.md)
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
| 1 | `<scene-navigation>` 场景**文件名清单** + `read_scene` 指引 | system prompt 稳定段 | run 开始读一次、run 内锁定 | `runtime.knowledge.memory.enabled` |
| 2 | `<user-persona>` 用户画像全文 | system prompt 稳定段 | 同上 | `runtime.knowledge.profile.enabled` |
| 3 | `<relevant-memories>` L1 检索记忆(top_k=8,floor=0.30) | 起始 items 一条 user 消息(历史后、技能目录后、任务输入前) | **run 开始注入一次**,后续块自然继承 | `memory.enabled` |
| 4 | `read_scene(scene_name)` 读单块全文 | 工具调用结果 | 模型按需 | 恒可用(纯读) |
| 5 | agent 自管工具(`memory_recall` 等) | 工具调用结果 | 模型按需 | `memory.enabled` |

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
   (供 read_scene 工具与整理 agent);注入路径只准用文件名清单——全文进注入面即违规。
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
| `runtime.knowledge.memory.enabled` | False | 记忆层总 gate(L1 注入 + L2 维护 + L3 蒸馏 + 自管工具;设置页「伙伴 → 记忆」) |
| `runtime.knowledge.memory.fts_tokenizer` | trigram | FTS 分词器 `simple`(需扩展) / `trigram`(缺省降级) |
| `runtime.knowledge.memory.fts_ext_dir` | `~/.omniagent/ext/simple` | simple 扩展目录(含 `simple.dll` + `dict/`) |
| `runtime.knowledge.memory.ltm.persona` | `""` | 记忆归属人格(`''` = 中性/共享) |
| `runtime.knowledge.memory.ltm.top_k` | 8 | L1 注入条数上限 |
| `runtime.knowledge.memory.ltm.inject_floor` | 0.30 | 注入分数下限 |
| `runtime.knowledge.memory.ltm.rank_weights` | relevance .55 / salience .30 / recency .15 | 融合排序权重 |
| `runtime.knowledge.memory.ltm.stability_days` | rule 90 / preference 120 / fact 30 / episode 5 | 衰减稳定期基线 |
| `runtime.knowledge.memory.ltm.consolidation_every` | 20 | 多少次 flush 触发一次巩固 |
| `runtime.knowledge.memory.ltm.forgetting_enabled` | False | 是否真的执行降级(dry-run 只统计) |
| `runtime.knowledge.memory.ltm.dormant_threshold` / `archive_threshold` | 0.15 / 0.05 | 降级阈值(需实测标定) |
| `runtime.knowledge.profile.enabled` | True | 画像注入 |
| `knowledge.profile.auto_maintain` | True | 画像自动蒸馏 |
| `knowledge.profile.trigger_every_n` | 20 | 画像蒸馏触发条数 |
| `knowledge.scene.max_blocks` | 15 | L2 容量上限(三级预警) |
| `knowledge.scene.prompt_mode` | personal | L2 提炼 prompt 家族 |
| `runtime.long_task.finish_intent_check` | True | 收尾意图门 |
| `skill.auto_distill` | False | 自动宏提取 |

## 9. 观测与真机验证

- 轨迹:`memory_injected`(placement / store=brain / hits / chars / layers)、
  `finish_intent_check`(verdict/fragments/chars)、`skill_catalog`、`instructions_injected`。
- `memory_flush` debug 事件(L1):`extracted / kept / stored / duplicated / merged /
  conflict / dropped / applied`;同事件附带 L2 统计(`created / updated / deleted /
  renamed / empty_maintenance / reply_preview`)。
- 调试面:`GET /api/debug/memory`——brain.db 单库转储(`atoms` 含 state/salience/
  access_count + `overview`),`POST /api/debug/memory/delete` 为唯一物理删除通道。
- 自检工具:`memory_overview`(总量 / 状态分布 / kind 分布 / 最近条目 / 上次巩固时间)。
- 真机回归:`python scripts/verify_memory_recall.py [--live]`(词法召回断言表,含 2 字词 /
  标点 / 长句 / 空 query);`python scripts/verify_scene_live.py`(S1 维护 agent 循环 /
  S2 容量红色预警真实合并 / S3 意图门历史夹具 / S4 端到端注入链,`--keep` 保留现场)。

## 10. 明确不做(现行约束)

- **厂商格式硬编码**:内核(含一切派发/解析路径)不得写死任何私有调用格式的标记
  或正则——畸形调用的识别只走 LLM 语义判断(意图门)。
- **DSML 类文本解析兜底**:绑定厂商格式,不做;意图门的纠正重试是唯一恢复通道。
- 每步尾部重发记忆的任何变体。
- **旧实现兼容/双方案分支**:架构升级即整段删除旧代码,不保留配置门控的 if/else 双路径。
- rowfs/对象存储双路、存量迁移、heat 工程侧自增(L2 治理机制天然不需要)。
- skill 跨域自动晋升、知识型技能机械转录、词面打分/语义规则化(相关性判断全 LLM)。

## 11. 端到端注入示例(看真实拼出来长啥样)

> 配套 §4 注入面全景:下面用一条假想任务演示「注入」到底把什么文本拼进了发给大模型的
> prompt。所有格式串均来自代码(`instructions.py` 的 `_memory_stable_block` / `_memory_lead_in`、
> `memory_brain.inject_text_brain`),非示意杜撰。
> **脱敏声明:本节示例均为演示数据,不含任何真实用户/项目信息,仅用于说明注入形态。**

### 11.1 一句话说清"注入"是什么

内核在**任务开始那一刻**,把磁盘上沉淀的记忆读出来,拼成几段文本塞进发给大模型的 prompt
(system 段 + 一条起始 user 消息)。模型"看到"这些文本 ≈ 拥有长期记忆。**只注入一次**,
后面每个块靠 `to_input_list()` 自然继承,不会每步重发(§5 不变量 1)。

### 11.2 第 0 步:磁盘上已经沉淀了什么(原料)

- **L1 原子记忆**(`~/.omniagent/memory/brain.db` 单库,SQLite + FTS5):
  ```
  atoms 表(单库,project 只是 origin 标签):
    m_a1 | fact       | importance .8 salience .8 | "adb 在 Windows 上需指定完整路径 C:\...\platform-tools\adb.exe"
    m_a2 | preference | importance .9 salience .7 | "用户偏好先确认方案再动手"
    ...(按"有没有命中"决定是否注入,无池子门槛)
  ```
- **L2 场景块**(`projects/<pid>/memory/scene_blocks/windows-环境事实.md`,注入只放文件名):
  ```
  META: created/updated/summary/heat
  正文: <完整环境事实...>(只经 read_scene 按需读取)
  ```
- **L3 用户画像**(`~/.omniagent/memory/user_profile.md`,全局单份):
  ```
  用户偏好命令行操作;习惯先确认方案再执行;对代码注释风格有特定偏好。
  ```

### 11.3 第 1 步:组装 system prompt(稳定段,run 开始读一次、锁到任务结束)

`_memory_stable_block` 吐出这两段,拼进 system(`instructions.py` 真实格式):

```
<scene-navigation>
以下是可用场景记忆的文件名清单。仅当用户请求与某场景明显相关时，调用 read_scene 工具（scene_name=文件名）读取该场景；不要在无关请求中主动提及场景内容。
- windows-环境事实.md
- 项目构建.md
</scene-navigation>

<user-persona>
用户偏好命令行操作；习惯先确认方案再执行；对代码注释风格有特定偏好。
</user-persona>
```

> 注意:**L2 只放文件名**,摘要与正文都不进常驻段(§5 不变量 2 + M-1 策略)。

### 11.4 第 2 步:起始 user 消息(L1 检索,run 开始注入一次)

`memory_brain.inject_text_brain` 检索 brain.db(融合排序,top_k=8,floor=0.30),
**拼成一条 user 消息**插在历史之后、任务输入之前:

```
<relevant-memories>
以下为检索到的历史记忆,仅作参考,不代表当前任务进程:
- 用户偏好先确认方案再动手
- adb 在 Windows 上需指定完整路径 C:\...\platform-tools\adb.exe
</relevant-memories>
```

### 11.5 第 3 步:模型按需拉 L2 全文(不是注入,是工具调用)

模型看到 11.3 的文件名清单,觉得要用,就调 `read_scene` 工具:

```
模型: read_scene(scene_name="windows-环境事实.md")
→ 工具返回该 .md 的完整正文(由 scene_executor.read_scene_block 读,仅此一个入口)
```

这就是「渐进披露」——文件名常驻、全文按需,避免 prompt 被塞爆。

### 11.6 第 4 步:任务收尾 → 写回(闭环)

任务结束后 `flush_brain` 取走 capture buffer:会话要点经 LLM 提炼 → 两段判
(价值/kind/importance → 一致性四态)→ 落 brain.db;随后
`scene_executor.maybe_maintain` 让场景 agent 循环 UPDATE/MERGE L2 块;
若巩固周期到期(`flush_count ≥ 20` 或距上次 >1 天),后台跑去重/抽象/强度重算/
画像重写。下次任务开始,11.3~11.4 就能读到这些新记忆。

### 11.7 注入顺序速记

- **system prompt** = base system + AGENTS.md(纪律) + character.md(角色卡) +
  `<scene-navigation>`(L2 文件名清单) + `<user-persona>`(L3 画像)
- **起始 items** = 历史 → 技能目录 top-3 → `<relevant-memories>`(L1 检索) → 任务输入
- 子任务(`is_sub`)零记忆注入(`_memory_stable_block` / `_memory_lead_in` 首行 `if is_sub` 即返回)。
