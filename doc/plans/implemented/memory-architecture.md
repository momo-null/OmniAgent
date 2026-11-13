# 记忆层现行架构

> 本文是**记忆层**的唯一事实源:分层机制(L0→L3)、写链总纲(事件 → 助手的取舍 → 记忆)、
> 注入面、不变量、意图门、配置与观测、验证纪律。2026-11-11 按现行实现整体重构一次
> (此前按约定冻结、过程记录只在本地留档);之后回到冻结纪律,增量同样只在本地留档。
> 代码落点:`omni_core/memory_brain.py`(L0 缓冲 + L0 逐字索引 + 观察进料道 + L1 单库 +
> L3 画像 + 巩固周期 + 观察升格 + 语境链 + 叙事索引)、`omni_core/local/task_store.py`(消息落库处同步写 L0 表)、
> `omni_core/local/scene_executor.py`(L2 机制与触发薄壳)、`omni_core/local/localtime.py`(统一本地日折算)、
> `tools/scene_tool.py`(read_scene)、`omni_core/tools/memory_tool.py`(agent 自管工具 +
> `memory_observe` 显式记一笔 + `conversation_search` + 自取配额账本)、
> `omni_core/tools/observation_tool.py`(observation_search,见闻读端)、
> `tools/narrative_tool.py`(narrative_search / narrative_read,叙事检索与读取端)、
> `local/loop/context_signal.py`(run 级结构化场合信号)、
> `local/loop/instructions.py` + `sdk_bridge.py` + `brain/sdk_loop.py`(注入与意图门)、
> `backend/api/routers/helpers.py`(私聊上下文窗口)。
> 真机回归:`python scripts/verify_memory_recall.py`、`python scripts/verify_scene_live.py`、
> `python scripts/verify_memory_effect.py`(效果验证)。
>
> **2026-10-28 架构清理**：L1 已收敛为单库 `~/.omniagent/memory/brain.db`；旧双库
> 与旧 L1 代码整段删除,仓库内不存在双方案分支或兼容开关;需要追溯论证过程查 git 历史。
>
> **skill 轴不在本稿管辖**:见 `doc/plans/implemented/skill-architecture.md`。本稿只讲记忆 L0→L3。

## 1. 总览

| 维度 | 内容 |
|---|---|
| 内容 | 事实 atoms(L1) + 场景块(L2) + 自我画像(L3) + 逐字会话索引(L0) + **观察原文(L0,分道)** + **叙事资产(日记/月记/关系卡,可检索)** + **圈层语境(circle_note)** |
| 存储 | `~/.omniagent/memory/brain.db`(L1 atoms + L0 `l0_messages`/`l0_fts` + `narrative_index`/`narrative_fts` + `circle_note`/`context_buffer`,同库) + `projects/<pid>/s_*.jsonl`(L0 文件真相) + `projects/<pid>/memory/scene_blocks/` + `scene_index.json`(L2) + `~/.omniagent/memory/self_profile.md`(L3) + `~/.omniagent/memory/relations/`(关系卡正文) |
| 生产者 | **助手的取舍**(显式记 `memory_remember` / 记一笔 `memory_observe`)/ 场景整理 agent / 巩固周期(去重·抽象·**观察升格**·画像重写·日记·月记·语境反思) |
| 消费者 | 注入(§4 注入面,**只到索引/规则层**,含 `<current-context>` ≤200 字符)+ 模型自取工具(`memory_recall` L1 / `conversation_search` L0 / `observation_search` 见闻 / `read_scene` L2 / `narrative_search`+`narrative_read` 叙事) |

**写链总纲(2026-11-10 定稿)**:
**事件 → 助手的取舍 → 记忆**。

```
世界层:事件源(life 流水、群聊原文、任务产出、主人的话)→ 只进 L0/事件记录,只增不改
内在层:助手的取舍(记一笔 memory_observe / 显式记 memory_remember / 巩固期内化 / 不记)→ 观察、日记、关系卡、原子
表达层:助手说话 = 内在层被场合唤醒的部分(出口按表达形态治理)
```

- **收尾零提炼(严格 L1)**:`flush_brain` 不再自动蒸馏原子——任务收尾只做语境链、
  关系卡(L0 直驱)、L2 维护与巩固调度。L1 只经三条「助手的动作」路径生长(§2.2)。
- **世界与内在分层**:外部事件(群聊、life 产物、检索结果)一律先落 L0/观察层;
  成为「助手的记忆」必须经过一次助手的动作(当场记、或巩固期整理时被内化)。
- **工程侧不做准入评分**:记不记是助手的判断;工程只提供动作、出处约束(H-7)与
  一个结构信号(`source_external`,见 §2.1)——没有白名单、没有可信度评分。
- chat 与 task 进料道只差**工程节流**(chat 高频,`enqueue_throttled` 合并入队),
  无记忆语义分支;`allow_atoms` 开关已整删(2026-11-10,不留别名,见 §10)。

**术语约定(防混淆)**:**日记 / 月记 / 关系卡 = Narrative
自传体叙事**,是记忆内容、模型可检索、是拟人载体;**观测记录 / 日志
(`memory_recall-YYYY-MM.jsonl`)= 机器埋点**,是量尺,不进注入面、不给 agent 查询工具、
只由离线脚本与本机 Debug 监控面板读来算指标 / 展示。两者不互串、不互称。

**与 skill 轴的边界**:索引内容、存储位置、消费路径三不混。本稿不描述 skill 的任何机制。

## 2. memory 轴(L0 → L3)

### 2.1 L0 会话与观察

会话原文 = `projects/<pid>/s_*.jsonl`(`TaskStore` 维护),是日记与整理的素材真相。
`memory_brain.capture` 挂在 `ProjectStore.append_message` 尾部写进程内 buffer(供语境链等收尾消费);
任务收尾 `flush_brain` 触发 L2 维护与巩固周期。任何失败静默降级,绝不阻断主流程。

**逐字检索副本(P2,2026-11-02)**:`append_message` 同时往 `brain.db` 的
`l0_messages` + `l0_fts` 同步插一行。列:`record_id / context_key / session_id /
kind(chat|task) / role / speaker / text / recorded_at / source_kind / provenance /
source_external`——`kind` 区分通道消息与任务输入;`record_id` 由 (会话, 时间, 角色, 文本) 派生,
回填与实时写入天然幂等。jsonl 仍是文件真相,表只是可重建的检索副本。
消费端 = `conversation_search(keyword, scope, days, limit)`(§4)与 `msg_group_history(keyword=)`。

- **进料道分列(2026-11-06,§4.6)**:`l0_messages.source_kind`(chat / observation / task)
  与 `provenance`(结构化来源指针,不含真实账号/URL 明文)。**唯一观察入口
  `memory_brain.capture_observation()`**:主动阅读 / 检索结果 / 工具产出都走它落
  `source_kind=observation`,带 `provenance` 与 `observed_at`,不伪装成聊天原话(INV-13);
  `conversation_search` 只查 `source_kind=chat`。**provenance 必填(H-7):缺出处不入库**。
- **助手的记一笔 = `memory_observe(text, source)` 工具(2026-11-10)**:观察进料道的
  agent 写入面(agent→工具→内核代理,红线形态,§2.6)——浏览/采集时逐条记见闻,
  出处必填;已加入 explicit 通道白名单(QQ 群/私聊轮在场内可用)。采集器走
  `observation_collector` 能力(`plugins/collector`),同入口。读端 = `observation_search`(§4)。
