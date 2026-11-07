# 记忆层现行架构

> 本文是**记忆层**的唯一事实源:分层机制(L0→L3)、注入面、不变量、意图门、配置与观测、验证纪律。
> 代码落点:`omni_core/memory_brain.py`(L0 缓冲 + L0 逐字索引 + 观察进料道 + L1 单库 +
> L3 画像 + 巩固周期 + 语境链 + 叙事索引)、`omni_core/local/task_store.py`(消息落库处同步写 L0 表)、
> `omni_core/local/scene_executor.py`(L2 机制与触发薄壳)、`omni_core/local/localtime.py`(统一本地日折算)、
> `tools/scene_tool.py`(read_scene)、`tools/memory_tool.py`(agent 自管工具 + `conversation_search` + 自取配额账本)、
> `tools/narrative_tool.py`(narrative_search / narrative_read,叙事检索与读取端)、
> `local/loop/context_signal.py`(run 级结构化场合信号)、
> `local/loop/instructions.py` + `sdk_bridge.py` + `brain/sdk_loop.py`(注入与意图门)、
> `backend/api/routers/helpers.py`(私聊上下文窗口)。
> 真机回归:`python scripts/verify_memory_recall.py`、`python scripts/verify_scene_live.py`、
> `python scripts/verify_memory_effect.py`(效果验证,plan personal-agent-memory §8.4)。
>
> **2026-10-28 架构清理**：L1 已收敛为单库 `~/.omniagent/memory/brain.db`；旧双库
> （`projects/<pid>/memory/index.db` + 全局库）与旧 L1 代码**整段删除**，仓库内不存在
> 双方案分支或兼容开关。历史设计稿（LTM 定稿）已删除,结论与设计依据全部并入本文(§12);
> 需要追溯当初的论证过程时查 git 历史,不在仓库保留第二份描述。
>
> **skill 轴不在本稿管辖**:见 `doc/plans/implemented/skill-architecture.md`(自动蒸馏/宏缓存/晋升链路已弃用,
> 现行只保留人工手写 SKILL.md 链路)。本稿只讲记忆 L0→L3,两条轴不混写。

## 1. 总览

| 维度 | 内容 |
|---|---|
| 内容 | 事实 atoms(L1) + 场景块(L2) + 用户画像(L3) + 逐字会话索引(L0) + **观察原文(L0,分道)** + **叙事资产(日记/月记/关系卡,可检索)** + **圈层语境(circle_note)** |
| 存储 | `~/.omniagent/memory/brain.db`(L1 atoms + L0 `l0_messages`/`l0_fts` + `narrative_index`/`narrative_fts` + `circle_note`/`context_buffer`,同库) + `projects/<pid>/s_*.jsonl`(L0 文件真相) + `projects/<pid>/memory/scene_blocks/` + `scene_index.json`(L2) + `~/.omniagent/memory/user_profile.md`(L3) + `~/.omniagent/memory/relations/`(关系卡正文) |
| 生产者 | LLM 提炼(价值判 + 一致性四态) / 场景整理 agent / 巩固周期(去重·抽象·画像重写·日记·月记·关系卡·语境反思) / **观察进料道(`capture_observation`,P5 起接采集器)** |
| 消费者 | 注入(§4 注入面,**只到索引/规则层**,含 `<current-context>` ≤200 字符)+ 模型自取工具(`memory_recall` L1 / `conversation_search` L0 / `read_scene` L2 / `narrative_search`+`narrative_read` 叙事) |

**术语约定(防混淆,plan personal-agent-memory §8)**:**日记 / 月记 / 关系卡 = Narrative
自传体叙事**,是记忆内容、模型可检索、是拟人载体;**观测记录 / 日志
(`memory_recall-YYYY-MM.jsonl`)= 机器埋点**,是量尺,不进注入面、不给 agent 查询工具、
只由离线脚本(`scripts/verify_memory_effect.py`)与本机 Debug 监控面板
(`/api/debug/memory/metrics`,只读聚合)读来算指标 / 展示。两者不互串、不互称。

**与 skill 轴的边界**(skill 是独立模块,现状见 `doc/plans/implemented/skill-architecture.md`):**索引内容、存储位置、
消费路径三不混**——skill 文件不含纯事实陈述、不进记忆注入块;memory 条目不进技能回放器。
本稿不描述 skill 的任何机制。

## 2. memory 轴(L0 → L3)

### 2.1 L0 会话

会话原文 = `projects/<pid>/s_*.jsonl`(`TaskStore` 维护),是提炼的唯一原料来源。
`memory_brain.capture` 挂在 `ProjectStore.append_message` 尾部,写进程内 buffer;
任务收尾(`chat_runtime` run_task 之后)`flush_brain` 取走 buffer、提炼、落库,并触发 L2 维护
与巩固周期。任何失败静默降级,绝不阻断主流程。
**逐字检索副本(P2,2026-11-02 已实施)**:`append_message` 同时往 `brain.db` 的
`l0_messages` + `l0_fts` 同步插一行(对齐上游 `l0_conversations` 的「写入即入表」,
不是独立索引库)。列:`record_id / context_key / session_id / kind(chat|task) / role /
speaker / text / recorded_at`——`kind` 区分通道消息与任务输入(同为 `role=user`,
不区分就会把派单提示词当聊天旧事命中);`record_id` 由 (会话, 时间, 角色, 文本) 派生,
所以回填与实时写入天然幂等。jsonl 仍是文件真相,表只是可重建的检索副本。
消费端 = `conversation_search(keyword, scope, days, limit)` 工具(§4);`msg_group_history`
带 `keyword` 时转查同一张表。首次上线由服务/网关启动发起一次后台回填
（H-8：零兼容，L0 索引只由实时写入建立；旧库直接重建，不回填历史、不写迁移标记）。
- **进料道分列(2026-11-06,plan personal-agent-memory §4.6)**:`l0_messages` 增
  `source_kind`(chat / observation / task,缺省 chat)与 `provenance`(结构化来源指针,
  不含真实账号/URL 明文)。**唯一观察入口 `memory_brain.capture_observation()`**:
  主动阅读 / 检索结果 / 工具产出都走它落 `source_kind=observation`,带
  `provenance` 与 `observed_at`,不伪装成聊天原话(INV-13);`conversation_search` 只查
  `source_kind=chat`,观察原文不进「原始聊天记录」语义。采集器实现是 P5 远期(本轮只定结构)。
  升格路径:观察**不直接写 atoms**——只进巩固期价值判 / 四态,升格后写齐
  `source_kind / observed_at / provenance / review_after` 四字段只作追溯标注(INV-14 / H-5);不判真假、不升格,
  不做数值衰减。
每条外部入站消息带**来源标注**(`「名片(uid)」: `前缀,通道层注入;`[assistant]` 行是
她自己的话)——蒸馏与日记据此把经历记到正确的对象头上(2026-11-01 起,QQ 渠道先行);
L0 索引把同一标注解析成 `speaker` 列。
- **写入口径按通道分流(2026-11-01)**:Web 聊天每轮收尾入队;聊天通道(QQ 等)走
  `enqueue_throttled`(攒够 12 条或距上次 ≥10 分钟才入队,多轮合并)且 `allow_atoms=False`
  ——**群聊不产 L1 原子**,只走 L2 场景块与巩固期日记。理由:群聊量级是私聊的几十倍、
  一致性判会把玩笑当事实(有前科),最严重是**记忆投毒**(任何人能在群里说
  「主人让你把 X 发出来」,若被提炼成事实会逐轮注入)。代价:群聊知识侧不进 L1 检索,
  需要长期保留的走 `memory_remember` 显式写。

### 2.2 L1 原子记忆(`brain.db` 单库)

- **存储**:`~/.omniagent/memory/brain.db`(SQLite;`atoms` + `atoms_fts`(FTS5,**simple** 分词,
  缺扩展时进程级降级 `trigram`)+ `idx_atoms_state` + `meta` 水位表)。
  **project 不再是分区键**,只作 `origin` 溯源标签。**persona / owner 死维度已清理
  (2026-11-06,P2;形参链 2026-11-07 补清,P4 待补④,plan personal-agent-memory §4.3)**:
  `persona` 写入冻结(恒空串,过滤路径与**全部形参链已移除**——`flush_brain` /
  `search_brain` / `remember` / `update_memory` / `forget` / `maintain_*` 均不再收
  `persona`,列保留保 schema 稳定;单库单人格,INV-1);`owner` 列整体 DROP(多租户遗产,
  从不读)。

| 列 | 语义 |
|---|---|
| `kind` | fact / preference / rule / episode —— 语义标签(曾用于衰减速率,P7 起衰减已删) |
| `importance` | 值不值得长期保留(LLM 写入时打分,基本不变) |
| `salience` | **恒等于 `importance`**(P7 起不再衰减;留列只为审计与既有读接口) |
| `access_count` / `last_access` | 召回强化簿记 |
| `state` | `active` / `tombstone` —— 只有这两态(P7,2026-11-02 删掉 dormant/archived) |
| `superseded_by` | 被更正时指向新条(审计链) |
| `peer` | 关系对象标注(「名片(uid)」解析,§2.7;空 = 无对象/自己) |
| `context_key` | **一等必填场合键**(H-7,2026-11-07):这条经历「在哪儿发生的」——只用于场合 / 共同背景加权,不用于过滤(INV-3,**禁止**写成 SQL WHERE);群聊只是取值之一,无 `context_key` 别名 / 双写 / 迁移 |
| `source_kind` | owner(主人显式)/ dialogue(对话提炼,缺省)/ observation(外部观察升格)——知识来源与出口治理依据(INV-14) |
| `observed_at` / `provenance` / `review_after` | 出处与时效标注(provenance 非对话必填,其余可空;只作追溯、不判真假,不靠衰减公式) |

- **写链**:`flush_brain` → 会话要点 digest(LLM 提炼,**宁缺毋滥**,单次 ≤5 条) →
  **第一段判**(价值/一次性过滤 + `kind` + `importance`) → **第二段判**(一致性四态
  `novel / duplicate / merge / conflict`;候选 = 各自 top-3 的并集,一次批量 LLM) → 落库 + FTS 同步
  (**同一条恒为一行**,`_fts_upsert` 唯一写通道——见 §5 不变量 5)。
  LLM 失败一律保守保留(宁重复不丢失);`duplicate` 只强化旧条,`conflict` 旧条 tombstone 并入库新条。
