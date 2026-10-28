# 长期记忆方案：从「按项目隔离的库」到「一个会遗忘的大脑」（定稿 2026-10-28）

> **定位（2026-10-28 已落地）**：实现见 `omni_core/memory_brain.py`；**现行实现的事实源是
> `memory-architecture.md`**，本文只保留「设计依据 / 参数标定纪律 / 验收指标 / 明确不做」，
> 不重复实现细节。旧双库 L1 与旧 `memory_tam.py` 已整段删除，不存在双方案分支。
> 解决的问题：project 隔离 + 只增不减 + 只有内核被动写，导致 agent 不像"一个人"，像"一堆项目的索引"。
>
> 落地进度：Phase 0-3 已落地（simple 分词 / 两段判四态 / 自管工具 / 巩固含画像重写）；
> Phase 4（语义召回）未立项。取代 `unified-chain-and-voice-2026-10-28.md` §4.5 的 M-3/M-4；
> M-1（scene 注入收紧）已落地；M-2（多角色卡）保留独立。

## 0. 一句话结论

把「两库三层的分区式记忆」改成**单库一脑 + 强度衰减 + 主动自管**：
记忆只有一份（不分项目，只记来源与人格归属），每条带 **importance（写入时判）** 与
**salience（随时间衰减、被召回则强化）**，agent 通过 `remember / recall / update_memory /
forget` 自管（内核代理落盘），内核在巩固期做合并、抽象、归档。
**遗忘是状态降级（active → dormant → archived）或墓碑化，不是物理删除**，保证可审计、可回生。

### 用户定稿时的两条修正

1. **旧记忆不迁移**：brain.db 空库起步；旧库（`projects/<pid>/memory/` + 全局库）**原样封存只读，
   一个字节不动**。蒸馏纪律修复后，有用记忆会随新会话重新沉淀出干净版；
   急用的规则走场景层（read_scene）或显式 remember。
2. **遗忘参数不作真理**：全部常数进配置、可调；**两段式上线**——先 dry-run
   （salience/access 照算，不执行降级），跑出真实分布后人工确认阈值再开
   `forgetting_enabled`。「30 天后 dormant+archived 占比 30-60%」降级为「先观测分布再定标」。

### 文献锚点（结构有据、参数无据的对策）

| 部件 | 原型 | 关系 |
|---|---|---|
| importance（LLM 打分）+ salience（指数衰减）+ relevance 三量 | Generative Agents（2023）：recency=0.995^小时 + importance 1-10 + relevance | 同构变体 |
| salience 衰减 + 召回强化 | MemoryBank（2023）：Ebbinghaus 遗忘曲线 + 召回增强 | 公式形状同源 |
| stability 随 access_count 增长 | ACT-R base-level learning（练习/间隔效应） | 简化版 |
| tombstone + superseded_by（失效不删除） | Zep/Graphiti bi-temporal（事实失效打时间戳） | 同语义 |
| remember/recall/update/forget 自管 | MemGPT/Letta self-editing memory | 同集 |
| episode→rule→persona 巩固 | CLS 互补学习系统（1995）+ CoALA episodic→semantic | 同结构 |

**风险定位：结构有据（文献原型），参数无据（文献常数不适配 agent 场景）**——
对策 = 初始值取文献公开形态、全部配置化、dry-run 先行、用自己的分布数据定标。

## 1. 结构性缺陷（历史记录：旧双库已删除，保留作设计动机）

| # | 缺陷 | 状态 |
|---|---|---|
| A | 分区而非一体（双库贯穿读写） | 本设计解决（Phase 1） |
| B | 只增不减（无 importance/salience/state） | 本设计解决（Phase 1-3） |
| C | 写入纯被动（agent 不能主动记/查） | 本设计解决（Phase 3 工具） |
| D | 无巩固/无抽象（同规律多种说法并存） | 本设计解决（Phase 3 巩固） |
| E | 中文双字词召回恒 0（trigram 长度门槛） | **已修**（切词分类 + LIKE 兜底）+ Phase 0 换 simple 根治 |
| F | 标点即整条查询失效（静默吞） | **已修**（引号化 + 降级） |
| G | 语义判断造假事实（蒸馏把系统内部机制当事实落库） | Phase 0 蒸馏纪律（类别边界 + 禁因果推断） |
| H | scope 标注漂移（人格/私有内容落全局层） | 单库 + persona 维度后自然消失（Phase 1） |

## 2. 存储布局与 schema v2

```
~/.omniagent/memory/brain.db     # 唯一记忆库（SQLite + FTS5 simple 分词）
~/.omniagent/memory/user_profile.md   # L3 画像（保留现有位置与语义）
~/.omniagent/ext/simple/         # simple 扩展（simple.dll + dict/，MIT）
archive/                         # 归档层（archived 条目导出 jsonl 分片）
```