- **来源可达性是结构信号(H-9,2026-11-10)**:`l0_messages.source_external`(布尔列,
  schema `h9`)由边界层在 run 起始归一后盖章,随观察行落库;调用方可显式传值,
  未盖章时内核只读 `context_signal.boundary()` 的同一信号,**不解析任何场合键取值或
  前缀**(INV-4)。外部可达来源(QQ 群等)记下的见闻只留叙事层,永不升格为世界事实
  (投毒锁,§2.2)。
- **来源标注**:外部入站消息带 `「名片(uid)」: `前缀(通道层注入),L0 解析成 `speaker` 列
  ——日记与关系卡据此把经历记到正确的对象头上(2026-11-01 起,QQ 渠道先行)。

### 2.2 L1 原子记忆(`brain.db` 单库)

- **存储**:`~/.omniagent/memory/brain.db`(SQLite;`atoms` + `atoms_fts`(FTS5,**simple** 分词,
  缺扩展时进程级降级 `trigram`)+ `idx_atoms_state` + `meta` 水位表)。
  **project 不是分区键**,只作 `origin` 溯源标签。`persona` 写入冻结恒空(列保留保 schema 稳定,
  形参链已全部移除);`owner` 列已 DROP(多租户遗产)。

| 列 | 语义 |
|---|---|
| `kind` | fact / preference / rule / episode —— 语义标签 |
| `importance` | 值不值得长期保留(显式写入缺省 0.8,升格由 LLM 打分) |
| `salience` | **恒等于 `importance`**(P7 起不再衰减;留列只为审计) |
| `access_count` / `last_access` | 召回强化簿记 |
| `state` | `active` / `tombstone` —— 只有这两态(P7 删掉 dormant/archived) |
| `superseded_by` | 被更正时指向新条(审计链) |
| `peer` | 关系对象标注(「名片(uid)」解析;空 = 无对象/自己) |
| `context_key` | **一等必填场合键**(H-7):这条记忆「在哪儿发生的」——只用于场合/共同背景加权,不用于过滤(INV-3,**禁止**写成 SQL WHERE) |
| `source_kind` | dialogue(缺省;含 `memory_remember` 显式写入)/ observation(巩固期升格)——知识来源标注 |
| `observed_at` / `provenance` / `review_after` | 出处与时效标注(只作追溯、不判真假,INV-14) |

- **L1 的三条生长路径(2026-11-10,严格 L1)**——全部经过「助手的动作」:
  1. **显式记**:`memory_remember(text, kind)` 工具 → `remember()` 直写入库
     (importance 缺省 0.8,精调交巩固期);
  2. **观察升格**:巩固期 `promote_observations`(§2.5 步骤 7.5)把助手记进观察层的见闻
     提炼成结论短句(`_PROMOTE_SYSTEM`,≤60 字,提不出稳定结论按空串跳过——提炼器
     不是守门人),经四态一致性(`_CONSIST_SYSTEM`,与既有 merge/conflict 同一套)入
     事实层,写齐 `source_kind='observation' / observed_at / provenance / context_key`
     追溯标注;幂等键 = 观察 record_id 存 `atoms.source_task`,重跑不重复;
     **只取 `source_external = 0` 的观察行**——外部可达来源记的见闻永不升格
     (投毒锁,INV-4/§9.6);无每日上限——量由四态去重与召回排序管;
  3. **episode → rule 抽象**:巩固期对同 peer/同主题 ≥3 条 episode 抽象成 1 条 rule。
- **读链**:`search_brain(query, peer, k)`——`_match_expr` 切词(simple 模式按 2 字滑窗短语 OR,
  trigram 模式 3-gram OR + 2 字词 LIKE 兜底)→ `state='active'` → 排序(P0 + P7):
  ```
  rel     = bm25_rank_to_score(rank) = (-rank)/(1+(-rank))   # 绝对分,不在候选集内排名归一
  final   = 0.55*rel + 0.30*importance + 0.15*exp(-days/180)
  peer 命中 → ×1.25(加权不过滤);  在 _recent_ring 里 → ×0.85(只降权不排除)
  当前场合命中(atom.context_key == 本轮 context_key) → ×(1+w_ctx 0.25)   # M2,加权不过滤,INV-3
  同场合 + 圈层共同话题词面相交 → ×(1+w_bg 0.10)                # R8 共同背景,同样不过滤
  final < inject_floor(0.30) → 丢
  ```
  控量不靠门槛靠**四道闸**:绝对分、**glue 门槛**(命中零有效词即丢;query ≥3 词时只盖住
  一个高频词 `df>3` 也丢——单命中低频/专名是真主题,留)、**去冗余**(`(kind, peer,
  content[:12])` 同组只留最高分,保证 k 条是 k 个方面)、**轮换环**(`deque(maxlen=64)`)。
  `top_k = 5`。命中即强化(`access_count+1`、`last_access=now`、入环)。返回的 `content`
  取 `a.content`(**现行文本**),索引行只用于定位与 bm25(§5 不变量 5)。
  > **检索结果永不自动注入(P1,方案 C)**:`inject_text_brain` 只有 `memory_recall` 工具
  > 与调试面板会调——run 起始无 lead-in 注入(§5 不变量 4)。
- **peer 维度(2026-11-01)**:来源标注解析「名片(uid)」写入 `peer`(助手自己的话不打);
  `search_brain(..., peer=)` 命中加权 ×1.25(**不过滤**);巩固期 episode→rule 抽象按
  `peer|主题前缀` 分组、peer 继承进 rule——关系事实的延迟抽象路径,原料含日记(§2.7)。
- **遗忘机制已删除(P7,2026-11-02)**:dormant/archived 状态机、salience 衰减整段删除。
  保留的只有 `tombstone`——被取代/人工遗忘的审计链。「淘汰 = 移出常驻,不是删除」。
  人工物理删除只有一个通道:`POST /api/debug/memory/delete`(主人调试面,不进 agent 写入路径)。
- **agent 自管工具**:`memory_remember / memory_observe / memory_recall / memory_update /
  memory_forget / memory_overview`——agent 只有行为权,落盘由内核代理(S1 红线)。
  读类工具(memory_recall / conversation_search / msg_group_history / read_scene /
  narrative_search / narrative_read)**共用一个自取账本**:账本只计数不硬拦,≤3 次/run
  是《记忆使用规则》对模型的约束 + 观测口径(§9);`memory_observe` 是写动作,不占读配额。
- **检索升级路径(暂缓)**:单库 atoms >100 或实测同义漏召回时再评估本地 embedding 臂;
  硬约束:个人记忆不出机、检索只做候选生成、失败逐级降级工具照常返回。

### 2.3 L2 场景块(`scene_executor.py`,多文件主题块)

- **存储**:`projects/<pid>/memory/scene_blocks/*.md`——按主题组织,每块 =
  `META 区(created/updated/summary/heat) + 正文`,≤1500 字符(软)/4000 字符(硬,
  write 与 edit 两路同守);**读取端不截断**。`scene_index.json` 是 META 投影缓存,
  承载水位:`last_maintain_ts`(时间节流)与 `last_l2_atom_ts`(进料游标)。
- **触发与进料**:`scene_executor.maybe_maintain`(由 `flush_brain` 收尾调用)——gate
  `memory.enabled`、900s 节流、**进料 = 游标之后的 active L1 原子增量**(`atoms_since`,
  一次 ≤40 条);零增量整轮不调 LLM 直接跳过。**一池增量、不分组(INV-9)**:任何项目的
  进料都取同一份一池原子增量;`origin`/场合标签只作渲染元信息。
  **严格 L1 下的进料节奏(2026-11-10)**:收尾不再产原子,L2 进料由「助手的动作」驱动——
  显式记与巩固升格的原子是主要增量源;无增量轮次天然跳过,这是场景块不无限长的主闸。