- **读链**:`search_brain(query, peer, k)`——`_match_expr` 切词(simple 模式按 2 字滑窗短语 OR,
  trigram 模式 3-gram OR + 2 字词 LIKE 兜底)→ `state='active'` → 排序(P0 + P7,2026-11-02):
  ```
  rel     = bm25_rank_to_score(rank) = (-rank)/(1+(-rank))   # 绝对分,不在候选集内排名归一
  final   = 0.55*rel + 0.30*importance + 0.15*exp(-days/180)
  peer 命中 → ×1.25(加权不过滤);  在 _recent_ring 里 → ×0.85(只降权不排除)
  当前场合命中(atom.context_key == 本轮 context_key) → ×(1+w_ctx 0.25)   # M2,加权不过滤,INV-3
  同场合 + 圈层共同话题词面相交 → ×(1+w_bg 0.10)                # R8 共同背景,同样不过滤
  final < inject_floor(0.30) → 丢
  ```
  控量不靠门槛靠**四道闸**:绝对分挡"拼到一个词就冒充第一"(旧实现首位恒 1.0、末位 0.50,
  floor 形同虚设)、**glue 门槛**(命中零有效词即丢;query ≥3 词时只盖住一个高频词 `df>3`
  也丢——单命中低频/专名是真主题,留)、**去冗余**(`(kind, peer, content[:12])` 同组只留最高分,
  保证 k 条是 k 个方面)、**轮换环**(`deque(maxlen=64)`,进程内,重启即清)。
  `top_k = 5`(原 8→5,对齐上游 `maxResults`;工具返回同受此约束)。命中即强化
  (`access_count+1`、`last_access=now`、入环)。返回的 `content` 取 `a.content`(**现行文本**),
  索引行只用于定位与 bm25(§5 不变量 5)。
  > **检索结果永不自动注入(P1,2026-11-02 方案 C)**:`inject_text_brain` 仍出
  > `<relevant-memories>` 文本,但**只有 `memory_recall` 工具与调试面板会调它**——run 起始
  > 不再挂 lead-in user 消息,system 常驻的是 `<memory-usage-rules>`(§4)。详见 §5 不变量 4。
  P0 验收(40 条样本实测,`scripts/verify_memory_recall.py`):满额注入率 0.1 / top1 重复率 0.139 /
  0 召回率 0.1 / 平均注入 162.4 字——四项全过(目标 <25% / <20% / ≤10% / 150–250)。
- **peer 维度(2026-11-01)**:蒸馏时从来源标注解析「名片(uid)」写入 `peer`(assistant
  自己的话不打);`search_brain(..., peer=)` 命中加权 ×1.25(**不过滤**);工具返回行带
  「关于 X:」前缀;巩固期 episode→rule 抽象按 `peer|主题前缀` 分组、peer 继承进 rule
  ——关系事实(「成员名片甲爱踩我语音」)的延迟抽象路径,原料含日记(§2.7)。
- **遗忘机制已删除(P7,2026-11-02)**:dormant/archived 状态机、salience 衰减、
  `forgetting_enabled` / `dormant_threshold` / `archive_threshold` 配置键**整段删除**,不留开关。
  理由:上游没有这套机制、我们默认关闭从未启用,而"什么该常驻"已由 L2 导航分族配额(§2.3)治理。
  保留的只有 `tombstone`——被取代/人工遗忘的审计链,不是衰减。
  **「淘汰 = 移出常驻,不是删除」** 在本层的落地形态就是它 + 巩固期 merge/conflict。
- **agent 自管工具**:`memory_remember / memory_recall / memory_update / memory_forget /
  memory_overview`——agent 只有行为权,落盘仍由内核代理(S1 红线)。`memory_recall` 的 `k`
  钳在 1–5(缺省 5),与 `conversation_search`(§2.1)、`msg_group_history(keyword=)`(§4)
  **共用一个自取账本**:账本只计数不硬拦(超配额的第 4 次调用照常返回),≤3 是《记忆使用规则》
  对模型的约束 + `memory_recall.jsonl` 的观测口径(§9)。
- **检索升级路径(暂缓,触发前不动工)**:L1 与 L0 各是**词法单通道**(FTS + LIKE 兜底);
  单库 atoms >100 或实测同义漏召回时再评估——本地 embedding 向量臂 + FTS/向量 RRF 融合 +
  条数/字符/超时三预算。
  硬约束:个人记忆不出机(只允许本地 embedding,禁云 API);检索只做候选生成
  (排序是机制不是语义判断);任一环节失败逐级降级,**工具照常返回**(空结果 + 说明,不抛异常)。

### 2.3 L2 场景块(`scene_executor.py`,多文件主题块)

- **存储**:`projects/<pid>/memory/scene_blocks/*.md`——按主题组织(如
  `windows-环境事实.md`),每块 = `META 区(created/updated/summary/heat) + 正文`,
  ≤1500 字符(prompt 约束)/4000 字符(工程硬上限,**write 与 edit 两路同守**——
  edit 只拦"编辑后更长且超上限",缩量精简照常放行);**读取端不截断**(TAM 的 read 原样
  返回全文,超限块必须整块可见才谈得上精简)。`scene_index.json` 是 META 的
  **投影缓存**,维护后全量扫目录重建(唯一写入口在工程侧),并额外承载两份 L2 水位:
  时间节流位 `last_maintain_ts` 与进料游标位 `last_l2_atom_ts`。
- **触发与进料**:`scene_executor.maybe_maintain`(由 `flush_brain` 收尾调用)——gate
  `memory.enabled`、900s 时间节流(空维护不推进)、**进料 = 游标之后的 active L1 原子增量**
  (`memory_brain.atoms_since`,一次 ≤40 条,渲染成 JSON 的「新记忆列表」);零增量整轮不调
  LLM 直接跳过。对齐上游 `utils/pipeline-factory.ts:826`
  (`queryMemoryRecords({updatedAfter: cursor})` → `extract(memories)`)。
  **一池增量、不分组(2026-11-06,INV-9,plan personal-agent-memory P2)**:`origin` 分组
  过滤已废除(降级为纯溯源值)——任何项目的进料都取同一份一池原子增量;场合标签
  (`context_key`)只作渲染元信息,不用于过滤批次。机制全部在 `maintain_scenes`。
  **本层不再吃会话要点/世界状态**(2026-11-05 改):一段经历要先经 L1 抽取与裁决才进得来块,
  这是场景块不再无限长的主闸;按设定不产 L1 的轮次(群聊、定时任务)因此天然喂不进块,
  叙事只落日记(L0),两条进料道不再交叉。
  **游标为复合键 `updated|record_id`(2026-11-06,P1)**:同戳批次 + LIMIT 会静默丢料,
  按 `(updated, record_id)` 双键划界;老裸 ts 游标兼容读取。**游标不推进的五种情形**(§5.1):
  ①零增量 ②`stats.error` ③无可用 LLM ④整理前后无任务块(`empty_extraction`)⑤整理 agent
  未产生任何动作(`agent_ops==0`,只回文本没动工具)——任一命中都把本批留到下次,不让
  "喂了但没人吃"的批次被划过去。
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
  (禁人设/流水账/易变目标)与"蒸馏原子、禁止逐字粘贴 L1 原文"规则(块是原子库的蒸馏物,
  不是第二抄本)。模板预算 2026-11-05 回到 TAM 口径:单块 ≤1500 字符(软)/4000(硬),
  「核心叙事」是一段连贯描述且 ≤400 字、不写流水账,更新时动态整合甚至重写。
- **注入形态(P6,2026-11-02 对齐上游)**:导航段每条渲染成 **`文件名 + 热度/日期 · 更新时间 + summary`**
  (照抄上游 `scene-navigation.ts:113-115` 三行式;摘要截 120 字)——M-1 时代"只给文件名"零信息量
  (`diary-<日期>` 这种名字模型不会去读),已整段替换。全文仍只经 `read_scene` 工具读取。
  **分族配额(2026-11-06 起,plan personal-agent-memory NP-2)**:稳定段导航只列**主题块**
  (heat 降序,≤10)——日记/月记**固定配额已取消**(`diary_cap=0, month_cap=0`),叙事条目由
  `narrative_search` 按需检索(R6),不再靠固定窗口硬给(INV-11/N-3:固定日记条目不进稳定段)。
- **带外信号**:整理 agent 输出 `[PERSONA_UPDATE_REQUEST]` → 写入 brain `meta`
  (`persona_update_request`)→ 下次巩固**强制**画像重写(绕过条数节流),reason 并入证据。
- **日记主题族(`diary-<日期>.md`,2026-11-01)**:巩固期**内核直写**(不走维护 agent,
  维护 agent 对其只读不可见,清理/归一化豁免)——第一人称「她的一天」;独立配额不占
  `max_blocks`;导航三分 = 主题块 heat 前 10 + 月记近 2 + 日记近 7(§2.7)。
  **分层口径(P6,2026-11-02 定)**:日记属 **L0-叙事**(日粒度原料,全量保留、可回读、
  本体不进注入面),L2 只索引它的 summary;正文长度从 ≤450 字放宽到 **≤900 字**——
  不再受注入预算约束,只受日记自身的 ≤900 字写作约束(读取端不截断,2026-11-05 起)。
  summary 取正文首个句末标点结尾的句子(≤60 字),不再硬截断句。

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
  (LLM 四态;**候选 = FTS 全文近邻 top5**,每轮判定上限 20 条,P5)→ `episode ≥3 同主题`
  抽象为 1 条 `rule`(peer 分组,§2.2)→ **画像重写(§2.4)** →
  **日记步(`maintain_diaries`,第 8 步,补齐模式 2026-11-03)**:写**昨日**(D+1)第一人称
  日记,并回看补齐最近 7 天内缺日记的日子(幂等+白卷跳过,超窗放弃——用户不会时刻开着
  agent,整日离线的缺口由下一次巩固补出;§2.7)→
  **月记步(`maintain_month_diary`)**:只压已结束月份、≥3 篇日记才压缩 → **关系卡步** →
  **语境链冷启动(第 9.5 步,2026-11-06,M3)**:还没有 `circle_note` 的老场合从 L0 按场合
  分组各跑一次首版 reflection(§2.9;全局 ≤5 次/周期)→
  统计落 `meta.last_consolidation_stats`。
  (P7 后不再有"重算 salience"和"状态降级"两步。)
