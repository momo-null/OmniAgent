# 记忆层现行架构

> 本文是**记忆层**的唯一事实源:分层机制(L0→L3)、注入面、不变量、意图门、配置与观测、验证纪律。
> 代码落点:`omni_core/memory_brain.py`(L0 缓冲 + L0 逐字索引 + L1 单库 + L3 画像 + 巩固周期)、
> `omni_core/local/task_store.py`(消息落库处同步写 L0 表)、`omni_core/local/scene_executor.py`(L2 机制与触发薄壳)、
> `tools/scene_tool.py`(read_scene)、`tools/memory_tool.py`(agent 自管工具 + `conversation_search`)、
> `local/loop/instructions.py` + `sdk_bridge.py` + `brain/sdk_loop.py`(注入与意图门)、
> `backend/api/routers/helpers.py`(私聊上下文窗口)。
> 真机回归:`python scripts/verify_memory_recall.py`、`python scripts/verify_scene_live.py`。
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
| 内容 | 事实 atoms(L1) + 场景块(L2) + 用户画像(L3) + 逐字会话索引(L0) |
| 存储 | `~/.omniagent/memory/brain.db`(L1 atoms + L0 `l0_messages`/`l0_fts` 同库) + `projects/<pid>/s_*.jsonl`(L0 文件真相) + `projects/<pid>/memory/scene_blocks/` + `scene_index.json`(L2) + `~/.omniagent/memory/user_profile.md`(L3) + `~/.omniagent/memory/relations/`(关系卡) |
| 生产者 | LLM 提炼(价值判 + 一致性四态) / 场景整理 agent / 巩固周期(去重·抽象·画像重写) |
| 消费者 | 注入(§4 注入面,**只到索引/规则层**)+ 模型自取工具(`memory_recall` L1 / `conversation_search` L0 / `read_scene` L2) |

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
不是独立索引库)。列:`record_id / chat_id / session_id / kind(chat|task) / role /
speaker / text / recorded_at`——`kind` 区分通道消息与任务输入(同为 `role=user`,
不区分就会把派单提示词当聊天旧事命中);`record_id` 由 (会话, 时间, 角色, 文本) 派生,
所以回填与实时写入天然幂等。jsonl 仍是文件真相,表只是可重建的检索副本。
消费端 = `conversation_search(keyword, scope, days, limit)` 工具(§4);`msg_group_history`
带 `keyword` 时转查同一张表。首次上线由服务/网关启动发起一次后台回填
(`memory_brain.start_l0_backfill`,meta 标记保证只跑一次;实测 1161 行 / 1.3s)。
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
  **project 不再是分区键**,只作 `origin` 溯源标签;人格维度是 `persona` 列
  (`''` = 中性/共享,按激活角色卡过滤)。

| 列 | 语义 |
|---|---|
| `kind` | fact / preference / rule / episode —— 语义标签(曾用于衰减速率,P7 起衰减已删) |
| `importance` | 值不值得长期保留(LLM 写入时打分,基本不变) |
| `salience` | **恒等于 `importance`**(P7 起不再衰减;留列只为审计与既有读接口) |
| `access_count` / `last_access` | 召回强化簿记 |
| `state` | `active` / `tombstone` —— 只有这两态(P7,2026-11-02 删掉 dormant/archived) |
| `superseded_by` | 被更正时指向新条(审计链) |
| `peer` | 关系对象标注(「名片(uid)」解析,§2.7;空 = 无对象/自己) |

- **写链**:`flush_brain` → 会话要点 digest(LLM 提炼,**宁缺毋滥**,单次 ≤5 条) →
  **第一段判**(价值/一次性过滤 + `kind` + `importance`) → **第二段判**(一致性四态
  `novel / duplicate / merge / conflict`;候选 = 各自 top-3 的并集,一次批量 LLM) → 落库 + FTS 同步
  (**同一条恒为一行**,`_fts_upsert` 唯一写通道——见 §5 不变量 5)。
  LLM 失败一律保守保留(宁重复不丢失);`duplicate` 只强化旧条,`conflict` 旧条 tombstone 并入库新条。