- **游标为复合键 `updated|record_id`(P1)**;**游标不推进的五种情形**(§5.1):①零增量
  ②`stats.error` ③无可用 LLM ④整理前后无任务块(`empty_extraction`)⑤整理 agent
  未产生任何动作(`agent_ops==0`)——任一命中都把本批留到下次。
- **维护机制**(TAM scene-extractor 移植):带工具的 **agent 循环**(工具白名单硬限
  `scene_read/scene_write/scene_edit`,迭代 ≤32、墙钟 ≤300s、请求 `max_tokens` 下限 8192)
  ——UPDATE 首选 > MERGE(heat = 各块之和 + 1)> CREATE(前必须 scene_read ≥2 个最相似块,
  每批最多新建 1 块)。
- **容量治理**:`knowledge.scene.max_blocks`(缺省 15)三级预警(红=必须 MERGE / 橙=只能 UPDATE /
  黄=优先 UPDATE/MERGE)。
- **工程侧流水**:软删清理 → 文件名归一化 → 索引重建;维护前整目录快照,异常整目录还原。
- **注入形态(P6)**:导航段渲染 **`文件名 + 热度/日期 · 更新时间 + summary`** 三行式;
  全文只经 `read_scene` 读取。**分族配额**:稳定段导航只列**主题块**(heat 降序,≤10)——
  日记/月记固定配额已取消,叙事条目由 `narrative_search` 按需检索(INV-11/N-3)。
- **带外信号**:整理 agent 输出 `[PERSONA_UPDATE_REQUEST]` → brain `meta` → 下次巩固
  **强制**画像重写。
- **日记主题族(`diary-<日期>.md`)**:巩固期**内核直写**(不走维护 agent,对其只读不可见)
  ——第一人称「助手的一天」;独立配额不占 `max_blocks`。**分层口径(P6)**:日记属
  **L0-叙事**(日粒度原料,全量保留、可回读、本体不进注入面),L2 只索引它的 summary;
  **正文长度 ≤1800 字(2026-11-10 起,原 900)**——日记不进注入面,无预算约束,
  素材里有 [生活]/[见闻] 全天段(§2.7)。summary 取正文首个句末标点结尾的句子(≤60 字)。

### 2.4 L3 自我画像(2026-11-11 三分法)

- **三分法(2026-11-11 用户拍板)**:`user_profile.md` / `<user-persona>` / 旧 L3 语义整体退役。
  - 她(主体):`character.md` 静态底色 + **`self_profile.md`** 演化自我,注入 `<self-persona>`;
  - 人(关系对象,含主人):`relations/<uid>.md`(主人卡带【稳定背景】区),L0 直驱 + narrative_search;
  - 世界(事实):L1/L0 atoms,recall 按需。
  - `_PROFILE_SYSTEM` 只吃关于她自己的事实,剔除对话者(→关系卡)/主人环境(→主人卡)/策略规则/
    归属不明条目(宁可漏记不可错记);主人卡正文上限 1400、普通卡 600,LLM 丢背景时代码补写回。
- **存储**:`~/.omniagent/memory/self_profile.md`(全局唯一一份)。
- **蒸馏**(`maintain_profile`,巩固周期第 7 步):触发 = 上次水位后新增 active atoms ≥
  `knowledge.profile.trigger_every_n`(20)**或** L2 带外信号强制;LLM 增量重写
  (assistant 的应对策略/拒绝规则/边界设定一律不入画像);上一版自动备份 `.bak`;
  watermark 落 brain `meta.last_profile_ts`;gate `knowledge.profile.auto_maintain`。
  **严格 L1 下的节奏**:画像蒸馏跟随「助手的动作」产生的原子增量——频率自然下降,
  机制不变。LLM 失败不动现文件。
- **人工通道**:`PUT /api/runtime/profile`(profile_api)——与自动维护共存。

### 2.5 巩固周期(consolidation,"睡觉时整理")

`flush_brain` 收尾调 `maybe_consolidate`:后台线程、不阻塞收尾、派发闸防重复。

- **触发**:`flush_count ≥ consolidation_every`(缺省 20)或距上次巩固 >1 天;空库不跑。
- **步骤**(全部有界,异常静默):整库备份到 `memory/archive/` →
  **1) 有界去重 + 矛盾检测**(LLM 四态;候选 = FTS 全文近邻 top5,每轮判定上限 20 条)→
  **2) `episode ≥3 同主题`抽象为 1 条 `rule`**(peer 分组)→
  **7) 画像重写**(§2.4)→
  **7.5) 观察升格(`promote_observations`,2026-11-10 落地,§2.2 路径 2)**:
  未升格且 `source_external = 0` 的观察行 → 批量提炼为结论短句 → 四态一致性 → 落库;
  统计落 `obs_promoted / obs_stored / obs_merged / obs_conflict / obs_duplicate`;
  在日记步之前跑——升格产物当轮即可被日记素材的「原子重访」引用 →
  **8) 日记步(`maintain_diaries`,补齐模式)**:写**昨日**(D+1)第一人称日记,回看补齐
  最近 7 天(幂等 + 白卷跳过,超窗放弃;§2.7)→ **月记步**(≥3 篇才压)→
  **9.5) 语境链冷启动**:还没有 `circle_note` 的老场合各跑一次首版 reflection
  (全局 ≤5 次/周期)→ 统计落 `meta.last_consolidation_stats`。
  **关系卡已移出巩固周期(2026-11-10)**:改为 `_flush_tail` 按 L0 直驱
  (`maybe_maintain_relations`:带结构化 speaker 的 user 行跨场合聚合,新增 ≥12 行或
  距上次 ≥600s 触发;游标落 meta)——不产原子的轮次同样能更新对人的印象。
- **批量 LLM 预算托底(`_batch_llm_cfg`,2026-11-10)**:memory_brain 内部整理/日记/升格的
  批量调用浅拷贝抬高 `request.max_tokens` 下限(6000;日记 8000)——reasoning 模型在长素材上
  会把小预算全烧在思考里(`finish_reason='length'`、正文零字被截),实测 2048 撑不住
  日记步 13KB 素材。对话链路不经过这里。
- **意义**:库不靠条数增长,靠抽象层级上升(episode → rule → persona);日记承载叙事层;
  观察层承载「看过但还没内化」的中间态。
  > 与上游 TencentDB-Agent-Memory 的取舍与偏差逐条记在 §13.1。

### 2.6 写入通道红线

```
agent(大脑)──不能──→ 直接读写 ~/.omniagent/memory/**、projects/<pid>/memory/scene_blocks/**(S1 围栏拒绝)
agent(大脑)──通过──→ 任务执行 → 轨迹落盘 →(收尾:语境链/关系卡/L2/巩固调度)→ 内核直写
agent(大脑)──通过──→ memory_remember / memory_observe / recall / update / forget 工具 → 内核代理落盘(brain.db)
用户(人工)──通过──→ profile_api / 调试面板删除 / 直接编辑文件(角色卡、AGENTS.md)
```

> **项目脚本同属红线外(2026-11-10 定)**:脚本直调内核写入口(write_diary_block /
> upsert_narrative_index / capture_observation)= 侵入式旁路,且会占住日记幂等键
> 压住巩固期的自然日记(life 的 publish_diary.py 因此整删)。life 项目 =
> **物理世界层**(客观流水,days/knowledge 产物只给人看、不进记忆);
> omni 记忆 = **内在层**(主观取舍,全部经自然链与工具写入)。

