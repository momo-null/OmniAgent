# Memory RAG 设计（检索增强注入 · 仅 memory 轴）

> 状态：设计稿（2026-10-04，用户定案：**这套只针对 memory，skill 轴严格排除在外、两轴正交是硬约束**）。
> 触发前不实施；触发条件见 §0.2。参照系 = 腾讯开源 **TencentDB Agent Memory**（下称 TAM，
> `Tencent/TencentDB-Agent-Memory`，MIT，本地 SQLite 部署形态）。

---

## 0. 背景与触发条件

### 0.1 现状与崩坏点

- 现行注入（TAM 移植，`omni_core/memory_tam.py`）：项目库 + 全局库两库合计 ≥20 条时
  检索段生效（FTS5 trigram 双路按分合并）；画像 `user_profile.md` 常驻；技能目录另行注入。
- 池子小的时候成立；hachimi 实测**池子 1 条时排序无差别**（检索无增益）。崩坏点在池子
  增长后：2000 字符装不下全部相关内容，常驻注入的召回率开始崩——这时候才需要 RAG。

### 0.2 触发条件（量化，满足其一即启动切片 1）

1. project `fact_index.json` 条数 + 全局 MEMORY.md bullet 数 **合计 > 100**；
2. 或 LLM 选档出现可观截断（注入块长期顶满 2000 且有相关事实被预算挤出）。

按 digest 定参（≈20 facts/活跃日），1–2 周活跃使用即可能触发。

### 0.3 硬约束（用户定案，2026-10-04）

- **两轴正交**：本设计**只索引 memory 栈**——L1（`rollouts/<tid>.md` 的 facts/lessons）、
  L2（`projects/<pid>/memory/MEMORY.md`）、L3（全局 `MEMORY.md`）。
- **skill 轴严格排除**：`substeps` / playbook / routine 标签一律**不进任何索引**；
  skill 轴的发现（注入目录 / `search_skill`）与消费（`replay_skill`）不经过本设计的
  任何环节。检索基础设施可以共用工程常识，但索引内容、存储位置、消费路径三不混
  （对齐 tam-porting-map.md 零重叠验收口径）。
- 语义判断全 LLM、脚本只做簿记的红线**不变**：检索只做**候选生成**（排序是机制不是
  语义判断），终审仍是 §8.2-1 的 LLM 选档。检索参数（k、RRF 常数）是机制参数，
  不构成 hachimi 意义上的"阈值写死语义"。

---

## 1. TAM 是怎么做的（参照系，2026-10-04 实测其仓库与 API 文档）

### 1.1 架构与存储

- **分层**：L0 Conversation（原始对话全文，核对原文/时间戳/来源用）→ L1 Atom（提取的
  事实/偏好/约束/事件，带 `episodic / persona / instruction` 三类型标注）→ L2 Scenario
  （按项目/场景组织的知识块）→ L3 Core/Persona（长期画像）。另有 Skill / LLM-Wiki /
  CodeGraph 三类资产（我们只参照 ChatMemory 侧；skill 轴 Omni 已独立成轴）。
- **存储**：**默认本地 SQLite**（MongoDB 为实验性后端），FTS5 全文检索，向量检索可选，
  Hybrid Search 可选；所有中间产物**人类可读**。
- **数据流**：写入成功后**异步**触发 L1 提炼管道（`notifyPipeline`）；归档阈值在服务端
  （`archived.reason ∈ {tool_calls, bytes, compressed, oversize}`——存在字节/压缩阈值，
  未公开具体默认值）；提炼 `max_iterations 1–64`。

### 1.2 检索与注入（本设计主要参照面）

| 机制 | TAM 做法 | 出处 |
|---|---|---|
| 分层检索 API | L0 `/v3/conversation/search`（limit 默认 5、上限 100，支持时间窗）；L1 `/v3/atomic/search`（同 limit，支持 type 过滤） | v3 API 文档 |
| 检索模式显式化 | skill search 的 `mode` 是一等参数：`bm25 / embedding / hybrid` 三态任选 | v3 API 文档 |
| 混合融合 | **BM25 + 向量 + RRF** 融合，按需回落 L1/L0 | README |
| 注入预算 | listing 生成 `<available_skills>` 块用 `char_budget`（0–64000，**默认 8000**） | v3 API 文档 |
| 三预算 | 结果受**条数 / 字符 / 超时**三类上限约束（参数值未公开） | README |
| 召回哲学 | 常驻 L2/L3 **恢复语境**，涉及具体约束再检索回落 L1/L0 | README / 设计文 |
| 头条收益 | 上下文卸载 + 任务画布 ⇒ **token −61%** | 宣传口径，未复现 |

### 1.3 本设计抄什么、不抄什么

- **抄**：SQLite + FTS5 起步（零重依赖）；检索模式显式三态（而非绑死 hybrid）；三预算；
  常驻恢复语境 + 检索回落分层；产物人类可读。
- **不抄**：团队 ACL / 可见性四级（单用户产品不需要）；Skill/LLM-Wiki/CodeGraph 资产
  全家桶（Omni 的 skill 轴已独立且有零重叠红线）；Mermaid 任务画布；云向量库
  （TCVDB）——TAM 自己的默认形态也是本地 SQLite，证明这套机制不需要重型基础设施。

---

## 2. Omni 设计

### 2.1 注入结构：常驻段不动 + 检索段新增