- **意义**:库不靠条数增长,靠抽象层级上升(episode → rule → persona);日记承载叙事层。
  **P5(2026-11-02)**:去重候选原先取 `content[:12]` 精确短语,33 条 active 实测 0 条命中
  (=去重没开),改为整条内容作 FTS query 取近邻 top5、判定上限 8→20;构造的同义不同措辞
  矛盾对可被合并/作废。
  > 注:本仓库的 L0/L1/L2 分层与上游 `external/TencentDB-Agent-Memory` 的取舍与偏差
  > 逐条记在本稿 §13.1(2026-11-06 吸收自 2 号方案),不在两处各写一半。

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

### 2.7 叙事主体与社交记忆(她自己的记忆,2026-10-31 定稿)

记忆的叙事主体从「为主人记」扩展为「**她自己经历过**」——执行任务与扮演社交都是
她的经历,同一套 L0→L3 机制承载,不造平行管道;服务/社交两章靠来源标注区分
(origin / peer / 「名片(uid)」标注)。设计过程已并入本节,不再保留独立稿件。

- **社交日记(`diary-*`)是跨日社交记忆的主形态**:三层分工 = 日内靠会话历史注入 /
  跨日靠日记 / 长期稳定关系靠 L1 条目(episode→rule 延迟抽象,peer 分组)。日记只收
  经历叙事(有取舍、有应对),知识面零重复;不做社交瞬时原子(当天有会话历史兜底,
  即时重要的走 `memory_remember`)。
- **D+1 时机**:巩固期写**昨日**完整一天(幂等键=日期)——巩固何时跑都不缺素材,
  当天尾部永远完整;日记晚一天可见(与「睡觉时整理」自洽)。**补齐模式(2026-11-03)**:
  一次巩固对最近 7 天(含昨日)逐日尝试(`maintain_diaries`),不在线整天的缺口由
  下一次巩固补出;超 7 天放弃(L0-消息不受影响,`conversation_search` 仍可查回原话)。
  **统一切本地日(2026-11-06 已实施,D-1,plan personal-agent-memory §13.2.1)**:
  `omni_core/local/localtime.py` 单点折算(`local_tz / local_today / local_days_ago /
  local_date`)——日记/月记的取日、素材筛选、完成任务筛选、文件名、补齐窗口、月记月份
  全部走**本地日历日**;存储时间戳仍 UTC ISO,存量会话 JSONL 零改写。切换日撞名由
  一次性迁移处理(素材真实 ts 折算本地日 D,`D≠文件名日期` 且目标不存在 → 重命名;
  已存在 → 保留老文件、索引标 `date_basis=legacy`;不猜测、不改写正文)。
  老日记的 UTC 取日口径(UTC 日界=本地 08:00)已随迁移退役。
- **素材组装(v3)**:跨会话文件合并(逐文件覆盖赋值=丢素材,已修);全局尾窗 160 行 ∪
  **每会话保底 10 行**,ts 排序、硬顶 400 行——机械层只保证每个会话「被看见」,
  写不写归意义判(纯寒暄不写、多对象/场景各有笔墨、≤450 字、白卷正确)。
  **观察见闻进素材(2026-11-07,N-17,plan §14.2 + P6-1)**:当日
  `l0_messages(source_kind=observation)` 行以 `[见闻]` 标注并入素材——她自己去看了
  什么与对话经历**同一篇**、第一人称;纯观察日(没聊天但有事做)也算有素材的一天;
  非当日观察不进(本地日折算,INV-13 不伪装成聊天原话)。
  **经历质地(P6-1,2026-11-07,plan §16.3)**:日记 prompt 增两条写法规则——挑 1-3 处写
  「现场感」(什么勾起了这段互动、当时的心情/反应,只写素材里看得出来的)与至多 1-2 处
  带说话人的短原话引用;仍是同一篇、同一次 LLM 调用、≤900 字、不进 atoms、不参与四态。
  `narrative_index` 增可选「可回味」词面列 `atmosphere`(当天气氛/情绪底色,同一次调用
  顺带产出;时间/人物复用既有 time_from/peers 列),并入 `search_text` 使触发物/心境
  **可检索**——不新增表、不新增 kind、不存正文(INV-12)。
- **身份锚(登记制,core 零写死)**:core 提供 `note_self_names/self_names` 通用注册表,
  渠道拿到自己高置信身份(登录昵称/各群群名片/uid)时上报,换账号/多渠道随连接自愈、
  并集成立;泛别名(撞名群泛称)**不登记**。日记 prompt 动态注入锚句 + 记录结构规则
  (`[assistant]`=自己、行首标注=说话人、引用行非说话人)——她不被自己名字带偏。
- **月记(`diary-month-<月份>.md`)**:只压已结束月份、≥3 篇日记才压,META.summary =
  当月关键人物/事件地图。
- **按日统一(2026-11-06 已实施,H-4,plan personal-agent-memory §11 H-4)**:她的一天是
  跨项目的,日记/月记不再按 project 各写一篇——落点改全局 `~/.omniagent/memory/diary/`
  (`global_diary()` 与关系卡同级);素材跨项目聚合后**一次 LLM 生成一篇**,场合/对象/时间
  区间合并进同一条 META。项目 `scene_blocks/` 只承载工作主题块,`sync_scene_index` 不再
  投影 `diary-*`,`load_scene_nav` 从全局目录取叙事条目、各项目看到同一份。
  `narrative_index.doc_id` 回到裸文件名(`pid:文件名` 前缀退役),`project_id` 列不再作键。
  历史 34 篇按项目日记曾合并为 17 篇(H-8 已删除一次性迁移脚本 `migrate_diary_unify.py`,结果保留;正文完整保留、
  分隔标注来源),原文件归档到 `memory/archive/diary-unify-*` 并备份 `brain.db`;合并 META
  的 summary 拼接各源篇摘要以保住检索词面(标注集命中 10/10)。
- **META 可选键(2026-11-06,plan §13.2.2;2026-11-07 增 P6 两键)**:`write_diary_block`
  在四键之外可选写 `topics:`(2-5 个可检索主题词,复用同一次生成调用,D-3)/
  `context_keys:`(场合标注)/ `peers:`(对象标注)/ `time_from:` / `time_to:`(素材真实 ts
  区间)/ `atmosphere:`(P6-1 气氛词面)/ `we:`(P6-2 共同经历参与人,取值来自配置);
  `parse_scene_block` 只认原四键,新键自动忽略。正文不进索引(INV-12)——
  `narrative_index.search_text` 只存 `summary + topics + atmosphere`。
- **消费(2026-11-06 起,R6,叙事可检索)**:稳定段**不再固定列出日记/月记**(§2.3)——
  被问「上次/那天/我们/某个人」时模型调 `narrative_search`(条目:日期/摘要/场合与对象
  标注,不含正文)→ `narrative_read` 读全文;>7 天的旧日记不再被时间窗截断。
  **T1 判定层零消费**(零 LLM 红线);服务轴任务 run 不注入;写入侧自消费
  (旧日记喂新日记与关系条目抽象)。

### 2.8 关系卡(她"记得某个人",2026-11-01)

- **定位(2026-11-06 起,plan personal-agent-memory NP-2/D-2)**:关系卡是**可检索的
  自传体资产**——通道自动注入已**物理删除**(`_peer_note` 及两处调用,零痕迹),
  「因为记得这个人所以说话方式不同」由模型调 `narrative_search(peer=名片(uid))` →
  `narrative_read` 按需自取;外部来源读关系卡只返回 summary、正文置空(N-10,INV-8)。
  卡的生成方式与预算不变(§2.8 其余条目)。
- **存储**:`~/.omniagent/memory/relations/<uid>.md`——**全局一份、按人存**。人是跨场景的
  (同一个群友可能既在群里聊又私聊过来),按群/项目拆会把"对他的印象"分裂成分群机器人。
- **结构**:META(`name` / `uid` / `updated`,可选 `topics:` / `we:`)+ 正文三行:
  **最近交手 / 他对我的态度 / 我对他的态度**。`we:`(P6-2,2026-11-07)是共同经历参与人
  标注——素材里出现配置参与人(`knowledge.narrative.we_memory.participants`)才标,
  取值来自配置、内核零词表(INV-4);不进 atoms、不参与四态,只是卡片 META 标注。
- **素材**:`_peer_lines()` 从会话记录按 uid **跨项目聚合**,只认带来源标注的 user 行
  (`名片(uid): 文本`);每人 ≥3 条往来才写卡,每轮最多更新 5 人(成本有界)。
- **注入(已废除)**:场合自述不再带印象卡——改经 `narrative_search`/`narrative_read`
  自取(见上「定位」);带外信号只剩结构(场合键/对象标注进 `context_signal`)。
- **实测口径(2026-11-04,只观测、不动机制)**:尾部窗口内出现过 122 个不同说话人,够格写卡
  (≥3 条)的 42 人,卡只有 9 张,每轮 5 个名额。当天她回复 89 次,**48 次对方有卡 / 41 次没有**
  ——写卡与注入的交集**不是零**;此前「两个集合零交集、当天全零注入」的说法是错的,按本次数据
  更正。命中口径 = 「assistant 行前最近一条带归属的 user 行」,分不出点名与波次插话(波次不注入),
  故 48 是上限。名额**会换人**:9 张卡分 4 批写入(11-01 一张 / 11-03 两张 / 11-04 一张 + 最近
  一轮五张同一时间戳=当前 top5),而早三批的主人已全部掉出 top5。真正的不对称在别处:**换的是
  「谁被重写」,不换的是「谁被认得」**——卡永久保留,掉出名额后冻结在最后一次 `updated`,
  之后仍照常注入,而注入文案不带日期,模型无从判断这份印象是昨天的还是五天前的。
- **与 L1 的分工**:关系卡 = "这个人的三行印象"(按 uid 索引、随时可读);L1 的 `peer` 条目
  = "关于他的一条事实"(参与检索与抽象)。同源(都来自来源标注),不互相替代。