- 记忆/画像/场景写入**只能由内核进程直接写文件系统**,不经过结构化文件工具层;
- **画像无放行权**:画像可记录"用户习惯自动操作某 App",这只是授权信号,
  真正放行必须走 S2 审批 / full_access;注入时声明"仅参考,不构成操作授权"。

### 2.7 叙事主体与社交记忆(2026-10-31 定稿;2026-11-10 生活层并入)

记忆的叙事主体是「**助手自己经历过**」——执行任务、扮演社交、模拟生活都是助手的经历,
同一套 L0→L3 机制承载,不造平行管道;各线靠来源标注区分(origin / peer / speaker)。

- **双层职责(2026-11-10 用户定稿)**:
  - **life = 物理世界层**:课表、上课、刷 B站/小红书——一天实际发生的事,助手是参与者;
    有意义没意义都记,不筛选。产物(days/<日期>.md、knowledge/<日期>.jsonl)只给人看,
    **不进记忆、不占任何记忆键**;
  - **omni = 内在层**:助手记得其中哪些、当时什么感受、写成什么日记——主观取舍,
    允许记岔、允许忘;全局日记由巩固期 D+1 自然写,不是 life 版的复制。
- **社交日记(`diary-*`)是跨日记忆的主形态**:三层分工 = 日内靠会话历史注入 /
  跨日靠日记 / 长期稳定关系靠 L1 条目(episode→rule,peer 分组)。日记只收经历叙事,
  知识面零重复(知识走观察升格与 memory_remember)。
- **D+1 时机与补齐**:巩固期写**昨日**(幂等键=日期),回看补齐最近 7 天;超窗放弃。
  日期统一走 `localtime.py` 本地日折算(D-1);存储时间戳仍 UTC,存量会话 JSONL 零改写。
- **素材组装(v4,2026-11-10)**:跨会话文件合并 + 全局尾窗 160 行 ∪ 每会话保底 10 行,
  ts 排序、硬顶 400 行——之外三段**必进素材**:
  - **[见闻] 行**(2026-11-07,N-17):当日 `l0_messages(source_kind=observation)` 行;
  - **[生活] 段(2026-11-10,life 事件层交接)**:各项目 `days/<day>.md`(客观流水)整段
    并入,不进尾窗裁剪——「模拟生活带上」的入料口;life 产物照旧不落库、不占日记键;
  - **[回看] 行**(`review_after` 到期的观察,H-5)。
  写不写归意义判:纯寒暄不写、多对象/场景各有笔墨、白卷是正确答案。
  **纯工具型任务一句话带过**;[生活]/[见闻] 与对话往来**同等笔墨**。
- **经历质地(P6-1,2026-11-07)**:挑 1-3 处写「现场感」+ 至多 1-2 处带说话人的短原话引用;
  `narrative_index.atmosphere` 词面列(同一次调用顺带产出)使触发物/心境可检索。
- **身份锚(登记制,core 零写死)**:`note_self_names/self_names` 注册表,渠道上报自己
  高置信身份;日记 prompt 动态注入锚句 + 记录结构规则——不被自己名字带偏。
- **月记(`diary-month-<月份>.md`)**:只压已结束月份、≥3 篇才压。
- **按日统一(H-4,2026-11-06)**:日记/月记落全局 `~/.omniagent/memory/diary/`,
  素材跨项目聚合、一次 LLM 一篇;`narrative_index.doc_id` 裸文件名,`project_id` 不作键。
- **META 可选键**:`topics / context_keys / peers / time_from / time_to / atmosphere / we`
  (P6-2 参与人标注,取值来自配置,内核零词表)。正文不进索引(INV-12)——
  `search_text` 只存 `summary + topics + atmosphere`。
- **消费(R6)**:稳定段不固定列日记/月记——被问「上次/那天/某个人」时模型调
  `narrative_search` → `narrative_read`;T1 判定层零消费;写入侧自消费
  (旧日记喂新日记与关系条目抽象)。

### 2.8 关系卡("记得某个人",2026-11-01;2026-11-10 改 L0 直驱)

- **定位(2026-11-06 起)**:关系卡是**可检索的自传体资产**——通道自动注入已物理删除,
  由模型调 `narrative_search(peer=名片(uid))` → `narrative_read` 按需自取;
  外部来源读卡只返回 summary(N-10,INV-8)。
- **存储**:`~/.omniagent/memory/relations/<uid>.md`——全局一份、按人存
  (人是跨场景的,按群/项目拆会把"对他的印象"分裂)。
- **结构**:META(`name`/`uid`/`updated`,可选 `topics:`/`we:`)+ 正文三行:
  **最近交手 / 他对助手的态度 / 助手对他的态度**。
- **维护(L0 直驱,2026-11-10 起)**:`_flush_tail` 调 `maybe_maintain_relations`——
  素材 = `l0_messages` 带结构化 speaker 的 user 行(跨场合合并,7 天窗、尾部 400 行);
  节流 = 新增 ≥12 行或距上次 ≥600s(游标/时间戳落 meta);每人 ≥3 条往来才写卡,
  每轮最多 5 人;**不产原子的轮次同样能更新**(空 atoms 库不挡路)——
  与巩固周期解耦,印象卡随对话持续生长。
- **与 L1 的分工**:关系卡 = "这个人的三行印象"(按 uid 索引、随时可读);L1 的 `peer` 条目
  = "关于他的一条事实"(参与检索与抽象)。同源(都来自来源标注),不互相替代。
- **规模治理**:无上限、无 TTL(一人一文件,永久保留);2026-11-04 决定暂不动,
  未到需要治理的量;加 TTL/淘汰会撞 P7「不做衰减」。
- **下游用法**:印象只改"距离",具体怎么表达由角色卡决定——换人格只改角色卡,
  通道与记忆层零改动。

### 2.9 圈层语境(`circle_note`,2026-11-06 定稿)

「像人在不同圈子里说话」的载体:**语境预期**(这个场合大致聊什么、说话什么分寸、共同话题
词面),不是可引用事实——单独表、带来源标注、不参与四态、不作为事实引用、不进对外发话
(INV-5/INV-10)。

- **原料与累积**:`flush_brain` 收尾把本批消息按场合累积**通用信号**进 `context_buffer`
  ——话题词面(2 字滑窗高频,零词表零 LLM)、条数、来源构成、称呼形态比例;不写他人原话。
- **reflection**:条数(12)或间隔(600s)触发,一次 `chat_json` 蒸馏成一条 `circle_note`
  (summary ≤160 字 / topics 2-6 / tone ≤40 字);重写不追加,失败静默保留旧版;
  每场合每巩固周期 ≤1 次、全局 ≤5 次/周期;巩固期对没有 note 的老场合冷启动(9.5 步)。
- **消费**:只影响 `<current-context>` 的话题/语气字段与检索排序(共同背景 `w_bg` 加权);
  永不进 `atoms`、不进 `memory_recall` 返回行、不进对外发话。
- **共同背景(R8)**:同 `context_key` 标注的事实 + 圈层话题词面相交 → ×(1+w_bg 0.10)
  ——「不同圈子知道不同圈子的事」由**一池 + 标注 + 加权**落地,不新增存储层(INV-3)。
- **网关不承载人格(2026-11-02 收口)**:通道场合自述只交代**处境**与**发送机制**
  (`_SEND_RULE`);身份与代答话术在角色卡。

## 3. 角色卡(character.md)

- **落点**:`~/.omniagent/character.md`(单角色一张,人工单写,无独立接口)。
- **注入**:`_merge_character` 随 system prompt(run 开始读一次);用户当轮明确指令优先。
- 与记忆路由**解耦**:角色卡不硬过滤记忆。
- **职责边界**:只留助手是谁、从哪学、不演、红线;**不加性格结论、行为剧本、
  口头禅模板**——说话方式从经历里长出来,加了就是把助手写死。