**project 不再是分区键**，只是 `origin` 溯源标签；`projects/<pid>/memory/` 封存只读。

```sql
CREATE TABLE atoms(
  record_id   TEXT PRIMARY KEY,
  content     TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'fact',   -- fact | preference | rule | episode
  persona     TEXT DEFAULT '',                -- 归属人格（'' = 中性/共享）；按激活角色卡过滤
  origin      TEXT DEFAULT '',                -- 来源 project_id（仅溯源，不作分区）
  source_task TEXT DEFAULT '',
  importance  REAL NOT NULL DEFAULT 0.5,      -- 0~1，写入时 LLM 打分，基本不变
  salience    REAL NOT NULL DEFAULT 0.5,      -- 0~1，动态强度
  access_count INTEGER NOT NULL DEFAULT 0,
  last_access TEXT DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'active', -- active | dormant | archived | tombstone
  superseded_by TEXT DEFAULT '',              -- 被更正时指向新条目
  version     INTEGER NOT NULL DEFAULT 0,
  created     TEXT NOT NULL,
  updated     TEXT NOT NULL
);
CREATE VIRTUAL TABLE atoms_fts USING fts5(content, record_id UNINDEXED, tokenize='simple');
CREATE INDEX idx_atoms_state ON atoms(state, salience DESC);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
```

`kind` 决定衰减速率（§3.2），`state` 决定是否可注入（§3.3），`persona` 决定按激活人格过滤。

## 3. 遗忘机制

### 3.1 两个量，不混

- **importance**：值不值得长期保留。语义判断，LLM 写入时打分，基本不变。
- **salience**：此刻多"热"。簿记计算，访问/巩固时重算。决定注入优先级与降级。

### 3.2 衰减（ACT-R 简化）

```
S(kind)      = stability_days：rule 90 / preference 120 / fact 30 / episode 5（配置项）
stability    = S(kind) × (1 + ln(1 + access_count))
salience(t)  = clamp(importance × exp(-days_since_last_touch / stability), 0, 1)
last_touch   = max(updated, last_access)
```

召回即强化：被 inject/recall 命中 → access_count+1、last_access=now → 衰减变慢。
**参数全部配置化（`knowledge.memory.ltm.*`），初值取文献形态，dry-run 后按分布定标。**

### 3.3 状态机（遗忘 = 降级，不是删除）

```
active ──(salience<dormant_threshold 且 access_count=0 且 age>30d)──> dormant
active/dormant ──(salience<archive_threshold 且 age>90d)──────────> archived（导出 archive/*.jsonl）
任意 ──(矛盾取代 / forget)────────────────────────────────────────> tombstone（superseded_by + 原因保留）
dormant/archived ──(重新命中 / 遗忘召回)──────────────────────────> active（回生）
```

- dormant：不进注入，recall 显式 include_dormant 可检到
- archived：默认检索不见，显式 include_archived 才见
- tombstone：永不参与检索，保留 content + superseded_by + 原因（可审计、可追溯）

### 3.4 检索排序（参数配置化）

```
relevance = 按本次候选集排名归一（首位 1.0、末位 0.5）
            （原设计写 1-normalized_bm25；实测 bm25 量级 ~1e-06，直接当 [0,1] 用会让
             relevance 权重形同虚设、低 importance 的精确命中被 inject_floor 误杀）
recency   = exp(-days_since_last_touch / 180)
final     = 0.55*relevance + 0.30*salience + 0.15*recency
```

只取 `state='active'`；dormant 仅显式召回。`final ≥ inject_floor(0.30)` 才进注入面，
`top_k = 8`。**去掉 `_INJECT_TRIGGER=20` 的冷启动门槛**——按"有没有命中"决定。

## 4. agent 自管理（行为权归 agent，落盘权留内核）

新增 `tools/memory_tool.py`（注册进 TOOL_REGISTRY，走现行 S2 审批链）：

| 工具（实际注册名） | 签名 | 语义 |
|---|---|---|
| `memory_remember` | (text, kind='fact') | 显式记一条；importance 内核默认 0.8，巩固期重评 |
| `memory_recall` | (query, k=5) | 主动检索（含 dormant）；命中即强化 |
| `memory_update` | (record_id, new_text) | 更正：旧条 tombstone + superseded_by |
| `memory_forget` | (record_id, reason) | 主动遗忘 → tombstone |
| `memory_overview` | () | 总量 / 状态分布 / kind 分布 / 最近条目 / 上次巩固时间 |

remember 与内核 flush 走同一条落盘与判重路径（消除时延与"手改 md"现状）。

### 写入两段判（修 G 类假事实的纪律）