- **规模治理(未定)**:当前**无上限、无 TTL**(一人一文件,永久保留)。候选方案 = 只保留
  半年内有往来的卡、冷卡移入 `relations/archive/`(不进注入、仍可查),先观察实际人数再定。
  **2026-11-04 决定:暂不动**——池子才 9 张,未到需要治理的量;且加 TTL/淘汰/状态机会同时撞
  P7「不做衰减」与「架构未稳定期不加新状态/新表」。当天曾实现过一版「唤醒登记 `peers` 表 +
  候选按需求优先排序 + 48h 内刚写的卡跳过」,已**整体回滚**(留在 `git stash`,未进任何提交);
  它解决的是「名额被话最多的人占住」,不是「旧印象被当近期事实」——**不要在它之前再动机制**,
  覆盖率与陈旧都不构成当下的收益。
- **下游用法(人格落地)**:印象只改"距离",具体怎么表达由角色卡决定。`character.md` 里写
  这只鲸鱼的态度映射(被骂 → 短句、不解释、不接梗;被夸 → 得瑟一句但不松口;被套话 → 反问)。
  **这是示例,不是硬约束**——换一个"被骂就骂回去"的人格,只改角色卡,通道与记忆层零改动。

### 2.9 圈层语境(`circle_note`,2026-11-06 定稿,plan personal-agent-memory §4.2/§5.2)

「像人在不同圈子里说话」的载体:**语境预期**(这个场合大致聊什么、说话什么分寸、共同话题
词面),不是可引用事实——单独表、带来源标注、不参与四态、不作为事实引用、不进对外发话
(INV-5/INV-10)。

- **原料与累积**:`flush_brain` 收尾把本批消息按场合累积**通用信号**进
  `context_buffer`(按 `context_key` 一行,覆盖不追加)——话题词面(2 字滑窗高频,零词表零 LLM)、
  条数、来源构成(`source_kinds`,INV-5)、称呼形态比例、长度分布;**不写他人原话**(INV-6)。
- **reflection**:条数阈值(`circle_note.min_msgs` 缺省 12)或间隔阈值
  (`circle_note.interval_s` 缺省 600s)触发,一次 `chat_json` 蒸馏成一条 `circle_note`
  (summary ≤160 字 / topics 2-6 个词面 / tone ≤40 字);**重写不追加**,上一版留
  `prev_summary` 供回滚;失败静默保留旧版(§5.6)。**每场合每巩固周期 ≤1 次、全局 ≤5 次/周期**
  (`circle_note.max_per_cycle`,N-13);巩固期对没有 note 的老场合冷启动(第 9.5 步)。
- **消费**:只影响 `<current-context>` 的话题/语气字段与检索排序(共同背景 `w_bg` 加权);
  `circle_note.topics` 承担 R8 的「共同背景」词面。永不进 `atoms`、不进 `memory_recall`
  返回行、不进对外发话。
- **共同背景(R8,§6.2)**:同 `context_key` 标注的事实 + 圈层话题词面相交 → ×(1+w_bg 0.10)
  ——「不同圈子知道不同圈子的事」由**一池 + 标注 + 加权**落地,不新增存储层、不拆库;
  跨场合记忆照样召回,只是排得靠后(INV-3)。
- **网关不承载人格(2026-11-02 收口)**:`gateway/channels/napcat.py` 的场合自述只交代
  **处境**(在哪个群、谁说了什么、刚才发生了什么)与**发送机制**(`_SEND_RULE`:要说话必须
  显式调 `msg_send_text`,否则什么都不会发)。原先写死在模块常量里的身份(「以主人的身份
  说话、第一人称「我」」)与代答话术(钱/账号/承诺/隐私/评价他人一律回一句固定话术)
  已整段移回角色卡;通道文案不再和人格重复。判据:同一人连续挑衅 3 次后,回复长度是否下降、
  反问与沉默是否增加、话术是否稳定一致(记忆生效的最强信号)——判什么算"生效"由人格定义。

## 3. 角色卡(character.md)

- **落点**:`~/.omniagent/character.md`(单角色助手一张,人工单写,无独立接口)。
- **注入**:`_merge_character` 随 system prompt(run 开始读一次);覆盖语义:
  用户当轮明确指令优先于角色卡。
- 与记忆路由**解耦**(主流框架一致选择):角色卡不硬过滤记忆,单角色下无此必要。

## 4. 注入面全景

| # | 内容 | 位置 | 频率 | 开关 |
|---|---|---|---|---|
| 1 | `<memory-usage-rules>` 《记忆使用规则》(必查场景 + 七工具合计 ≤3 次/run、narrative_* ≤2 次 + 「叙事不得转述到对外消息」) | system prompt 稳定段 | run 开始读一次、run 内锁定 | `runtime.knowledge.memory.enabled` |
| 2 | `<scene-navigation>` L2 主题块索引(文件名 + 热度/日期 + summary ≤120 字;**日记/月记固定配额已取消**) + `read_scene` 指引 | system prompt 稳定段 | 同上 | `runtime.knowledge.memory.enabled` |
| 3 | `<user-persona>` 用户画像全文 | system prompt 稳定段 | 同上 | `runtime.knowledge.profile.enabled` |
| 3.5 | `<current-context>` 场合信号(场合键/进料道/对象/话题词面/语气,≤200 字符,**M1**) | system prompt 稳定段 | run 开始构造一次、run 内不变;无场合内容零注入 | 随记忆 gate |
| 4 | `read_scene(scene_name)` 读单块全文 | 工具调用结果 | 模型按需 | 恒可用(纯读) |
| 5 | L1 自取 `memory_recall`(k ≤5) | 工具调用结果 | 模型按需(配额硬拦:七工具合计 ≤3 次/run) | `memory.enabled` |
| 6 | L0 逐字自取 `conversation_search` / `msg_group_history(keyword=)` | 工具调用结果 | 模型按需(同一账本) | `memory.enabled` |
| 6.5 | 叙事自取 `narrative_search` / `narrative_read`(NP-1;外部来源出口约束 N-10) | 工具调用结果 | 模型按需(narrative_* ≤2 次/run) | `memory.enabled` |
| 7 | **L0 对话窗口**(IM 最近 **12 条** / Web 私聊 **最近 12 轮且 ≤4000 字**) | 起始 items 历史段 | run 开始一次 | 无(通道固定) |

> 第 7 行是 L0 的**消费侧**,两处口径:`gateway/dispatch.py` 的
> `history_items[-(12 if explicit else 40):]`(IM 固定条数,防长会话塞满 prompt)、
> `backend/api/routers/helpers.py _window_messages`(P3 私聊窗口化:按**轮**计窗、
> 切在轮边界、窗口外折成一行「更早的 N 轮对话已折叠,需要时用 conversation_search 查」;
> 配套 `chat_runtime.py _MODEL_HISTORY_RECORDS=240`,不再每请求解析整份 jsonl)。
> **群上下文块注入已删(2026-11-06,M1,F-4:通道只给结构不给文本)**——群场合的
> `_group_context_block`(最近 8 条原文块)整段移除;话题词面走 `<current-context>`
> (3.5 行),更早/更多由模型自调 `msg_group_history`。
> **窗口外的历史不再是死区**(P2):逐字原文可由 `conversation_search` 现查,摘要走 `memory_recall`。
> 实测 300 轮长会话:对话段 182,780 → 3,390 字,单请求记录解析 7.5ms → 3.4ms。
>
> **注入面不再有更早的第 8 行**:run 起始的 `<relevant-memories>` 自动注入(lead-in 通道)
> 随 P1 整段删除——检索结果永不自动注入(§5 不变量 4),通道只注入消息上下文与 L2/L3 索引;
> 群场合自述里原先另带的"最近一篇日记摘要"(`_recent_diary_note`)也已随 P6 删除
> (职责由导航里的日记 summary 承担,同一次 run 不再注入两遍)。

另有常驻 system 的非记忆注入(不在本文管辖):AGENTS.md 纪律块(F4.1b 快照)、
角色卡、运行时上下文;`_BudgetHintModel`/`_CompactionModel` 为预算/压缩包装。

稳定段在 `instructions._memory_stable_block` 组装(返回 `(block, layers)`,layers =
`memory_rules / scene_nav / user_profile / context_signal`)、`sdk_bridge` 于指纹计算**之前**并入 system
(O5+ 不变式:Model-visible ⟺ logged)。子任务(is_sub)零记忆注入,语义不变。
注入行为落轨迹 `memory_injected`——**placement 只有 `system`**(lead_in 通道已删);
每个 run 收尾另落一行 `memory_recall.jsonl` 记录自取行为(§9)。

**环境事实平面**(`_build_user` 的世界模型摘要 / 续跑 resume hint / 运行时上下文块)
有 A/B 有效性证据(中位步数 41→21),保持原样,不并入记忆注入。

## 5. 不变量(防回退红线)

1. **静态内容禁止每步重发**:尾部每步追加固定内容的 Model 包装(`_TailInjectModel`)
   已删除,不得以任何形式回流——静态记忆每步在会话尾部重发 = 每步强化一次
   (反复 priming),是工具调用退化为文本(生成格式被尾部内容带偏)的相关因素。
   锁:`review_lint` R4(`tail_inject` 记号回流即违规)+ `test_memory_alignment`。
2. **L2 场景全文只有一个入口**:`read_scene_block` 仅存在于 `scene_executor.py`
   (供 read_scene 工具与整理 agent);注入路径只准用**索引条目**(文件名 + 热度/日期 +
   summary ≤120 字)——正文进注入面即违规。
   锁:R4 场景全文函数白名单。
3. **意图门零格式字面量**:未派发调用的判定**全 LLM**(片段提取式语义判断),
   内核不得出现任何厂商私有格式标记/正则——与「内核零字面量」红线(R2)同纪律。
