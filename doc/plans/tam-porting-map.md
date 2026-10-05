# 知识层现行设计：memory 轴（TAM 移植）+ skill 轴

> 两轴正交是硬约束：索引内容、存储位置、消费路径三不混。
> memory 轴设计稿 = `doc/plans/memory-rag-design.md`；本文是两轴现状事实的唯一索引。

## 1. 两轴总览（零重叠口径）

| 维度 | memory 轴 | skill 轴 |
|---|---|---|
| 内容 | 事实 atoms + 用户画像 | 知识型技能（playbook）+ 可重放宏（substeps） |
| 存储 | `projects/<pid>/memory/index.db` + `~/.omniagent/memory/index.db` + `user_profile.md` | `projects/<pid>/skills/` + `~/.omniagent/skills/` |
| 生产者 | LLM 提炼（scope=global/project 标注）→ 判重 → 落库 | 人工维护 + 自动宏提取（`skill.auto_distill`） |
| 消费者 | 注入检索段（两路 FTS）+ 画像常驻 | 目录 top-3 + `load_skill` / `search_skill` / `replay_skill` |
| 晋升 | LLM 标注 scope 入全局库 | 升 global 仅人工「设为全局」；N=3 candidate→active |

验收口径：store / 内容 / 消费者 / 晋升四分离——skill 文件不含纯事实陈述、不进注入块；memory 条目不进回放器。

## 2. memory 轴 · TAM 移植映射

> 依据：对 `external/TencentDB-Agent-Memory`（MemoryCore/src，TypeScript）的源码精读。

### 2.1 TAM 本地形态（核实）

- **单文件 SQLite** `{dataDir}/vectors.db`：L0 对话表 + L1 原子记忆表 + FTS5 + 向量（可选）。
- **人类可读层**：`records/<date>.jsonl`（L1 真相源）、`scene_blocks/*.md`（L2）、`persona.md`（L3）。
- **FTS5 是 jieba 预分词 + unicode61**（写侧分词空格连接、查侧分词去停用词 OR 连接）；不是 trigram。Omni 用 trigram（中文可用、零分词依赖），但吸收其两列法（索引列 + 原文列）与 schema 版本写 meta 表（FTS5 不支持 ALTER，换 schema 只能 drop+重建+回填）。
- L2/L3 不进 SQLite：场景块由 LLM agent 改 markdown、画像增量重写 persona.md——Omni 不搬执行器，只搬治理机制。

### 2.2 写入链（核实）

- 触发：每 5 轮对话（warmup 翻倍封顶 5）+ idle 兜底 + 串行队列；提炼超时 180s、单次入库上限 20。
- 提炼 prompt 三任务合一（情境切分 + 类型化提取 + JSON 输出），「宁缺毋滥 / 独立完整 / 归纳合并」，负面清单排闲聊；`<think>` 剥离 + JSON 修复是 thinking 模型必需。
- 判重两阶段：①无 LLM 候选召回（FTS/向量 topK=5+新条数；库空→整批直接 store）；②单次 LLM 统一池批判，四态 **store/update/merge/skip**，merge/update 必填 merged_content 且 version=max+1；**失败语义：LLM 失败/解析失败/漏判/非法 action 一律 fallback store（宁重复不丢失）**。

### 2.3 读链（核实）

- 候选池扩 `limit*3`，超取后内存过滤再截断。
- 混合检索：FTS 臂 ∥ 向量臂 → RRF(k=60) 融合；单臂降级；任一臂异常非致命。
- bm25 归一 `relevance/(1+relevance)`，门槛 0.3。
- 注入预算出处是 auto-recall 钩子：maxResults=5 / 字符预算（码点安全截断）/ 超时整体跳过（主链路不断）/ scoreThreshold=0.3。
- 注入结构 = 常驻段（画像，system 末尾）+ 动态段（`<relevant-memories>` 包裹）。

### 2.4 移植决策与实施状态

| TAM 机制 | 决策 | 状态 |
|---|---|---|
| SQLite 单库 | ✅ 每项目一个 `memory/index.db` + 全局库；裁剪 ACL 列 | ✅ 已实施（含全局层，2026-10-05） |
| FTS5 | ✅ trigram；吸收原文列 + schema 版本 meta + 不可用降级旗标 | ✅ 已实施（S1 纯 FTS） |
| L0 表 | ❌ 不建：Omni 会话 jsonl 已是 L0，buffer 直读 | — |
| 提炼 prompt / JSON 解析防御 | ✅ 移植裁剪；提炼时 LLM 顺带标注 **scope**（global=用户偏好/跨项目规律；未标注按 project 保守处理） | ✅ 已实施 |
| 四态判重 + fallback store | ✅ 搬，并三态（update 并入 duplicate） | ✅ 已实施 |
| 写入节流 | ✅ 简化：任务收尾触发（run 结束即检查点） | ✅ 已实施 |
| RRF / bm25 归一 / 0.3 门槛 | 向量臂（S2）时启用 | ⏸ 未做 |
| 三预算 + 超时跳过 | 条数 5 / 字符 / 超时 | 部分（条数已定） |
| L2 场景 agent / L3 persona 执行器 | 治理机制可借鉴，执行器不搬；画像自动维护以 `memory_tam._maybe_maintain_profile` 落地（gate `knowledge.profile.auto_maintain`） | ✅ 画像已实施 |