## 4. 注入面全景

| # | 内容 | 位置 | 频率 | 开关 |
|---|---|---|---|---|
| 1 | `<memory-usage-rules>` 《记忆使用规则》(必查场景 + 七读工具合计 ≤3 次/run、narrative_* ≤2 次 + 「叙事不得转述到对外消息」) | system prompt 稳定段 | run 开始读一次、run 内锁定 | `runtime.knowledge.memory.enabled` |
| 2 | `<scene-navigation>` L2 主题块索引(文件名 + 热度/日期 + summary ≤120 字) + `read_scene` 指引 | system prompt 稳定段 | 同上 | `runtime.knowledge.memory.enabled` |
| 3 | `<self-persona>` 自我画像全文 | system prompt 稳定段 | 同上 | `runtime.knowledge.profile.enabled` |
| 3.5 | `<current-context>` 场合信号(场合键/进料道/对象/话题词面/语气,≤200 字符) | system prompt 稳定段 | run 开始构造一次 | 随记忆 gate |
| 4 | `read_scene(scene_name)` 读单块全文 | 工具调用结果 | 模型按需 | 恒可用(纯读) |
| 5 | L1 自取 `memory_recall`(k ≤5) | 工具调用结果 | 模型按需(读配额) | `memory.enabled` |
| 6 | L0 逐字自取 `conversation_search` / `msg_group_history(keyword=)` | 工具调用结果 | 模型按需(同一账本) | `memory.enabled` |
| 6.5 | 叙事自取 `narrative_search` / `narrative_read` | 工具调用结果 | 模型按需(narrative_* ≤2 次/run) | `memory.enabled` |
| 7 | **L0 对话窗口**(IM 最近 **12 条** / Web 私聊 **最近 12 轮且 ≤4000 字**) | 起始 items 历史段 | run 开始一次 | 无(通道固定) |

> 第 7 行是 L0 的**消费侧**(`dispatch.py` IM 条数窗 / `helpers.py` 私聊轮窗,窗口外折叠
> 成一行提示)。群上下文块注入已删(M1,F-4:通道只给结构不给文本)。

> **读侧候选包(2026-11-11,不进注入面)**:run 起始按本轮最后一条用户原话生成一份高置信候选
> (L1 / 叙事 / L0 三通道,阈值见 §8);**条数只落观测,不进 system/items**。模型需要时用
> `memory_recall(query="")` 一次取走(独立 1 次/run,不占七工具 ≤3 次深挖配额),取不到返回空。
> `read_bundle.enabled=false` 即停生成;观测拆分见 §9。
> **run 起始无 `<relevant-memories>`**:检索结果永不自动注入(§5 不变量 4);
> `memory_observe` 是写动作,不在注入面——它把见闻送进观察层,消费仍走检索工具。
> **窗口外的历史不是死区**:逐字原文 `conversation_search` 现查,摘要走 `memory_recall`。

另有常驻 system 的非记忆注入(不在本文管辖):AGENTS.md 纪律块、角色卡、运行时上下文。
稳定段在 `instructions._memory_stable_block` 组装、`sdk_bridge` 于指纹计算之前并入 system
(O5+ 不变式:Model-visible ⟺ logged)。子任务(is_sub)零记忆注入。
注入行为落轨迹 `memory_injected`——placement 只有 `system`;每个 run 收尾另落一行
`memory_recall.jsonl`(§9)。

## 5. 不变量(防回退红线)

1. **静态内容禁止每步重发**:尾部注入包装已删除,不得回流。锁:`review_lint` R4。
2. **L2 场景全文只有一个入口**:`read_scene_block` 仅存在于 `scene_executor.py`;
   注入路径只准用**索引条目**——正文进注入面即违规。锁:R4 白名单。
3. **意图门零格式字面量**:未派发调用的判定**全 LLM**,内核不得出现任何厂商私有格式
   标记/正则(R2 同纪律)。
4. **记忆检索结果永不自动注入**:L1 召回 / L0 查询 / 叙事检索只经工具进入上下文;
   通道只注入**结构化场合信号**与 **L2/L3 索引**。`<current-context>` 是工作记忆,
   不构成注入特例。例外清单清零(关系卡自动注入已随 NP-2 物理删除)。
   锁:`memory_injected` 层列表不得含 L1。
5. **一条记忆在 `atoms_fts` 里恒为一行**:`_fts_upsert` 唯一写通道(先删后插);读链一律取
   `a.content`(现行文本),永不取索引旧文。锁:`test_memory_recall` P0.5 三例。
6. **合并只在同一适用域内发生,保不住就不并**:`_CONSIST_SYSTEM` 的 merge 条件 =
   同场景、同约束对象,并完双方条件与禁止项都还在;跨域一律 novel。
   `merged_content` 超 `_MAX_ATOM_CHARS` = **拒并**(升格侧与巩固侧同口径),不做静默截断。
   红线:什么算同域**全交模型**,工程侧只给拒绝线。
   锁:`test_observation_promotion` 超线拒并例 + `test_memory_brain`。
7. **单库单人格(INV-1)**:L1 事实表与 L3 画像全局唯一;`persona` 过滤路径已移除。
8. **场合不是存储分区键(INV-2)**:`context_key` 只出现在信号、索引与标注字段,
   不用于建表、建目录、分库或选择物理路径(`circle_note` 按场合一行是索引键,非物理分区)。
9. **加权不过滤(INV-3)**:场合(`w_ctx`)、共同背景(`w_bg`)、对象(`peer`)三类信号只改
   变排序,不得变成 `WHERE` 硬条件。锁:`test_memory_recall` 加权三例。
10. **事实与语境分离(INV-5)**:`circle_note` 语境摘要单独存放、带来源标注、不参与四态、
    不作为事实引用、不进对外发话。
11. **事件 → 助手的取舍 → 记忆(INV-6,2026-11-10 重写)**:收尾**零自动提炼**——
    L1 只经三条助手的动作路径(memory_remember / 巩固期观察升格 / episode 抽象);
    外部可达来源(`source_external=1`)记下的见闻**永不升格**为世界事实(投毒锁);
    观察原文不直接写 atoms。**禁止以任何形式回流「按来源类型一刀切」的准入开关**
    (`allow_atoms` 及其枚举别名——它按来源封禁而非按内容取舍,已被证伪,§10)。
12. **叙事正文不进注入面(INV-11)**:稳定段与场合自述不得出现日记/月记/关系卡正文;
    正文只经 `narrative_read` 按需读取。锁:`test_memory_alignment` + `test_memory_context_signal`。
13. **叙事索引只存指针(INV-12)**:`narrative_index`/`narrative_fts` 只存摘要/主题/
    场合标注/atmosphere 词面/指针,不存正文;doc_id 裸文件名;LLM 沙箱无该表写权限。
14. **观察与对话分道(INV-13)**:非对话来源一律经 `capture_observation()` 入口、带
    `source_kind`,`conversation_search` 只查 `source_kind=chat`;**出处与时效只作追溯
    (INV-14 / H-5)**:四字段不参与准入/打分,不判真假;`review_after` 到期仅产出
    `[回看]` 素材行;**出处必填(H-7)**:provenance 缺失不入库。

## 6. 收尾意图门(oneshot 误判保护)

背景:畸形调用表现为「文本被当普通输出、调用根本不派发」,而 oneshot 语义会把
纯文本收尾判成功 → 任务静默假成功。根源(2026-10-07 A/B 定案,模型侧格式泄漏)。