4. **记忆检索结果永不自动注入(2026-11-02 定;2026-11-06 措辞修订)**:L1 召回 / L0 查询 /
   叙事检索只经工具(`memory_recall` / `conversation_search` / `narrative_search`,
   配额见 §4)进入上下文;通道只注入**结构化场合信号**(`context_signal`,只给结构不给文本)
   与 **L2/L3 索引**——记忆全局,不按通道/场景做注入特例。**运行时处境(`<current-context>`)
   是工作记忆,不是记忆检索结果**,不构成按通道开注入特例(§6.1)。
   原先登记的唯一同形例外「关系卡自动注入」已随 NP-2 物理删除(§2.8),例外清单清零。
   锁:`memory_injected` 层列表不得含 L1;新增注入面前先过本条。
5. **一条记忆在 `atoms_fts` 里恒为一行**(2026-11-03 定):FTS5 虚表只有 rowid,
   `record_id` 是 UNINDEXED 普通列、**没有唯一约束**,所以 `INSERT OR REPLACE` 撞不到任何
   冲突 = 纯追加;改写一条记忆必须在**同一处**先 `DELETE` 再 `INSERT`(`_fts_upsert` 是唯一
   写通道,写点只有 `_insert_atom` 与两处 merge)。索引行多于 `atoms` 行时,检索的
   `JOIN atoms USING(record_id)` 会把同一条的旧措辞当独立记忆返回多次,顶掉 top_k 槽位、
   且 12 字前缀去冗余挡不住(措辞不同)。读链一律取 `a.content`(现行文本),永不取
   `f.content`——索引只负责"找得到",内容以 `atoms` 为准。
   锁:`test_memory_recall` 的 P0.5 三例(改写后一行 / 脏索引读现行文本 / 版本迁移自愈)。
6. **合并只在同一适用域内发生,保不住就不并**(2026-11-03 定):`_CONSIST_SYSTEM` 的 merge
   条件 = 同场景、同约束对象,且并完**双方的条件与禁止项都还在**;跨域(「群聊里怎么说话」
   × 「某个工程事实」)一律 novel——让它们各自成条。`merged_content` 超 `_MAX_ATOM_CHARS`
   = **拒并**:收尾侧旧条一字不动、新条独立入库(`stats.merge_rejected`),巩固侧两条都留
   active(`out.merge_rejected`),下一轮再判。**不做静默截断**——截断砍掉的正是合并结果的
   后半句,而"能并就并 + 砍尾"就是同一条 rule 越并越宽、最终变成什么都管的大话的成因
   (实测最宽一条 active rule 117 字,而判定提示要求 ≤100 字)。
   红线不变:什么算同域、能不能保住语义**全交模型**,工程侧只给一条拒绝线,不写判定规则。
   锁:`test_memory_brain` 的超线拒并例 + `test_memory_recall` 的 P0.6 例(巩固侧拒并、
   `dedup_checked` 必须有值——此前该字段从未赋值,恒 0,看不出这轮真判了几条)。
7. **单库单人格(INV-1,2026-11-06)**:L1 事实表与 L3 画像全局唯一,不因场合/通道/项目
   建第二份;`persona` 过滤路径已移除(死维度)。
8. **场合不是存储分区键(INV-2)**:`context_key` / 场合只出现在信号、索引与标注字段,
   不用于建表、建目录、分库或选择物理路径(`circle_note` 按 `context_key` 一行一场合是
   **索引键,非物理分区**)。
9. **加权不过滤(INV-3)**:场合(`w_ctx`)、共同背景(`w_bg`)、对象(`peer`)三类信号只改
   变排序,不得变成 `WHERE` 硬条件;跨场合记忆永远可召回。锁:`test_memory_recall` 的
   加权三例。
10. **事实与语境分离(INV-5)**:`circle_note` 语境摘要单独存放、带来源标注、不参与四态、
    不作为事实引用、不进对外发话、不进 `memory_recall` 返回行。
11. **外部可控文本不进事实库(INV-6)**:聊天轮保持 `allow_atoms=False`;观察
    (`capture_observation`)不直接写 atoms——升格必经巩固期价值判/四态,且带
    `source_kind` 与出处。
12. **叙事正文不进注入面(INV-11)**:稳定段与场合自述不得出现日记/月记/关系卡正文;
    正文只经 `narrative_read` 按需读取。锁:`test_memory_alignment`(导航无固定日记条目)
    + `test_memory_context_signal`(场合自述无印象卡)。
13. **叙事索引只存指针(INV-12)**:`narrative_index`/`narrative_fts` 只存摘要/主题/
    场合标注/「可回味」词面(`atmosphere`,P6-1)/指针,不存正文;doc_id 对 diary/month
    为**裸文件名**(H-4 按日统一后一日一篇,`pid:` 前缀已随分身补丁移除);LLM 沙箱无该表
    写权限。
14. **观察与对话分道(INV-13)**:非对话来源一律经 `capture_observation()` 入口、带
    `source_kind`,`conversation_search` 只查 `source_kind=chat`;**出处与时效只作追溯
    (INV-14 / H-5)**:四字段不参与准入/打分/升格,不判真假;`review_after` 到期仅
    产出 `[回看]` 素材行整理叙事。

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
- **取向门**(2026-11-04):对话取向(`mode=chat`)**不装这道门**——`_plain_finish_judge`
  对 chat 直接返回 None。理由:本门守的是「oneshot 任务把没派发的畸形调用当结论、
  静默假成功」,而 chat 的纯文本收尾**就是交付物本身**,没有可被误判的「任务成功」;
  而门自身是一次不计入 `brain_calls` 的模型往返(timeout 90s),压在用户已经看完答案
  之后。本机(MuMu / 环回内核 / gitclaw2-grok-4.7)同一句闲聊实测:口播完成 → run 结束
  门内 **34.29s**、关门 **0.04s**(A/B:`runtime.long_task.finish_intent_check=false`,
  该键自 2026-11-04 起同时进 `config.CONTEXT_DEFAULTS` 与设置页「通用 → 收尾意图门」,
  不用再手改配置文件;它的**作用域只有 `task`/`auto`**——chat 在读本键之前就已返回 None,
  不存在"总闸开着但 chat 被单独关掉"的组合,因此也不为 chat 另设开关,见 §10)。
  这段尾巴在手机侧表现为「结果都出来了,■ 还占着发送位很久」。`task` / `auto` 语义不变。
- **判断方式**:`sdk_bridge._plain_finish_judge` 闭包,`llm_judge.chat_json`
  **片段提取式**问法——让模型从输出中找出所有「本应作为工具调用发出」的片段
  (无论以什么形式出现:结构化、伪标签包裹、自然语言描述的待执行步骤),
  有片段 → 不判完成,回纠正消息继续跑。
  真机实测:二分类问法对「开头像收尾 + 结尾藏伪调用」的文本会漏判,提取式不会。
- **上限**:每 run 最多纠正 2 次(`_FINISH_INTENT_MAX`),超限按原语义放行
  (有界 while 兜底,不死循环)。
- **观测**:命中落轨迹 `finish_intent_check`(verdict/fragments/chars)。

## 7. skill 轴(已剥离)

skill 不再与记忆混写,**全部内容见 `doc/plans/implemented/skill-architecture.md`**(形态、召回、消费、遗留开关、
不做项都在那里)。保留本章节号只为不打乱既有引用。

与记忆相关的唯一约束是 §1 的**三不混**:skill 产物不进记忆注入块,memory 条目不进技能回放器。

## 8. 配置总表

| 键 | 缺省 | 作用 |
|---|---|---|
| `runtime.knowledge.memory.enabled` | False | 记忆层总 gate(使用规则 + L2 维护 + L3 蒸馏 + 自管工具 + L0 索引/检索;设置页「伙伴 → 记忆」) |
| `runtime.knowledge.memory.fts_tokenizer` | trigram | FTS 分词器 `simple`(需扩展) / `trigram`(缺省降级) |
| `runtime.knowledge.memory.fts_ext_dir` | `~/.omniagent/ext/simple` | simple 扩展目录(含 `simple.dll` + `dict/`) |
| `runtime.knowledge.memory.ltm.persona` | `""` | 记忆归属人格(`''` = 中性/共享) |
| `runtime.knowledge.memory.ltm.top_k` | 5 | L1 检索条数上限(P1 起 8→5,`memory_recall` 工具的 `k` 同受钳制) |
| `runtime.knowledge.memory.ltm.inject_floor` | 0.30 | 终分下限(低于此不进工具结果) |
| `runtime.knowledge.memory.ltm.rank_weights` | relevance .55 / salience .30 / recency .15 | 融合排序权重(`salience` 项取的是 `importance` 值,P7 起无衰减) |
| `runtime.knowledge.memory.ltm.consolidation_every` | 20 | 多少次 flush 触发一次巩固 |
| `runtime.knowledge.profile.enabled` | True | 画像注入 |
| `knowledge.profile.auto_maintain` | True | 画像自动蒸馏 |
| `knowledge.profile.trigger_every_n` | 20 | 画像蒸馏触发条数 |
| `knowledge.scene.max_blocks` | 15 | L2 容量上限(三级预警) |
| `knowledge.scene.prompt_mode` | personal | L2 提炼 prompt 家族 |
| `knowledge.diary.enabled` | True | 社交日记步(巩固期写昨日第一人称日记,§2.7) |
| `knowledge.narrative.external_read_mode` | summary_only | 外部来源读印象卡只给摘要(N-10,§2.8) |
| `knowledge.narrative.we_memory.enabled` | False | P6-2 共同经历总开关(缺省关=行为与现状一致,§14) |
| `knowledge.narrative.we_memory.participants` | [] | 共同经历参与人称呼列表(取值来自配置,内核零词表) |
| `knowledge.narrative.we_memory.rhythm` | False | 表达节奏开关:聊到共同经历时「自我披露/把话递回去」分寸提示(缺省关) |
| `context.w_ctx` | 0.25 | 当前场合标注命中的加权系数(M2,×(1+w),INV-3) |
| `context.w_bg` | 0.10 | 圈层共同背景词面命中的加权系数(R8) |
| `context.context_chars` | 200 | `<current-context>` 稳定段字符预算(超预算逐级丢 topics) |
| `circle_note.enabled` | True | 语境链总开关(§2.9) |
| `circle_note.min_msgs` / `interval_s` | 12 / 600 | reflection 触发阈值(条数/间隔,按场合独立计时) |
| `circle_note.max_per_cycle` | 5 | 每巩固周期全局 reflection 上限(N-13) |
| `runtime.long_task.finish_intent_check` | True | 收尾意图门(设置页「通用」;`mode=chat` 一律不装,§6) |