- **读链**:`search_brain(query, persona, peer, k)`——`_match_expr` 切词(simple 模式按 2 字滑窗短语 OR,
  trigram 模式 3-gram OR + 2 字词 LIKE 兜底)→ `state='active'` → 排序(P0 + P7,2026-11-02):
  ```
  rel     = bm25_rank_to_score(rank) = (-rank)/(1+(-rank))   # 绝对分,不在候选集内排名归一
  final   = 0.55*rel + 0.30*importance + 0.15*exp(-days/180)
  peer 命中 → ×1.25(加权不过滤);  在 _recent_ring 里 → ×0.85(只降权不排除)
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
- **注入形态(P6,2026-11-02 对齐上游)**:导航段每条渲染成 **`文件名 + 热度/日期 · 更新时间 + summary`**
  (照抄上游 `scene-navigation.ts:113-115` 三行式;摘要截 120 字)——M-1 时代"只给文件名"零信息量
  (`diary-<日期>` 这种名字模型不会去读),已整段替换。全文仍只经 `read_scene` 工具读取。
  **分族配额**在 `scene_executor.load_scene_nav`:主题块 ≤10(heat 降序)/ 日记最近 7 篇(日期倒序,
  heat 恒 0 不参与排序、渲染成日期)/ 月记最近 2 篇 = 总 19 条,字符预算 ≈1.8k。
  "最近一周发生了什么"直接用这 7 篇日记的 summary 当索引——**不再单独提炼周叙事**(P4 已取消)。
- **带外信号**:整理 agent 输出 `[PERSONA_UPDATE_REQUEST]` → 写入 brain `meta`
  (`persona_update_request`)→ 下次巩固**强制**画像重写(绕过条数节流),reason 并入证据。
- **日记主题族(`diary-<日期>.md`,2026-11-01)**:巩固期**内核直写**(不走维护 agent,
  维护 agent 对其只读不可见,清理/归一化豁免)——第一人称「她的一天」;独立配额不占
  `max_blocks`;导航三分 = 主题块 heat 前 10 + 月记近 2 + 日记近 7(§2.7)。
  **分层口径(P6,2026-11-02 定)**:日记属 **L0-叙事**(日粒度原料,全量保留、可回读、
  本体不进注入面),L2 只索引它的 summary;正文长度从 ≤450 字放宽到 **≤900 字**——
  不再受注入预算约束,只受 `read_scene` 单块读取上限约束。
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
  统计落 `meta.last_consolidation_stats`。
  (P7 后不再有"重算 salience"和"状态降级"两步。)
- **意义**:库不靠条数增长,靠抽象层级上升(episode → rule → persona);日记承载叙事层。
  **P5(2026-11-02)**:去重候选原先取 `content[:12]` 精确短语,33 条 active 实测 0 条命中
  (=去重没开),改为整条内容作 FTS query 取近邻 top5、判定上限 8→20;构造的同义不同措辞
  矛盾对可被合并/作废。
  > 注:本仓库的 L0/L1/L2 分层与上游 `external/TencentDB-Agent-Memory` 的取舍与偏差
  > 逐条记在 `memory-recall-2026-11-02.md` §3(哪些丢了、哪些是自创),不在两处各写一半。

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
  日记日期为 UTC 日期,UTC 日界=本地 08:00,恰在安静时段边缘,「日记的一天」≈ 本地
  08:00 起的一天(**待办:统一切本地日**——2026-11-03 用户定,后续连月记/检索时间窗
  一起改,不单点改日记)。
- **素材组装(v3)**:跨会话文件合并(逐文件覆盖赋值=丢素材,已修);全局尾窗 160 行 ∪
  **每会话保底 10 行**,ts 排序、硬顶 400 行——机械层只保证每个会话「被看见」,
  写不写归意义判(纯寒暄不写、多对象/场景各有笔墨、≤450 字、白卷正确)。
- **身份锚(登记制,core 零写死)**:core 提供 `note_self_names/self_names` 通用注册表,
  渠道拿到自己高置信身份(登录昵称/各群群名片/uid)时上报,换账号/多渠道随连接自愈、
  并集成立;泛别名(撞名群泛称)**不登记**。日记 prompt 动态注入锚句 + 记录结构规则
  (`[assistant]`=自己、行首标注=说话人、引用行非说话人)——她不被自己名字带偏。
- **月记(`diary-month-<月份>.md`)**:只压已结束月份、≥3 篇日记才压,META.summary =
  当月关键人物/事件地图;导航恒有界(主题块 heat 前 10 + 月记近 2 + 日记近 7,§2.3)。
- **消费**:导航清单(知道有)→ 被点名/波次升格 run 的自述指路 → `read_scene` 按需
  精读;**T1 判定层零消费**(零 LLM 红线);服务轴任务 run 不注入;写入侧自消费
  (旧日记喂新日记与关系条目抽象)。

### 2.8 关系卡(她"记得某个人",2026-11-01)

- **存储**:`~/.omniagent/memory/relations/<uid>.md`——**全局一份、按人存**。人是跨场景的
  (同一个群友可能既在群里聊又私聊过来),按群/项目拆会把"对他的印象"分裂成分群机器人。
- **结构**:META(`name` / `uid` / `updated`)+ 正文三行:**最近交手 / 他对我的态度 /
  我对他的态度**。
- **素材**:`_peer_lines()` 从会话记录按 uid **跨项目聚合**,只认带来源标注的 user 行
  (`名片(uid): 文本`);每人 ≥3 条往来才写卡,每轮最多更新 5 人(成本有界)。
- **注入**:被某人 @(T0)或私聊时,场合自述带 `[关于这个人的印象(你自己的记忆)]`,≤200 字;
  **无卡零注入**(不给空壳,避免模型自己编印象)。
- **与 L1 的分工**:关系卡 = "这个人的三行印象"(按 uid 索引、随时可读);L1 的 `peer` 条目
  = "关于他的一条事实"(参与检索与抽象)。同源(都来自来源标注),不互相替代。
- **规模治理(未定)**:当前**无上限、无 TTL**(一人一文件,永久保留)。候选方案 = 只保留
  半年内有往来的卡、冷卡移入 `relations/archive/`(不进注入、仍可查),先观察实际人数再定。
- **下游用法(人格落地)**:印象只改"距离",具体怎么表达由角色卡决定。`character.md` 里写
  这只鲸鱼的态度映射(被骂 → 短句、不解释、不接梗;被夸 → 得瑟一句但不松口;被套话 → 反问)。
  **这是示例,不是硬约束**——换一个"被骂就骂回去"的人格,只改角色卡,通道与记忆层零改动。
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
| 1 | `<memory-usage-rules>` 《记忆使用规则》(必查场景 + 一个 run 自取合计 ≤3 次) | system prompt 稳定段 | run 开始读一次、run 内锁定 | `runtime.knowledge.memory.enabled` |
| 2 | `<scene-navigation>` L2 索引(文件名 + 热度/日期 + summary ≤120 字) + `read_scene` 指引 | system prompt 稳定段 | 同上 | `runtime.knowledge.memory.enabled` |
| 3 | `<user-persona>` 用户画像全文 | system prompt 稳定段 | 同上 | `runtime.knowledge.profile.enabled` |
| 4 | `read_scene(scene_name)` 读单块全文 | 工具调用结果 | 模型按需 | 恒可用(纯读) |
| 5 | L1 自取 `memory_recall`(k ≤5) | 工具调用结果 | 模型按需(受 ≤3 次/run 规则约束) | `memory.enabled` |
| 6 | L0 逐字自取 `conversation_search` / `msg_group_history(keyword=)` | 工具调用结果 | 模型按需(同一账本) | `memory.enabled` |
| 7 | **L0 对话窗口**(IM 最近 **12 条** / Web 私聊 **最近 12 轮且 ≤4000 字** / 群上下文 **8 条**) | 起始 items 历史段 / 群场合自述 | run 开始一次 | 无(通道固定) |

> 第 7 行是 L0 的**消费侧**,三处口径:`gateway/dispatch.py:303` 的
> `history_items[-(12 if explicit else 40):]`(IM 固定条数,防长会话塞满 prompt)、
> `gateway/channels/napcat.py:266` `_GROUP_CTX_COUNT=8`(群消息上下文)、
> `backend/api/routers/helpers.py:477 _window_messages`(P3 私聊窗口化:按**轮**计窗、
> 切在轮边界、窗口外折成一行「更早的 N 轮对话已折叠,需要时用 conversation_search 查」;
> 配套 `chat_runtime.py:64 _MODEL_HISTORY_RECORDS=240`,不再每请求解析整份 jsonl)。
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
`memory_rules / scene_nav / user_profile`)、`sdk_bridge` 于指纹计算**之前**并入 system
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
4. **记忆检索结果永不自动注入(2026-11-02 定)**:L1 召回 / L0 查询只经工具
   (`memory_recall` / `conversation_search`,每轮合计 ≤3 次)进入上下文;通道只注入
   **消息上下文**(场合自述、群窗口、来源标注)与 **L2/L3 索引**——记忆全局,不按通道/场景
   做注入特例。锁:`memory_injected` 层列表不得含 L1;新增注入面前先过本条。
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
| `runtime.long_task.finish_intent_check` | True | 收尾意图门 |

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
- **自取观测(P1)**:`~/.omniagent/memory/memory_recall.jsonl` 每 run 一行
  `{ts, project, source(T0/T1/private/api), calls, hits, top1_id, zero_query}`——
  `calls` 是 `memory_recall` + `conversation_search` + `msg_group_history(keyword=)` 的合计
  (与《记忆使用规则》的 ≤3 配额同一账本);`zero_query=true` 行对出"该查而没查"的 run,
  配合轨迹人工评。
- `memory_flush` debug 事件(L1):`extracted / kept / stored / duplicated / merged /
  conflict / dropped / applied`;同事件附带 L2 统计(`created / updated / deleted /
  renamed / empty_maintenance / reply_preview`)。
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
- rowfs/对象存储双路、存量迁移、heat 工程侧自增(L2 治理机制天然不需要)。
- **按通道给检索结果开自动注入特例**(2026-11-02 方案 C):私聊/群聊/API 同一规则,
  检索结果只经工具自取——见 §5 不变量 4。
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
- 查不到就如实说「没有相关记录」；一个 run 内自取过往（memory_recall + conversation_search + msg_group_history）合计不超过 3 次，不要反复换关键词硬查；
- 与当前请求无关的记忆内容不要主动提及。
</memory-usage-rules>

<scene-navigation>
以下是可用场景记忆的索引（文件名 + 摘要）。摘要与本轮相关但信息不足时，调用 read_scene 工具（scene_name=文件名）读取全文；内容已足够时不必读。不要在无关请求中主动提及场景内容。
- windows-环境事实.md（热度 7 · 更新 2026-11-01）
  adb 在本机不在 PATH，需用 platform-tools 下的完整路径调用。
- diary-2026-11-01.md（更新 2026-11-01）
  今天主人让我把周报整理成表格，群里的演示稿也被我一句话带过了。
</scene-navigation>

<user-persona>
用户偏好命令行操作；习惯先确认方案再执行；对代码注释风格有特定偏好。
</user-persona>
```