```
任务启动 → load_memory_text(query, brain_cfg)
  ├─ 常驻段（机制不变）：全局 MEMORY.md（人级核心 + digest）+ 画像   ← TAM「恢复语境」
  └─ 检索段（新增）：query = 任务目标 + 最近用户消息
       → 候选生成：BM25(FTS5) top-k₁ ∪ 向量(本地 embedding) top-k₂ → RRF 融合 → ~30 条
       → LLM 选档终审（§8.2-1 现有机制，候选从"全量"变"检索出的 30 条"）
       → 三预算打包 → 注入块检索部分
```

关键点：**RAG 改变的是候选生成，不改变判断归属**。池子 ≤ 触发阈值时检索段关闭，
行为与现状逐字节一致（§2.5 降级阶梯保证）。

### 2.2 索引布局（SQLite，每 project 一个库）

```
projects/<pid>/memory/index.db
  ├── fts        （FTS5 表，trigram tokenizer 中文可用：fact_id, text）
  ├── vectors    （fact_id, embedding BLOB, model, dim）    ← 向量臂切片启用
  └── meta       （fact_id, source_task, ts, scope, indexed_at）
全局层：memory/index.db（同一 schema，索引全局 MEMORY.md bullets + L3 摘要）
```

- **写时索引**：`distill_task_memory` / merge 落 fact → 写 meta + FTS →（开关开时）embed
  → upsert vectors。单条失败不阻断入库（事实照存，`indexed_at` 留空）。
- **惰性回填**：curator 空闲轮扫描 `indexed_at IS NULL` 补索引——与 digest/profile 同模式。
- **效用不进索引**：A5 效用分查询时 join `fact_stats`，效用更新**不需要重嵌入**。
- **规模判断**：fact_slot_cap 200 + 全局数百条 → 暴力余弦 <10ms，**不需要 FAISS /
  向量数据库**（TAM 默认形态同理）。

### 2.3 embedding 选型（个人数据不出机 = 硬要求）

- 本地 llama-server 挂 embedding 模型（bge-m3 / gte 小模型档，`/embeddings` 端点），
  项目已有本地模型生命周期管理可复用。
- **排除**一切云 embedding API——记忆是用户个人数据，出机即越线。
- 向量列带 `model` 标记；换模型 = 整库重嵌入（惰性，curator 慢速回填）。

### 2.4 三预算（对齐 TAM 口径，初值待校准）

| 预算 | 初值 | 说明 |
|---|---|---|
| 条数 | 检索段 top 8–10 | 候选池 30 条 → LLM 选档后 ≤10 进注入 |
| 字符 | 检索段 ~1000 | 与常驻段分摊现有 2000 总额；常驻段相应让位 |
| 超时 | 150ms | 检索超时 → 该段回退现行为，注入主链路不断 |

### 2.5 降级阶梯（永不因检索失败断注入）

```
hybrid（FTS5 + 向量 + RRF）
  → embedding 模型不可用 → BM25-only
  → FTS 不可用 / 池子 ≤ 触发阈值 → 现状行为（常驻两段 + 原序，逐字节一致）
```

无 brain 时同样降级：检索照跑（纯簿记排序），LLM 选档跳过、按效用序取预算内条目——
与 §8.2-1"无模型退化"同款语义。

### 2.6 不做什么（用户定案 2026-10-04）

1. **不给 skill 建任何索引**——substeps / playbook / 标签不进 FTS、不进向量；
   skill 轴发现与消费不经本设计（两轴正交硬约束，§0.3）。
2. **不上云向量库 / 云 embedding API**（个人记忆不出机）。
3. **不写检索阈值语义规则**——检索只排序（BM25 分 / 余弦 / RRF 是机制），
   相关性判断归 LLM 选档。
4. **效用治理不搬家**——A5 台账、fact_slot_cap 降级机制照旧，检索只是新增候选入口。
5. **不建团队 ACL / 可见性分级**（TAM 的四级 ACL，单用户产品不需要）。
6. **不做 Mermaid 画布 / 上下文卸载全家桶**——只取检索增强这一刀。

---

## 3. 落地切片与验收用例

| 切片 | 内容 | 验收用例 |
|---|---|---|
| **S1 FTS5 先行** | index.db + 写时 FTS + 检索段接入 load_memory_text；零模型依赖 | 池子 150 条时注入块含 query 相关事实且不含噪声头部；无 FTS 时行为与现状逐字节一致；中文 query（trigram）命中 |
| **S2 向量臂** | llama-server embedding + 写时向量 + 余弦 top-k | 同义改写 query（BM25 漏、向量中）能进候选池；embedding 服务不在 → 自动降 BM25-only，注入不中断 |
| **S3 RRF 合流 + 三预算** | 融合 + LLM 选档接候选池 + 条数/字符/超时预算生效 | RRF 合并去重正确（k=60）；超时注入 <200ms 回退；LLM 选档输入 ≤30 条（不再全量） |

每步独立可回退、独立可验收；S1 完成即获得"池子大了找得回"的下限收益。

## 4. 待定决策点

- embedding 模型档位（bge-m3 全功能 vs 更小快模型）——待 S2 时按本机资源定。
- 检索段与常驻段的字符分摊比（初值 1000/1000）——按真机注入质量校准。
- 全局层（L3）是否也进检索段：v1 建议只索引 bullet 级（digest 条目有人工同名行），
  画像不进索引（走常驻）。
- 检索段 query 的构成（任务目标 only vs + 最近用户消息）——S1 用任务目标起步。