```
候选事实
  ├─ 价值判（LLM）：值得长期记？否 → 丢弃（一次性操作/寒暄/主观感受）
  ├─ kind 归类：fact | preference | rule | episode
  ├─ importance 打分 0~1
  ├─ 一致性判（LLM，检索 top-5）：duplicate → 强化旧条 / merge → 合并 /
  │   conflict → 以新为准旧条 tombstone(superseded_by) / novel → 新增
  └─ 落库 + FTS 同步
```

**蒸馏纪律（两处 prompt 都加）**：
- L3 画像：只记用户本人事实/偏好/习惯/背景；assistant 应对策略/拒绝规则/边界设定/
  对用户行为的模式判定条款一律不入画像（带外「优先处理」信号同样过滤）；
- L1 提炼/价值判：**禁止产出对系统内部机制的因果推断**（「系统为什么这样/被拒的原因/
  策略如何」），只记「发生了什么、观察到什么事实」。

## 5. 巩固周期（consolidation，"睡觉时整理"）

触发：每 20 次 flush / 距上次 >24h / agent 显式调 `memory_consolidate`。
**后台单线程队列执行，不挂任务收尾路径**；步骤 0 = `brain.db → archive/backup-<ts>.db` 备份。

```
1. 备份
2. 全库去重（有界：近段更新条目 vs 各自 top-5 候选，扩展 _decide 语义）
3. 抽象提升：同主题 episode ≥3 → LLM 提炼 1 条 rule；episode 降权（有界：单次 ≤N 条）
4. 矛盾检测：冲突以新为准，旧 tombstone
5. 强度重算：全部 active 重算 salience
6. 状态降级：**仅 forgetting_enabled=true 时执行**；dry-run 只落统计
7. 画像重写：复用 L3 维护（输入换 brain.db 新 atoms）
8. 健康报告：consolidation_stats 落轨迹 + memory_overview 可查
```

## 6. 实施阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 0 | simple 分词器集成（加载/jieba_dict 绝对路径/重建回填/双模式查询）+ 蒸馏纪律 prompt | 已落地 |
| Phase 1 | brain.db 单库 + schema v2（含 persona 列）+ 写链/读链切换（无门控分支；旧库与旧代码已删除） | 已落地 |
| Phase 2 | 注入面切换：scene 文件名清单 + 排序融合 + 零命中观测 + 遗忘 dry-run | 已落地 |
| Phase 3 | 自管工具 + 巩固周期（含画像重写；遗忘执行 gated off） | 已落地（阈值待定标） |
| Phase 4 | 语义召回（本地 embedding + RRF，个人记忆不出机） | 未立项 |

## 7. 不变量（沿用并新增）

1. 语义判断全 LLM，脚本只做簿记（importance/kind/判重/抽象 LLM；衰减/状态/排序公式）。
2. 记忆文件只由内核写：agent 经 memory_tool 提交意图，内核落盘（S1 红线不变）。
3. 遗忘不是删除：state 降级或 tombstone；物理删除仅人工 delete_atom 通道。
4. 任何异常不得打断主流程：记忆层失败静默降级（沿用）。
5. 注入面硬上限：stable 段与 dynamic 段分别有字符预算，超预算按 salience 截断。
6. 单一事实源：brain.db 唯一可写记忆；scene_blocks / persona 为派生抽象层
   （**派生重建待 consolidation 验证后才切换，此前 scene 维持现状**）。
7. **旧库清除**（2026-10-28 用户改判，取代原「封存只读」）：旧 L1 库（`projects/<pid>/memory/`
   与全局库）、旧 L2 场景块、旧 L3 画像**直接删除、不备份**；旧 L1 代码同步整段删除，
   仓库内不保留双方案分支。
8. **遗忘两段式**：dry-run（只算不降级）→ 分布数据确认 → 开 forgetting_enabled。

## 8. 验收指标

> **状态（2026-10-28）：以下四项均未测，属待办。** 现有 `scripts/verify_memory_recall.py`
> 只覆盖词法召回回归（断言表全 PASS），不覆盖 hit@8 / 假事实率 / 遗忘分布 / 强化有效性。

| 指标 | 定义 | 目标 |
|---|---|---|
| 召回回归 | verify_memory_recall.py 断言表（双模式） | 全 PASS |
| hit@8 召回率 | 标注集（Phase 2 建标）命中率 | ≥ 0.85 |
| 假事实率 | 抽检判定「系统内部机制推断/未观测因果」 | < 5%（且新增为 0） |
| 库体积 | active 条数增长 | 收敛（靠巩固抽象） |
| 遗忘分布 | dormant+archived 占比 | 先观测，再定标 |
| 强化有效性 | 高 access 条目存活率 vs 零访问组 | 显著更高 |

## 9. 明确不做

- 不做项目级物理分区（origin 只是标签）；不做用户手动编辑为常规路径；
- 不做实时向量检索（Phase 4 独立立项）；不做"永不遗忘"保底；不做跨机/云端同步。