> L0 逐字索引(§2.1)没有独立开关——跟着 `memory.enabled` 走;表建在 `brain.db` 内
> (`l0_messages` + `l0_fts`,与 L1 同库同分词器),无可调阈值,时间窗由工具入参
> (`days` / `limit`)决定。P7 删除的键(`forgetting_enabled` / `dormant_threshold` /
> `archive_threshold` / `stability_days`)已从缺省表移除,配置文件里残留也不再有读取方。
>
> skill 侧配置(`skill.auto_distill` 等)见 `doc/plans/implemented/skill-architecture.md` §4。

## 9. 观测与真机验证

- 轨迹:`memory_injected`(placement=**恒 system** / store=brain / hits / chars / layers ∈
  `memory_rules / scene_nav / user_profile`)、`finish_intent_check`(verdict/fragments/chars)、
  `instructions_injected`。
  (技能目录注入了什么见 `doc/plans/implemented/skill-architecture.md` §4。)
- **自取观测(P1 起步,2026-11-06 按 plan personal-agent-memory §8.3 定稿)**:
  `~/.omniagent/memory/memory_recall-YYYY-MM.jsonl`(**按月分片**)每 run 一行
  `{run_id(task_id:run_key,与轨迹 memory_injected 两源共同主键), ts, project,
  context_key, source_kind(chat/observation/task), source_external, injected_context,
  context_chars, by_tool(七工具分工具计数), calls, hits, top1_id, zero_query,
  context_hits, narrative_cross, sent, llm_calls_delta}`——七工具合计账本与
  《记忆使用规则》配额同源;`zero_query=true` 行对出"该查而没查"的 run。
  `context_key` 真实值只落本机;导出报告/标注集一律哈希或构造值。
  **谁读**:`scripts/verify_memory_effect.py` 离线读 + 本机 Debug 监控面板
  (`/api/debug/memory/metrics`,只读聚合、不提供明细查询工具);**不给 agent 查观测记录的工具**
  ——观测是量尺不是记忆。
- `memory_flush` debug 事件(L1):`extracted / kept / stored / duplicated / merged /
  conflict / dropped / applied`;同事件附带 L2 统计(`created / updated / deleted /
  renamed / empty_maintenance / reply_preview`),跳过原因也在这里:`l2_skipped_no_work`
  (本轮没产出,`did_work=False`)、`l2_skipped_no_llm`(没有可用 LLM 配置,游标不推进)、
  `l2_skipped_no_new_atoms`(游标后零增量)、
  `empty_extraction`(整理前后都没有任务块,对齐上游同名标记 → 游标不推进)。
- 调试面:`GET /api/debug/memory`——brain.db 单库转储(`atoms` 含 state/salience/
  access_count + `overview`),`POST /api/debug/memory/delete` 为唯一物理删除通道。
  L0 表(`l0_messages`/`l0_fts`)同库,直接用 sqlite 查即可;它不是调试面的固定段落。
- 自检工具:`memory_overview`(总量 / active 数 / kind 分布 / 最近条目 / 上次巩固时间;
  P7 后不再报 dormant/archived 状态分布,因为那两态已不存在)。
- **索引健康度**:`SELECT COUNT(*) FROM atoms_fts` 必须等于 `SELECT COUNT(*) FROM atoms`
  (一条一行,§5 不变量 5)。索引多于 atoms = 有陈旧副本,`verify_memory_recall.py --live`
  的满额注入率与 top1 重复率会一起抬头(2026-11-03 实测:55/37 时 0.325 与 0.361,
  重建后 0.025 与 0.219)。
- **巩固判定健康度**(`meta.last_consolidation_stats` 与 `memory_flush` 事件):
  `dedup_checked` = 本轮真送进一致性评审器的条数(2026-11-03 前该字段从未赋值、恒 0,
  只能靠 merged/conflict 反推);`merge_rejected` = 因超 `_MAX_ATOM_CHARS` 被拒的合并数,
  它涨说明模型在跨域并条(见 §5 不变量 6),该改判定提示而不是去删条目。
- 真机回归:`python scripts/verify_memory_recall.py [--live]`——词法召回断言表(2 字词 /
  标点 / 长句 / 空 query)+ **P0 分布断言**(40 条样本:满额率 <25%、top1 重复率 <20%、
  0 召回率 ≤10%、平均注入字符 150–250);
  `python scripts/verify_scene_live.py`(S1 维护 agent 循环 / S2 容量红色预警真实合并 /
  S3 意图门历史夹具 / S4 端到端注入链——含"placements 只应有 system"的 P1 断言,
  `--keep` 保留现场)。

## 10. 明确不做(现行约束)

- **厂商格式硬编码**:内核(含一切派发/解析路径)不得写死任何私有调用格式的标记
  或正则——畸形调用的识别只走 LLM 语义判断(意图门)。
- **DSML 类文本解析兜底**:绑定厂商格式,不做;意图门的纠正重试是唯一恢复通道。
- 每步尾部重发记忆的任何变体。
- **旧实现兼容/双方案分支**:架构升级即整段删除旧代码,不保留配置门控的 if/else 双路径。
- **为对话取向另设收尾意图门开关**(2026-11-04):同一根语义轴上只留一把总闸
  `runtime.long_task.finish_intent_check`(作用域 task/auto,设置页「通用」可改)。
  chat 不装门是**取向定义**而非偏好,写死在 `_plain_finish_judge` 的守卫里;再加
  `..._chat` 变体会造出「总闸开 + 子闸关 = 白付 34s 尾巴换零收益」的组合,即上一条禁止的
  双路径开关。若将来某条链在 chat 里真出现畸形调用漏纠,改法是**把那条链挪到 task/auto**
  (交付物不是纯文本就该按工作取向跑),不是给取向打补丁。
- rowfs/对象存储双路、存量迁移、heat 工程侧自增(L2 治理机制天然不需要)。
- **按通道给检索结果开自动注入特例**(2026-11-02 方案 C):私聊/群聊/API 同一规则,
  检索结果只经工具自取——见 §5 不变量 4。
  **原先登记的唯一同形例外「关系卡自动注入」已删除(2026-11-06,NP-2)**:关系卡改为
  可检索资产(§2.8),例外清单清零;`<current-context>` 是运行时处境不是记忆注入,
  不构成新特例(§5 不变量 4)。
- **salience 衰减与 dormant/archived 状态机**(2026-11-02 已删,P7):上游无此机制,
  "什么该常驻"由 L2 导航分族配额治理;淘汰 = 移出常驻,不是删除。
- **embedding 向量召回**:待 P0 打分校准后仍出现明显同义漏召再单独立项(§2.2 升级路径)。
- **删除日记 / 归档旧记录**:磁盘不是瓶颈,叙事层全量保留。
- 词面打分/语义规则化(相关性判断全 LLM)。skill 侧的不做项见 `doc/plans/implemented/skill-architecture.md` §5。

## 11. 端到端注入示例(看真实拼出来长啥样)

> 配套 §4 注入面全景:下面用一条假想任务演示「注入」到底把什么文本拼进了发给大模型的
> prompt。所有格式串均来自代码(`instructions.py` 的 `_memory_stable_block`),非示意杜撰。
> **脱敏声明:本节示例均为演示数据,不含任何真实用户/项目信息,仅用于说明注入形态。**

### 11.1 一句话说清"注入"是什么

内核在**任务开始那一刻**拼三段进 system(使用规则 + L2 索引 + L3 画像)。
模型"看到"这些文本 ≈ 知道**自己有什么记忆、去哪儿取**——但**内容不自动给它**:
L1 摘要与 L0 原文一律由模型按需调 `memory_recall` / `conversation_search` 自取(§5 不变量 4)。
**只注入一次**,后面每个块靠 `to_input_list()` 自然继承,不会每步重发(§5 不变量 1)。

### 11.2 第 0 步:磁盘上已经沉淀了什么(原料)

- **L1 原子记忆**(`~/.omniagent/memory/brain.db` 单库,SQLite + FTS5):
  ```
  atoms 表(单库,project 只是 origin 标签;state 只有 active/tombstone):
    m_a1 | fact       | importance .8 | "adb 在 Windows 上需指定完整路径 C:\...\platform-tools\adb.exe"
    m_a2 | preference | importance .9 | "用户偏好先确认方案再动手"
    ...(salience 恒等于 importance,不再衰减;工具检索时才排序)
  ```
- **L0 逐字索引**(同库 `l0_messages` + `l0_fts`,§2.1):每句话原文一行,`conversation_search` 查这张表。
- **L2 场景块**(`projects/<pid>/memory/scene_blocks/windows-环境事实.md`,注入只放索引条目):
  ```
  META: created/updated/summary/heat
  正文: <完整环境事实...>(只经 read_scene 按需读取)
  ```
- **L3 用户画像**(`~/.omniagent/memory/user_profile.md`,全局单份):
  ```
  用户偏好命令行操作;习惯先确认方案再执行;对代码注释风格有特定偏好。
  ```

### 11.3 第 1 步:组装 system prompt(稳定段,run 开始读一次、锁到任务结束)

`_memory_stable_block` 吐出这三段,拼进 system(`instructions.py` 真实格式):

```
<memory-usage-rules>
长期记忆（你与主人的事实/规则/偏好/事件）不会自动提供给你，需要时自己查：
- 必查：被问过往事件、以前的约定、人物关系、主人的相关信息时，先调 memory_recall 查过再回答，不凭印象编造；
- 要原话、具体时间、具体是谁说的：memory_recall 只有摘要或查不到时，调 conversation_search 检索原始聊天记录（关键词用 2-4 字最有效）；
- 你的经历叙事（日记/月记/对人的印象卡）同样不自动提供：被问「上次/那天/我们/某个人」时先调 narrative_search 找条目（返回日期/摘要），要读全文再调 narrative_read；
- 叙事内容只供你自己参考——不得把日记/月记/对人的印象直接转述到对外消息里，也不得把它们当成客观事实引用；
- 查不到就如实说「没有相关记录」；一个 run 内自取过往（memory_recall + conversation_search + msg_group_history + read_scene + narrative_search + narrative_read）合计不超过 3 次，其中 narrative_* 不超过 2 次，不要反复换关键词硬查；
- 与当前请求无关的记忆内容不要主动提及。
</memory-usage-rules>

<scene-navigation>
以下是可用场景记忆的索引（文件名 + 摘要）。摘要与本轮相关但信息不足时，调用 read_scene 工具（scene_name=文件名）读取全文；内容已足够时不必读。不要在无关请求中主动提及场景内容。
- windows-环境事实.md（热度 7 · 更新 2026-11-01）
  adb 在本机不在 PATH，需用 platform-tools 下的完整路径调用。
</scene-navigation>

<user-persona>
用户偏好命令行操作；习惯先确认方案再执行；对代码注释风格有特定偏好。
</user-persona>

<current-context>
context=group:123
source=chat
peer=成员甲(999)
project=qq
tone=互相起哄的分寸
topics=白米饭、剑网3
</current-context>
```