> 注意:**L2 只放索引条目**(文件名 + 热度/日期 + summary ≤120 字),正文不进常驻段
> (§5 不变量 2);分族配额 = 主题块 heat 前 10 + 月记近 2 + 日记近 7,总字符预算 ≈1.8k(P6)。

### 11.4 第 2 步:模型自取 L1/L0(工具调用,不是注入)

run 起始**没有** `<relevant-memories>`——P1(2026-11-02,方案 C)起该通道整段删除。
模型看到 11.3 的规则与索引,问到旧事时自己发工具调用:

```
模型: memory_recall(query="adb 路径", k=5)
→ search_brain 融合排序(0.55*绝对相关性 + 0.30*importance + 0.15*recency,floor 0.30,
   k≤5,glue 门槛 + 同主题去冗余 + 轮换降权)后返回条目列表
模型: conversation_search(keyword="周报", scope="current", days=30)
→ 查 l0_fts 返回 [时间] 谁：原话(外部来源锁定当前会话,不可 scope=all)
```

三个自取工具(`memory_recall` / `conversation_search` / `msg_group_history(keyword=)`)
共用一个配额账本,run 收尾落一行 `memory_recall.jsonl`(§9)。

### 11.5 第 3 步:模型按需拉 L2 全文(同样是工具调用)