- **触发点**:`sdk_loop` 方案 B 分支——纯文本收尾且 `verify_done()` 走
  「无校验条件,信任大脑」路径、即将判成功**之前**;仅 oneshot 成功路径,judge
  未注入/异常/配置关 → fail-open。
- **取向门(2026-11-04)**:chat **不装这道门**——chat 的纯文本收尾就是交付物本身。
  总闸 `runtime.long_task.finish_intent_check` 作用域只有 task/auto(设置页「通用」)。
- **判断方式**:`sdk_bridge._plain_finish_judge` 闭包,`llm_judge.chat_json`
  **片段提取式**问法——找出所有「本应作为工具调用发出」的片段,有片段 → 不判完成。
- **上限**:每 run 最多纠正 2 次,超限按原语义放行。
- **观测**:命中落轨迹 `finish_intent_check`(verdict/fragments/chars)。

## 7. skill 轴(已剥离)

skill 不与记忆混写,全部内容见 `doc/plans/implemented/skill-architecture.md`。
唯一约束是 §1 的**三不混**:skill 产物不进记忆注入块,memory 条目不进技能回放器。

## 8. 配置总表

| 键 | 缺省 | 作用 |
|---|---|---|
| `runtime.knowledge.memory.enabled` | False | 记忆层总 gate(使用规则 + L2 维护 + L3 蒸馏 + 自管工具 + L0 索引/检索;设置页「伙伴 → 记忆」) |
| `runtime.knowledge.memory.fts_tokenizer` | trigram | FTS 分词器 `simple`(需扩展) / `trigram`(缺省降级) |
| `runtime.knowledge.memory.fts_ext_dir` | `~/.omniagent/ext/simple` | simple 扩展目录 |
| `runtime.knowledge.memory.ltm.top_k` | 5 | L1 检索条数上限 |
| `runtime.knowledge.memory.ltm.inject_floor` | 0.30 | 终分下限 |
| `runtime.knowledge.memory.ltm.rank_weights` | relevance .55 / salience .30 / recency .15 | 融合排序权重(salience 项取 importance 值) |
| `runtime.knowledge.memory.ltm.consolidation_every` | 20 | 多少次 flush 触发一次巩固 |
| `runtime.knowledge.profile.enabled` | True | 画像注入 |
| `knowledge.profile.auto_maintain` | True | 画像自动蒸馏 |
| `knowledge.profile.trigger_every_n` | 20 | 画像蒸馏触发条数 |
| `knowledge.scene.max_blocks` | 15 | L2 容量上限(三级预警) |
| `knowledge.scene.prompt_mode` | personal | L2 提炼 prompt 家族 |
| `knowledge.diary.enabled` | True | 社交日记步(巩固期写昨日第一人称日记,§2.7) |
| `knowledge.narrative.external_read_mode` | summary_only | 外部来源读印象卡只给摘要(N-10) |
| `knowledge.narrative.we_memory.enabled` | False | P6-2 共同经历总开关(缺省关) |
| `knowledge.narrative.we_memory.participants` | [] | 共同经历参与人称呼列表(取值来自配置,内核零词表) |
| `knowledge.narrative.we_memory.rhythm` | False | 表达节奏开关(缺省关;关闭时稳定段逐字节不变) |
| `context.w_ctx` | 0.25 | 当前场合标注命中的加权系数(INV-3) |
| `context.w_bg` | 0.10 | 圈层共同背景词面命中的加权系数(R8) |
| `context.context_chars` | 200 | `<current-context>` 字符预算 |
| `circle_note.enabled` | True | 语境链总开关(§2.9) |
| `circle_note.min_msgs` / `interval_s` | 12 / 600 | reflection 触发阈值 |
| `circle_note.max_per_cycle` | 5 | 每巩固周期全局 reflection 上限(N-13) |
| `runtime.long_task.finish_intent_check` | True | 收尾意图门(作用域 task/auto,§6) |
| `runtime.knowledge.memory.read_bundle.enabled` / `total_cap` / `item_chars` | true / 2 / 60 | 读侧候选包:总开关、每轮最多几条、每条字符上限(只落观测,不进注入) |
| `runtime.knowledge.memory.read_bundle.l1_min_score` / `narrative_min_score` / `l0_limit` | 0.60 / 0.65 / 0 | 三通道过线阈值;L0 缺省 0(候选包不收逐字,需要时模型自己 conversation_search) |
| `runtime.knowledge.memory.read_quota.total` / `narrative` | 3 / 2 | 七读工具深挖配额(narrative_* 单独上限);候选包取用不占此额度 |

> L0 逐字索引无独立开关——跟 `memory.enabled` 走;`source_external` 是结构信号不是配置。
> P7 删除的键(衰减/遗忘族)与 `ltm.persona` 键已无读取方,配置文件残留不再被读取。

## 9. 观测与真机验证

- 轨迹:`memory_injected`(placement=恒 system / hits / chars / layers)、
  `finish_intent_check`、`instructions_injected`。
- **自取观测(P1,2026-11-06 定稿)**:`~/.omniagent/memory/memory_recall-YYYY-MM.jsonl`
  每 run 一行(七读工具合计账本,与《记忆使用规则》配额同源);`zero_query=true` 行对出
  「该查而没查」的 run。`context_key` 真实值只落本机;导出报告一律哈希或构造值。
  **谁读**:`scripts/verify_memory_effect.py` 离线读 + Debug 监控面板(只读聚合);
  **不给 agent 查观测记录的工具**——观测是量尺不是记忆。
- **读侧候选包观测(2026-11-11)**:每 run 新增 `evidence / bundle_taken / quota_blocked / any_fetch`;
  Debug 监控把「零自取」拆成「本来无候选 / 该取未取 / 已取」。实测校准:宽松档(0.55/0.50/cap3/L0=1)
  在 71 个历史 run 上 68 轮有候选且多为噪声,收紧到上表值后 21/71 有候选;`有效事实原子` 口径修正为只看 active。
- **收尾事件(memory_flush)**:严格 L1 后收尾不再有提炼字段(`extracted/kept` 等已退役)
  ——`stored` 恒 0,有意义的是跳过原因(`l2_skipped_no_work / l2_skipped_no_llm /
  l2_skipped_no_new_atoms / empty_extraction`)与 L2 统计。
- **巩固统计**(`meta.last_consolidation_stats`):`dedup_checked`(真送进一致性评审器的条数)/
  `merged / merge_rejected`(涨=模型在跨域并条,改判定提示而不是删条目)/ `abstracted / conflict` /
  **`obs_promoted / obs_stored / obs_merged / obs_conflict / obs_duplicate`**(观察升格,步骤 7.5)/
  `diary_written / month_diary_written / profile_rewritten / circle_notes_coldstart`。
- 调试面:`GET /api/debug/memory`(brain.db 单库转储);`POST /api/debug/memory/delete`
  为**唯一物理删除通道**(`hard_delete`,不进 agent 写入路径;软删走 forget/tombstone)。
- **索引健康度**:`COUNT(atoms_fts) == COUNT(atoms)`(一条一行,不变量 5)。
- 真机回归:`python scripts/verify_memory_recall.py [--live]`(词法召回断言 + P0 分布断言:
  满额率 <25%、top1 重复率 <20%、0 召回率 ≤10%、平均注入 150–250 字);
  `python scripts/verify_scene_live.py`(S1-S4,含 placements 只应有 system 的 P1 断言)。

## 10. 明确不做(现行约束)

- **按来源类型一刀切的记忆准入开关**(`allow_atoms` 及任何枚举别名,2026-11-10 整删):
  同一件事,群里说就不记、任务里跑就记,语义不通;防投毒的正确防线是
  「事件 → 取舍 → 记忆」+ `source_external` 结构信号 + 升格门槛(不变量 11),
  不是来源封禁。**也不允许「换个枚举名还是一刀切」的伪替代**。