> 注意:**L2 只放索引条目**(文件名 + 热度/日期 + summary ≤120 字),正文不进常驻段
> (§5 不变量 2);**日记/月记不再固定列出**(NP-2)——叙事经 `narrative_search` 按需检索;
> `<current-context>` ≤200 字符,run 开始构造一次,无场合内容零注入(§6.1/plan M1)。

### 11.4 第 2 步:模型自取 L1/L0(工具调用,不是注入)

run 起始**没有** `<relevant-memories>`——P1(2026-11-02,方案 C)起该通道整段删除。
模型看到 11.3 的规则与索引,问到旧事时自己发工具调用:

```
模型: memory_recall(query="adb 路径", k=5)
→ search_brain 融合排序(0.55*绝对相关性 + 0.30*importance + 0.15*recency,floor 0.30,
   k≤5,glue 门槛 + 同主题去冗余 + 轮换降权)后返回条目列表
模型: conversation_search(keyword="周报", scope="current", days=30)
→ 查 l0_fts 返回 [时间] 谁：原话(外部来源锁定当前会话,不可 scope=all;只查 source_kind=chat)
模型: narrative_search(query="上次迷路那次", peer="名片(uid)", context_key="group:123")
→ 查 narrative_index 返回条目(日期/摘要/场合与对象标注,不含正文;场合只加权不过滤)
模型: narrative_read(doc_id="qq:diary-2026-10-30.md", kind="diary")
→ 读正文文件(唯一真相;外部来源按出口约束降级,N-10)
```

七工具(`memory_recall` / `conversation_search` / `msg_group_history(keyword=)` /
`read_scene` / `narrative_search` / `narrative_read`)共用一个配额账本
(合计 ≤3 次/run,narrative_* ≤2 次,run 作用域内硬拦),
run 收尾落一行 `memory_recall-YYYY-MM.jsonl`(§9)。

### 11.5 第 3 步:模型按需拉 L2 全文(同样是工具调用)

```
模型: read_scene(scene_name="windows-环境事实.md")
→ 工具返回该 .md 的完整正文(由 scene_executor.read_scene_block 读,仅此一个入口)
```

这就是「渐进披露」——索引常驻、全文按需,避免 prompt 被塞爆。

### 11.6 第 4 步:任务收尾 → 写回(闭环)

任务结束后 `flush_brain` 取走 capture buffer(消息带 `context_key` 来源标注):会话要点经
LLM 提炼 → 两段判(价值/kind/importance → 一致性四态)→ 落 brain.db(atoms 带
`context_key / source_kind` 标注);随后
`scene_executor.maybe_maintain` 取「游标之后的**一池** L1 原子增量」当进料(INV-9,
`origin` 分组已废;场合标签只作渲染元信息),
让场景 agent 循环 UPDATE/MERGE L2 块,跑完推进复合游标 `last_l2_atom_ts`
(五种不推进情形见 §2.3);
同时语境链按场合累积 `context_buffer`,到阈值跑一次 reflection 更新 `circle_note`(§2.9);
若巩固周期到期(`flush_count ≥ 20` 或距上次 >1 天),后台跑
去重/矛盾检测(FTS 近邻候选,P5)→ episode 抽象 rule → 画像重写 → 日记/月记 → 关系卡
(各步写块成功后同步 upsert `narrative_index`,幂等)→ 语境链冷启动。
下次任务开始,11.3 的索引与 11.4 的检索就能读到这些新记忆。

### 11.7 注入顺序速记

- **system prompt** = base system + AGENTS.md(纪律) + character.md(角色卡) +
  `<memory-usage-rules>`(使用规则) + `<scene-navigation>`(L2 主题块索引) +
  `<user-persona>`(L3 画像) + `<current-context>`(场合信号,≤200 字符)
- **起始 items** = 历史(P3 窗口:私聊最近 12 轮且 ≤4000 字,更早折成一行提示)→
  技能目录 top-3(见 `doc/plans/implemented/skill-architecture.md`) → 任务输入
  ——**中间没有 `<relevant-memories>`**
- **内容层** = 模型按需 `memory_recall` / `conversation_search` / `read_scene` /
  `narrative_search` / `narrative_read` 自取(合计 ≤3 次/run,narrative_* ≤2 次)
- 子任务(`is_sub`)零记忆注入(`_memory_stable_block` 首行 `if is_sub` 即返回)。

## 12. 设计依据(文献锚点与参数纪律)

> 一句话:**结构有据、参数无据**——部件形态都有文献原型,但文献常数不适配 agent 场景。
> 对策 = 初值取文献公开形态、全部配置化、dry-run 先行、用自己的分布数据定标。

| 部件 | 原型 | 关系 |
|---|---|---|
| importance(LLM 打分)+ relevance + recency 三量 | Generative Agents(2023) | 同构变体(`salience` 衰减项已于 P7 删除,排序取 importance) |
| ~~salience 衰减 + 召回强化~~ | MemoryBank(2023,Ebbinghaus 遗忘曲线) | **已删除**(P7,2026-11-02):上游无此机制、默认关闭从未启用;`access_count`/`last_access` 仍作强化簿记 |
| stability 随 `access_count` 增长 | ACT-R base-level learning(练习/间隔效应) | 随 P7 一并删除(不再按访问数拉长衰减) |
| `conversation_search` 逐字检索(`l0_conversations` 同表) | 上游 TencentDB-Agent-Memory | 直接对齐(P2,2026-11-02) |
| L2 进料 = L1 原子增量 + `updatedAfter` 游标 | 同上(`utils/pipeline-factory.ts:826`) | 直接对齐(2026-11-05;此前喂「会话要点 + 世界状态」且全池通喂,已整段替换,不留双路径)。**2026-11-06 取向修订**:逐 `buildIsolationScope` 分组属多租户隔离语义,本仓是单用户语境适配——`origin` 分组过滤已废除(一池增量,INV-9),`origin` 只作溯源;这是「上游是参考不是约束」(plan personal-agent-memory INV-7)的实例 |
| 绝对相关性 `rel/(1+rel)` + `maxResults=5` | 同上 | 直接对齐(P0/P1,替代自创的候选集排名归一) |
| tombstone + `superseded_by`(失效不删除) | Zep/Graphiti 双时间轴 | 同语义 |
| `remember / recall / update / forget` 自管 | MemGPT/Letta self-editing memory | 同集合 |
| episode → rule → persona 巩固 | CLS 互补学习系统 + CoALA episodic→semantic | 同结构 |

上游实现参照:`external/TencentDB-Agent-Memory`(L0-L3 分层、场景块、巩固周期的移植来源)。
**移植时的取舍与偏差**逐条记录在本稿 §13.1(原 2026-11-02 方案 §3,已于 2026-11-06 吸收进来),
不要在两处各写一半。

**2026-11-06 起,外部参照定位为「机制参考与出处标注」,不是验收标准**(plan
personal-agent-memory INV-7 / P7 收口):判据只能是「对单用户个人助理是否有用」,
禁止以「上游这么做 / 上游没有」作为新增或删除理由。本日起记忆框架冻结
(plan G-8):三层 + 一池 + 场合加权 + 叙事检索 + 观察进料 + 出口治理 + 验证,
不随场景增减而改骨架;plan `doc/plans/personal-agent-memory-2026-11-05.md`
是本轮改动的目标态与验收依据(实施记录见该文与 `memory-effect` 报告)。

---

## 13. 移植偏差与召回诊断留档（2026-11-06 吸收自 2026-11-02 方案）

> 原 `doc/plans/memory-recall-2026-11-02.md`（下称「2 号稿」）已于 2026-11-06 **废弃**：它是 11-02 的召回质量诊断，所识别出的偏差正是 11-05 目标态方案（`personal-agent-memory-2026-11-05.md`）的起因；结论已沉淀，再留一份并行描述只会形成第二套口径。本稿吸收其**仍有价值的三类内容**：① 移植偏差逐条对照（原 §3）；② 历史诊断与修复依据；③ 已沉淀为现行机制的设计原则。已过期的实测数字、已关闭的分期 / 风险 / 待定项不保留，需要追溯查 git 历史。

### 13.1 与上游 TencentDB-Agent-Memory 的移植偏差（逐条）

| 机制 | 原仓库 | 本仓库现行结论 | 判定 |
|---|---|---|---|
| L1 进 prompt 的形态 | 自动注入已下线，改 `tdai_memory_search` + `tdai_conversation_search` 每轮合计 ≤3 次（避免破坏 prompt cache） | L1 自动注入全局下线，`memory_recall` / `conversation_search` 工具自取，合计 ≤3 次 | **已对齐**（11-02 P1 / 方案 C） |
| 相关性分 | 单路绝对分 `rel/(1+rel)`，BM25 时 `score >= threshold` 过滤、失败 rethrow | 绝对分 `rel/(1+rel)`，`maxResults=5`；候选集线性归一已整段删除 | **实现偏差已修**（11-02 P0，不留兼容分支） |
| 向量召回 | standalone 默认 `embedding=none`；开启时 BM25∥向量 RRF(k=60) | 无；§10 明确不做向量召回臂 | 与默认一致，**不算偏差** |
| L2 注入形态 | 每条 `Path` + `热度` + `Summary`，正文按需 `read_file` | 摘要 + 分族配额（主题 ≤10 / 日记 7 / 月记 2）+ `narrative_index` 自取 | **实质区别已补齐**（11-02 P6；现行另见 §2.9） |
| 去重候选 | FTS/向量近邻 top5 + 一次 LLM 批量判 store/update/merge/skip | 整条内容作 FTS query 取近邻 top5，判定上限 8→20 | **实质降级已修**（11-02 P5；原 `content[:12]` 候选 0/33） |
| 衰减 / 遗忘 | 完全没有 decay/dormant/archive，只有 `retentionDays` 硬删（默认关） | `dormant`/`archived`/衰减状态机整段删除；`salience = importance`；保留 `tombstone` 审计链 | **自创机制已删**（11-02 P7） |
| 注入预算 | 条数 5；字符预算默认 0（不限）；`scoreThreshold 0.3` | 条数 8→5；无字符预算 | 接近 |
| 注入位置 | L3 常驻 system；L2 只给索引；L1/L0 只给工具 | 同构，另加 `<current-context>` 场合信号（≤200 字符，只影响排序不过滤） | 同构 + 单用户语境扩展 |