```
模型: read_scene(scene_name="windows-环境事实.md")
→ 工具返回该 .md 的完整正文(由 scene_executor.read_scene_block 读,仅此一个入口)
```

这就是「渐进披露」——索引常驻、全文按需,避免 prompt 被塞爆。

### 11.6 第 4 步:任务收尾 → 写回(闭环)

任务结束后 `flush_brain` 取走 capture buffer:会话要点经 LLM 提炼 → 两段判
(价值/kind/importance → 一致性四态)→ 落 brain.db;随后
`scene_executor.maybe_maintain` 让场景 agent 循环 UPDATE/MERGE L2 块;
若巩固周期到期(`flush_count ≥ 20` 或距上次 >1 天),后台跑
去重/矛盾检测(FTS 近邻候选,P5)→ episode 抽象 rule → 画像重写 → 日记/月记 → 关系卡。
下次任务开始,11.3 的索引与 11.4 的检索就能读到这些新记忆。

### 11.7 注入顺序速记

- **system prompt** = base system + AGENTS.md(纪律) + character.md(角色卡) +
  `<memory-usage-rules>`(使用规则) + `<scene-navigation>`(L2 索引) + `<user-persona>`(L3 画像)
- **起始 items** = 历史(P3 窗口:私聊最近 12 轮且 ≤4000 字,更早折成一行提示)→
  技能目录 top-3(见 `doc/plans/implemented/skill-architecture.md`) → 任务输入
  ——**中间没有 `<relevant-memories>`**
- **内容层** = 模型按需 `memory_recall` / `conversation_search` / `read_scene` 自取(≤3 次/run)
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
| 绝对相关性 `rel/(1+rel)` + `maxResults=5` | 同上 | 直接对齐(P0/P1,替代自创的候选集排名归一) |
| tombstone + `superseded_by`(失效不删除) | Zep/Graphiti 双时间轴 | 同语义 |
| `remember / recall / update / forget` 自管 | MemGPT/Letta self-editing memory | 同集合 |
| episode → rule → persona 巩固 | CLS 互补学习系统 + CoALA episodic→semantic | 同结构 |

上游实现参照:`external/TencentDB-Agent-Memory`(L0-L3 分层、场景块、巩固周期的移植来源)。
**移植时的取舍与偏差**逐条记录在 `memory-recall-2026-11-02.md` §3(哪些丢了、哪些是自创),
不要在两处各写一半。