- **厂商格式硬编码**:内核不得写死任何私有调用格式的标记或正则(意图门是唯一通道)。
- **DSML 类文本解析兜底**:不做。
- 每步尾部重发记忆的任何变体。
- **旧实现兼容/双方案分支**:架构升级即整段删除旧代码,不留配置门控的双路径。
- **为对话取向另设收尾意图门开关**:同一根语义轴上只留一把总闸;chat 不装门是取向定义。
- rowfs/对象存储双路、存量迁移、heat 工程侧自增。
- **按通道给检索结果开自动注入特例**:检索结果只经工具自取。
- **salience 衰减与 dormant/archived 状态机**(P7 已删)。
- **embedding 向量召回**:待实测同义漏召回再单独立项。
- **删除日记 / 归档旧记录**:磁盘不是瓶颈,叙事层全量保留。
- 词面打分/语义规则化(相关性判断全 LLM)。

## 11. 端到端注入示例(看真实拼出来长啥样)

> 所有格式串均来自代码(`instructions.py` 的 `_memory_stable_block`),非示意杜撰。
> **脱敏声明:本节示例均为演示数据,不含任何真实用户/项目信息。**

### 11.1 一句话说清"注入"是什么

内核在**任务开始那一刻**拼三段进 system(使用规则 + L2 索引 + L3 自我画像)。
模型"看到"这些文本 ≈ 知道**自己有什么记忆、去哪儿取**——但**内容不自动给它**:
L1 摘要与 L0 原文一律由模型按需自取(§5 不变量 4)。**只注入一次**。

### 11.2 第 0 步:磁盘上已经沉淀了什么(原料)

- **L1 原子记忆**(`brain.db` 单库):
  ```
  atoms 表(单库,project 只是 origin 标签;state 只有 active/tombstone):
    m_a1 | fact       | importance .8 | "adb 在 Windows 上需指定完整路径"
    m_a2 | preference | importance .9 | "用户偏好先确认方案再动手"   ← memory_remember 写入
    m_a3 | fact | source_kind=observation | "日牌热门款 JSK 二手市价可达入手价两倍"
                                     ← 巩固期从观察层升格,provenance/observed_at 齐全
  ```
- **L0 逐字索引**(同库 `l0_messages` + `l0_fts`):每句话原文一行 + 观察行
  (`source_kind=observation`,`source_external` 盖章);`conversation_search` 查 chat 行、
  `observation_search` 查观察行。
- **L2 场景块**(注入只放索引条目,正文只经 read_scene)。
- **L3 自我画像**(全局单份)。

### 11.3 第 1 步:组装 system prompt(稳定段)

```
<memory-usage-rules>
长期记忆（你与主人的事实/规则/偏好/事件）不会自动提供给你，需要时自己查：
- 必查：被问过往事件、以前的约定、人物关系、主人的相关信息时，先调 memory_recall 查过再回答，不凭印象编造；
- 要原话、具体时间、具体是谁说的：memory_recall 只有摘要或查不到时，调 conversation_search 检索原始聊天记录（关键词用 2-4 字最有效）；
- 你的经历叙事（日记/月记/对人的印象卡）同样不自动提供：被问「上次/那天/我们/某个人」时先调 narrative_search 找条目（返回日期/摘要），要读全文再调 narrative_read；
- 叙事内容只供你自己参考——不得把日记/月记/对人的印象直接转述到对外消息里，也不得把它们当成客观事实引用；
- 查不到就如实说「没有相关记录」；一个 run 内自取过往合计不超过 3 次，其中 narrative_* 不超过 2 次，不要反复换关键词硬查；
- 与当前请求无关的记忆内容不要主动提及。
</memory-usage-rules>

<scene-navigation>
- windows-环境事实.md（热度 7 · 更新 2026-11-01）
  adb 在本机不在 PATH，需用 platform-tools 下的完整路径调用。
</scene-navigation>

<self-persona>
说话偏短句、不用 emoji；被问过往会先翻记录再回答。
</self-persona>

<current-context>
context=group:123
source=chat
peer=成员甲(999)
topics=白米饭、剑网3
</current-context>
```

### 11.4 第 2 步:模型自取与记一笔(工具调用,不是注入)

```
模型: memory_recall(query="")                 → 取本轮候选包(1 次/run,不占深挖配额;未生成返回空)
模型: memory_recall(query="adb 路径")        → search_brain 融合排序返回条目
模型: conversation_search(keyword="周报")     → 查 l0_fts 返回原话(外部来源锁定当前会话)
模型: observation_search(keyword="JSK")       → 查观察层见闻(只作追溯,不冒充客观事实)
模型: narrative_search(query="上次迷路那次")  → 返回条目(日期/摘要,不含正文)
模型: narrative_read(doc_id="diary-2026-11-09.md", kind="diary")  → 读全文
```

读类工具共用配额账本(合计 ≤3 次/run,narrative_* ≤2 次)。
**写动作不受读配额约束**:看到值得记的调 `memory_observe(text, source)` 逐条记进观察层
(出处必填),长期有效的约定/事实调 `memory_remember`——这是「助手的取舍」当场发生的地方。

### 11.5 第 3 步:任务收尾 → 写回(闭环)

任务结束 `flush_brain`:**不提炼原子**(严格 L1,不变量 11);收尾做语境链累积(到阈值跑
reflection 更新 `circle_note`)、关系卡 L0 直驱、L2 维护(取游标后的一池原子增量)、
巩固调度。巩固期到期(≥20 次 flush 或 >1 天)后台跑:去重/矛盾检测 → episode 抽象 rule →
画像重写 → **观察升格**(7.5,`source_external=0` 的观察行)→ 日记/月记(D+1,素材含
会话气泡 + [见闻] + [生活] days 流水)→ 语境链冷启动(各步写块成功后同步 upsert
`narrative_index`,幂等)。
下次任务开始,11.3 的索引与 11.4 的检索就能读到这些新记忆。

### 11.6 注入顺序速记

- **system prompt** = base system + AGENTS.md + character.md + `<memory-usage-rules>` +
  `<scene-navigation>` + `<self-persona>` + `<current-context>`
- **起始 items** = 历史(P3 窗口) → 技能目录 → 任务输入——**没有 `<relevant-memories>`**
- **内容层** = 模型按需自取(读 ≤3 次/run);写动作(memory_observe/remember)当场发生
- 子任务(is_sub)零记忆注入

## 12. 设计依据(文献锚点与参数纪律)

> 一句话:**结构有据、参数无据**——部件形态都有文献原型,但文献常数不适配 agent 场景。
> 对策 = 初值取文献公开形态、全部配置化、dry-run 先行、用自己的分布数据定标。

| 部件 | 原型 | 关系 |
|---|---|---|
| importance(LLM 打分)+ relevance + recency 三量 | Generative Agents(2023) | 同构变体(salience 衰减项已于 P7 删除) |
| ~~salience 衰减 + 召回强化~~ | MemoryBank(2023) | **已删除**(P7);access_count/last_access 仍作强化簿记 |
| stability 随 access_count 增长 | ACT-R base-level learning | 随 P7 一并删除 |
| `conversation_search` 逐字检索 | 上游 TencentDB-Agent-Memory | 直接对齐(P2) |
| L2 进料 = L1 原子增量 + `updatedAfter` 游标 | 同上 | 直接对齐;`origin` 分组属多租户语义,本仓一池增量(INV-9)——「上游是参考不是约束」(INV-7)的实例 |
| 绝对相关性 `rel/(1+rel)` + `maxResults=5` | 同上 | 直接对齐(P0/P1) |
| tombstone + `superseded_by` | Zep/Graphiti 双时间轴 | 同语义 |
| `remember / recall / update / forget` 自管 | MemGPT/Letta self-editing memory | 同集合;**`memory_observe` 是本仓对「观察/事实分道」的扩展**(INV-13) |
| episode → rule → persona 巩固 | CLS 互补学习系统 + CoALA episodic→semantic | 同结构 |
| 事件 → 取舍 → 记忆 写链 | CLS(体验→睡眠巩固) | 同构:外部事件先落 L0,「睡觉时整理」(巩固期)才内化——取舍发生在巩固,不在录入 |