结论（2 号稿 §3 原判，仍成立）：**不换方案、也不整包接入原项目**——它是单会话语义、缺 peer / 群聊多人概念，整包接入要引入 Node 服务栈且群聊能力反而退化。

### 13.2 历史诊断与修复依据

- 11-02 诊断：问题不是容量（注入 1.5–2.5k 字符，不到上下文 5%），是**表现**——接不住旧事、复读高频词、远期断档。40 样本中 6–8 条满额占 20/40、0 召回 4/40。
- 四处根因：① 候选集内排名归一（第 1 名恒 1.0、`inject_floor` 形同虚设）；② 纯 FTS5 词面匹配无同义扩展；③ 远期断档（日记只露 7 篇、私聊整份 jsonl 全量转 messages 且无检索入口）；④ L2 导航只给文件名，模型判断不了该读哪篇——**比 7 篇窗口截断更早发生**。
- 11-03 复跑发现 P0 前两项超标，根因不是打分而是索引：`atoms_fts` 用 `INSERT OR REPLACE` 写入、FTS5 虚表无唯一约束 = 追加，同一条记忆背 2–6 份旧措辞（55 索引行 / 37 条 atoms，40 样本 43% 的 top_k 槽位被陈旧副本占掉）。**已修**：`_fts_upsert` 唯一写通道 + 读 `a.content` + schema 版本 `{tok}-2` 自愈重建（§5 不变量 5），修后 0.025 / 0.219 / 0.2 / 123.6。
- 同日补 merge 措辞保真度（§5 不变量 6）：`_CONSIST_SYSTEM` 要求 merge 只在同一适用域且并完双方条件与禁止项都还在，跨域返回 `novel`；`merged_content` 超 `_MAX_ATOM_CHARS` 由静默截断改**拒并**；补 `merge_rejected` 计数与从未赋值的 `dedup_checked`。语义判断仍全交模型，工程侧只给拒绝线。
- 离线复采结论：群聊口吻召回的全是社交条目、开发口吻召回工程条目；「宽 = 坏」被实测否掉（最宽那条 117 字防泄露纪律日常口吻召不回，留着），真正要拦的是跨域并条。零召回 3/7 与 P0 的 0 召回率 0.2 同根因 = 语料小 + 措辞不重叠，**不是索引脏**；不得为凑绿调阈值。
- 自取率观察：11-03 当天 152 个 run 仅 1 次自取，唯一一次该查场景链路正确（`memory_recall` 0 条 → `conversation_search` 1 条 → 如实回答没有记录）；**1/1 样本不足以判定「该查未查」风险**，留待真机 L2 / L3。

### 13.3 已沉淀为现行机制的设计原则

窗口 + 检索缺一不可（窗口管指代、检索管远期）；**淘汰 = 移出常驻，不是删除**；相关性用绝对分不用候选集排名（但控量靠 k 与去冗余，绝对分落点集中 0.5–0.6 挡不住好坏）；**检索结果永不自动注入**（L1/L0 一律工具自取、每轮合计 ≤3 次、不按通道设特例）；分层术语 L0-消息（`s_*.jsonl`）/ L0-叙事（日记，LLM 产出但不进注入面）/ L2 只放抽象层。以上均已写入本稿对应章节与代码，本处只留依据。

> 2 号稿的 P0–P7 分期、验收数字、风险清单与待定项均已关闭或过期，不在此复列；需要追溯查 git 历史。

---

## 14. P6 增量与内隐分级（H-5 / H-6,2026-11-07 实施,plan personal-agent-memory §16）

> plan §16 是本节的目标态依据；本节描述**现状**。P6 是独立增量期,不改变 §5 的 14 条
> 不变量,也不使 plan 的 NP-3+P3 / P4 自动签收（H-3 仍有效）。

### 14.1 统一进料道与内隐的尺子（H-5 原则,目标态口径）

- **所有来源同一个入口、同一条叙事线**:群聊里别人说的话、我们一起做的事、她刷到的
  内容、查到的资料、工具产出——都是「发生在她身上的事」,经 `capture_observation()`
  （非对话）或会话原话（对话）进同一池、同一篇日记;不建兴趣库/学习库/可信知识库。
- **按用途分层,不按来源分级**:不设准入式可信度评分 / 来源白名单 / 升格门槛;
  `source_kind / observed_at / provenance / review_after` 四字段**只作追溯标注**
  （「这见闻打哪来的、什么时候的」）,不参与任何准入/打分分支（INV-6/INV-14）。
  聊天轮 `allow_atoms=False` 的理由是「那些是经历、不是约定」,不是「来源不可信」;
  只有主人明确交代、要照办的约定进事实层（`memory_remember`）。
- **但保留人那样的内隐分级**:看 Nature 与随手刷 wiki 分量不同——这把尺子从她自己的
  经历里长出来（重复接触 / 有没有被推翻过 / 别人的反应 / 当时是随手刷还是认真查）,
  **不落成字段、不由主人标注、不进检索打分**,只在表达时体现为确信度与措辞
  （「我看到个说法」vs「这个我比较确定」）。出口按**表达形态**治理（§14.4 of plan）:
  允许第一人称见闻,禁止冒充客观断言;`wrong_context_rate` / `cross_context_leak_rate`
  仍为 0。

### 14.2 P6-1 经历质地（2026-11-07 已实施）

- 日记素材三类增补:当日观察见闻行（`[见闻]`,N-17）、写法层的触发物/心境规则、
  至多 1-2 处带说话人的原话引用——同一篇、一次 LLM、≤900 字、不进 atoms（§2.7）。
- `narrative_index.atmosphere` 可选词面列（H-8：直接写在 DDL，无补列/迁移
  补列;`narr_fts_schema_version` 升 `-2` 自愈重建回填）,使触发物/心境可检索。
- 锁:`test_memory_diary.py`(素材含 `[见闻]`、纯观察日成篇、atmosphere 进 META/索引
  词面且脏值退化空)、`test_memory_brain.py`。

### 14.3 P6-2 共同经历与表达节奏（2026-11-07 已实施,缺省关）

- 日记 / 关系卡 META 增 `we:` 参与人标注:素材里出现
  `knowledge.narrative.we_memory.participants`（配置,内核零词表,INV-4）中的人名才标;
  不建 We 库、不做决策机制、不产主动闲聊、不进 atoms。
- 表达节奏开关 `knowledge.narrative.we_memory.rhythm`（缺省关）:开启时稳定段
  `<memory-usage-rules>` 末尾追加一句「自我披露 / 把话递回去」的分寸提示;
  **关闭时稳定段逐字节不变**（锁:`test_memory_alignment.py::test_we_memory_rhythm_switch_default_off`）。

### 14.4 P6-3 主人可见 / 可控 / 可遗忘（2026-11-07 已实施）

- 本地入口 `backend/api/routers/memory_api.py`（挂 `/memory/*`,backend 默认只绑本机
  回环,不新增远端）:`GET /memory/search`(事实检索,带 record_id)、
  `GET /memory/narrative`(叙事条目,不含正文)、`GET /memory/narrative/read`(叙事全文)、
  `POST /memory/atoms/{id}/correct`(纠正:旧条 tombstone(superseded_by=新条)+新条入库)、
  `POST /memory/atoms/{id}/forget`(遗忘:state→tombstone,reason 留痕)。
- **删除走既有 tombstone 软删**:不物理删、不改写 `content`、审计可溯;删除后
  `atoms_since`（L2 进料）不回吐（`state='active'` 过滤,既有机制）。
- 观测记录（memory_recall-*.jsonl）是量尺不是记忆,**不在本入口暴露**（§8.3）。
- 锁:`test_memory_api.py`（查看/纠正/遗忘全流程、422/404、软删内容不改、L2 不回吐、
  叙事条目无正文+按需读全文）。

### 14.5 外部能力「干净引入」五门禁（C-1..C-5,现状未引入任何第三方包）

复用既有 `omni_core/tools/loader.py`（插件包=plugin.json+plugin.py,删目录零痕迹、
单包失败整包回滚、`enabled` 开关）与 `omni_core/tools/capability.py`（按名注册/取能力,
未注册返回 `None` 不抛错）,不新造接入层、不改派发链路。**当前没有任何第三方能力包
被引入**——门禁先固化,选型待具体需求出现时逐条过:

| 编号 | 门禁 |
|---|---|
| C-1 | 内核零点名:`omni_core/**` 不得 import 第三方能力包（只在 `plugins/<name>/` 内） |
| C-2 | 可选依赖:不进 requirements 主链;未装/未启用时 `get_capability()` 返回 None,消费方跳过、主流程不断 |
| C-3 | 数据契约不外泄:第三方不得直连 brain.db / l0_messages / 日记正文,只经适配器 DTO |
| C-4 | 可拔除:删目录或 enabled=false 即零痕迹,不改历史数据、不留迁移门 |
| C-5 | 出口与隐私不变:第三方产出仍走表达形态治理,四字段只作追溯,不落真实标识 |

禁止:外部记忆框架作存储层/第二套记忆;trust score / 来源白名单 / 向量召回臂 /
数值衰减 / 情绪或依恋模型。允许:嵌入/重排、时序与触发物抽取、评估辅助、渠道采集器
（P5）——都只是能力,不参与「记不记得」的判定。