### 2.5 Omni 落点（实际实现）

- **实现 = 单模块 `omni_core/memory_tam.py`**（atoms 表 + FTS5 trigram；项目库与全局库同 schema，`runtime_paths.global_memory_db()`）。
- 写链：`capture`（`task_store.append_message` 后）→ `flush`（`chat_runtime` run 收尾）：LLM 提炼 → scope 分库（global 入 `~/.omniagent/memory/index.db`，跨项目可见；project 入项目库）、判重在各自库内做；全局库打不开只写项目库不阻断。
- 读链：`inject_text`（`loop/instructions.py` F4.2 记忆检索段）：**两库合计 ≥ 20 条**才注入；项目库 + 全局库双路 FTS 检索按分合并取 top-5，`<relevant-memories>` 包裹。
- gate = `runtime.knowledge.memory.enabled`；任何失败静默降级（白卷 / 零注入 / 不产出），绝不打断主流程。

## 3. skill 轴（现行设计）

### 3.1 身份与晋升

- **身份**：`entry_id = sha1(归一动作序列)[:16]`。归一白名单（方向经实测校准）：
  - `text / content / query / input / command / url` → `<text>`；`path` → `<path>`
  - 坐标（`x/y/x1/y1/x2/y2`）→ 保留键名、去值
  - 结构键保留原值：`resource_id`、`view_id`、包名、`key/keycode/code`
  - **其余字符串值 → `<text>`**（默认易变：`task_done(reason=…)` 措辞每次不同，保守保留会让晋升永不命中）
- **晋升**：`promote_or_insert` 身份优先合并（entry_id → name → objective_pattern 全等兜底）；`success_count` **累计**（失败不清零，只累加 `failure_count`——不构成双罚），满 3 → active。
- **跨域**：升 global 仅人工 `promote_to_global`（project 副本移除避免遮蔽）；跨域自动晋升不实现代码。

### 3.2 两种形态（正交）

| | 知识型技能 | 宏缓存（过程性） |
|---|---|---|
| 产出 | LLM 提炼：`routine`（≤12 字动宾短语）/ `description` / `playbook`（引导型文字，非动作录像） | `substeps` = 轨迹**原始 args** 结构化提取（`substeps_from_trajectory`） |
| 身份 | 无 entry_id（手写）或归一哈希 | 归一哈希（缓存键语义：同序列=同宏，精确匹配是特性） |
| 消费 | 注入目录 + `load_skill` 文字引导 | `replay_skill` 回放执行器；**不进注入块** |

**硬禁令**：知识型技能不得从执行轨迹自动转录、不得以 `objective[:40]` 写死规则命名；LLM 提炼也不得产出录像式步骤。

### 3.3 自动蒸馏（`skill.auto_distill`，默认关）

- **触发**：任务收尾（`loop/finish.py::_finish` 写完 run record 后）→ `skill_library.maybe_distill_skill`。
- **双轨**：步骤 = 轨迹结构化提取（原始 args）；标签 = LLM（routine/description）。
- **降级链（宁严勿松，任一命中即不产出）**：无 brain / 任务未成功 / executed < 2 步 / 标签 LLM 失败 / 防幻觉校验不过（`validate_skill_summary`：routine ≤12 字、playbook 长度有界、正文引用的工具名 ⊆ 实际执行集）。
- 开关关闭：蒸馏不产出、晋升不触发；已有技能的检索/加载/人工维护不受影响。

### 3.4 发现与消费

| 机制 | 做法 |
|---|---|
| **常驻目录** | 技能目录 top-3（LLM 选档；无模型按 success_count 效用序），仅摘要；top-1 附归一化动作序列摘要。**正文不进常驻块** |
| **按需加载** | `load_skill(name)`：有 playbook 返回文字引导，否则回退脱敏动作序列；命中即记 `total_uses` |
| **检索** | `search_skill(query, limit)`；现状缺陷与修复方案见 `skill-search-redesign.md` |
| **回放** | `replay_skill`：白名单硬重放（`skill.replay_allow_tools`）；GUI 坐标类降级「参考建议」；S0/S2 照常生效 |
| **缓存淘汰** | `SkillLibrary.evict_stale`：candidate TTL 14d / active 闲置 30d 且 confidence<0.5；移入 `_archive/` 降级不删；手写技能永不淘汰。触发点待接（Curator 退役后暂无生产者） |

**选档纪律**：相关性判断交模型，不做词面打分；无模型时保持原序、不做相关性判断。

## 4. 明确不做

- ❌ 知识型技能的机械转录；宏缓存不在禁令内
- ❌ skill 跨域自动晋升
- ❌ 语义规则化（词面打分、关键词表、场景启发式）
- ❌ skill 轴与 memory 轴共用索引/消费路径
- ❌ 存量迁移与兼容分支

## 5. skill 宏轴有效性验证

- 主指标：**回放命中后任务步数节省**（对比无 skill 基线），非注入提升。
- B 组命中必须 **execution-verified**（回放真省步才算命中）；「检索到/注入」不算命中。
- 对照：A=无 skill；B=skill 回放；同任务集交错执行。
- 负结果纪律：连续 2 个任务集 B 不优于 A 即停手。