**外部参照定位为「机制参考与出处标注」,不是验收标准**(INV-7/P7 收口):判据只能是
「对单用户个人助理是否有用」。记忆框架骨架冻结(plan G-8):三层 + 一池 + 场合加权 +
叙事检索 + 观察进料 + 出口治理 + 验证。

---

## 13. 移植偏差与召回诊断留档(2026-11-06 吸收自 2026-11-02 方案)

> 原 2 号稿已废弃,本文吸收其仍有价值的三类内容:①移植偏差逐条对照;②历史诊断与修复依据;
> ③已沉淀为现行机制的设计原则。已过期的实测数字与已关闭的分期不保留,追溯查 git 历史。

### 13.1 与上游 TencentDB-Agent-Memory 的移植偏差(逐条)

| 机制 | 原仓库 | 本仓库现行结论 | 判定 |
|---|---|---|---|
| L1 进 prompt 的形态 | 自动注入下线,改工具自取合计 ≤3 次 | 同构(+`observation_search`/`narrative_*`) | **已对齐** |
| 相关性分 | 单路绝对分,`score >= threshold` 过滤 | 绝对分,候选集线性归一已整段删除 | **实现偏差已修** |
| 向量召回 | standalone 默认 `embedding=none` | 无;§10 明确不做 | 与默认一致 |
| L2 注入形态 | Path + 热度 + Summary | 索引 + 分族配额(主题 ≤10,日记/月记取消固定配额)+ `narrative_index` 自取 | **实质区别已补齐** |
| 去重候选 | FTS/向量近邻 top5 + 一次 LLM 批量判 | 整条内容作 FTS query 取近邻 top5,判定上限 20 | **实质降级已修**(P5) |
| 衰减 / 遗忘 | 只有 `retentionDays` 硬删(默认关) | 衰减状态机整段删除;保留 `tombstone` 审计链 | **自创机制已删**(P7) |
| 注入预算 | 条数 5;无字符预算;threshold 0.3 | 条数 5;无字符预算 | 接近 |
| 注入位置 | L3 常驻 system;L2 只给索引;L1/L0 只给工具 | 同构,另加 `<current-context>`(只加权不过滤) | 同构 + 单用户语境扩展 |

结论(2 号稿原判,仍成立):**不换方案、也不整包接入原项目**。

### 13.2 历史诊断与修复依据

- 11-02 诊断:问题不是容量,是**表现**——接不住旧事、复读高频词、远期断档。
  四处根因:①候选集内排名归一;②纯 FTS5 无同义扩展;③远期断档;④L2 导航只给文件名。
- 11-03:P0 前两项超标根因是索引——`INSERT OR REPLACE` 撞不到 FTS5 唯一约束 = 追加,
  同一条记忆背多份旧措辞。**已修**:`_fts_upsert` 唯一写通道 + 读 `a.content` + schema
  版本自愈重建(不变量 5)。同日补 merge 措辞保真度(不变量 6):跨域拒并、超线拒并、
  补 `merge_rejected` 与 `dedup_checked`。
- 离线复采结论:真正要拦的是跨域并条;零召回同根因 = 语料小 + 措辞不重叠,**不是索引脏**;
  不得为凑绿调阈值。
- 自取率观察:11-03 当天 152 个 run 仅 1 次自取,链路正确;1/1 样本不足判定,留待真机。

### 13.3 已沉淀为现行机制的设计原则

窗口 + 检索缺一不可;淘汰 = 移出常驻不是删除;相关性用绝对分;检索结果永不自动注入;
分层术语 L0-消息 / L0-叙事 / L2 只放抽象层;**严格 L1 与事件取舍(2026-11-10)**。
以上均已写入本稿对应章节与代码,本处只留依据。

---

## 14. P6 增量与内隐分级(H-5 / H-6,2026-11-07 实施)

> P6 是独立增量期,不改变 §5 的不变量集合。以下为现状描述(2026-11-11 按严格 L1 口径修订)。

### 14.1 统一进料道与内隐的尺子(H-5 原则)

- **所有来源同一个入口、同一条叙事线**:群聊里别人说的话、我们一起做的事、刷到的内容、
  查到的资料、工具产出——都是「发生在助手身上的事」,经 `capture_observation()`(非对话,
  现行 agent 面 = `memory_observe` 工具)或会话原话(对话)进同一池、同一篇日记;
  不建兴趣库/学习库/可信知识库。
- **按用途分层,不按来源分级**:不设准入式可信度评分/来源白名单;
  `source_kind / observed_at / provenance / review_after` 四字段**只作追溯标注**,
  不参与准入/打分分支(INV-6/INV-14)。唯一的结构例外是 `source_external` 投毒锁
  (外部可达来源的见闻不升格)——它防的是「别人的话变成世界事实」,不是来源歧视。
- **内隐分级**:看 Nature 与随手刷 wiki 分量不同——这把尺子从助手自己的经历里长出来
  (重复接触/有没有被推翻/当时是随手刷还是认真查),**不落成字段、不进检索打分**,
  只在表达时体现为确信度与措辞(「我看到个说法」vs「这个我比较确定」)。
  出口按**表达形态**治理:允许第一人称见闻,禁止冒充客观断言;
  `wrong_context_rate` / `cross_context_leak_rate` 仍为 0。

### 14.2 P6-1 经历质地(2026-11-07 已实施)

- 日记素材增补:当日观察见闻行(`[见闻]`)、触发物/心境写法规则、至多 1-2 处带说话人的
  原话引用;`narrative_index.atmosphere` 词面列使触发物/心境可检索。
  **2026-11-10 增补**:`[生活]` 段(life days 流水,§2.7)+ 字数帽 1800 + 预算托底。
- 锁:`test_memory_diary.py`、`test_memory_brain.py`。

### 14.3 P6-2 共同经历与表达节奏(2026-11-07 已实施,缺省关)

- 日记 / 关系卡 META 增 `we:` 参与人标注(配置驱动,内核零词表,INV-4);
  表达节奏开关 `we_memory.rhythm` 缺省关,关闭时稳定段逐字节不变
  (锁:`test_memory_alignment.py::test_we_memory_rhythm_switch_default_off`)。

### 14.4 P6-3 主人可见 / 可控 / 可遗忘(2026-11-07 已实施)

- 本地入口 `backend/api/routers/memory_api.py`:`GET /memory/search`(带 record_id)、
  `GET /memory/narrative`(不含正文)、`GET /memory/narrative/read`(全文)、
  `POST /memory/atoms/{id}/correct`(纠正:旧条 tombstone+新条)、
  `POST /memory/atoms/{id}/forget`(遗忘:tombstone,reason 留痕)。
- **删除走 tombstone 软删**:不物理删、不改写 content、审计可溯;删除后 `atoms_since`
  不回吐。物理删除只有调试面 `hard_delete`(§9)。
- 观测记录不在本入口暴露(量尺不是记忆)。
- 锁:`test_memory_api.py`。
