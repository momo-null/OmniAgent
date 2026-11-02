# skill 轴现行状态(2026-11-02)

> **定位:skill 是一般模块,不是能力叙事。** 自动生成链路(蒸馏/宏缓存/晋升/淘汰)已弃用,
> 配置恒关、代码保留但不再维护;现行只有**人工手写 SKILL.md** 一条链路。
>
> 与记忆层的关系:两条正交的轴,**索引内容、存储位置、消费路径三不混**——skill 文件不含
> 纯事实陈述、不进记忆注入块;memory 条目不进技能回放器。记忆层事实源见
> `doc/plans/implemented/memory-architecture.md`(本稿只在需要划界时引用它)。

## 1. 现行机制

| 维度 | 内容 |
|---|---|
| 形态 | 手写 `SKILL.md`(Hermes 风:frontmatter + 正文) |
| 存储 | 全局 `~/.omniagent/skills/`(跨 project 共享)+ task 私有 `~/.omniagent/tasks/<task_id>/skills/`;`projects/<pid>/skills/` 为旧位置 |
| 召回 | `build_skill_catalog`(`omni_core/local/knowledge_inject.py`)合并两层、task 优先,`_CATALOG_MAX_ITEMS = 3`(目录 top-3,摘要 ≤300 字) |
| 消费 | run 开始注入**目录摘要**(正文不进常驻块)→ `load_skill` 按需拉正文 → `search_skill` 检索 → `replay_skill` 回放(substeps 硬重放) |
| 生产者 | **人工维护**。自动蒸馏/晋级链路已关闭 |

## 2. 为什么弃用自动生成(2026-10-30 定位调整)

自动晋级与记忆**共用同一套 LLM 提炼**:产物无论贴 `skill` 还是 `memory` 标签,消费端最终都是
prompt 注入。该需求现由记忆轴承载(L1 检索注入 + L2 场景块 + 角色卡),双标签冗余。

实测支撑:蒸馏链 5/5 成功、`entry_id` 正确,但**注入技能形态零步数收益**(Δsteps=0 负观测);
唯一非 prompt 的收益来自 `replay_skill` 回放(跳过每步 LLM 决策),而它同时引入执行风险耦合。

**渊源**:晋升机制最初借鉴 Hermes 的 Background Curator,借鉴时点早于记忆层成为成熟品类。
将来若记忆面出现明确缺口,**优先扩记忆而非复活 skill 自动线**。

## 3. 遗留代码与开关(保留但不再作为能力)

| 项 | 状态 |
|---|---|
| `skill.auto_distill` | **恒 False**(总闸);打开也无人消费其产物叙事 |
| 自动宏提取 / 宏缓存晋升 / 淘汰(`skill.evict_*`) | 代码保留,参数未标定,无真机数据 |
| `replay_skill` 回放执行器 | 保留可用;收益验证口径必须是"实际调用 replay_skill 的硬重放",不得用"注入技能序列"当治疗臂 |
| 技能有效性 A/B 叙事 | 已停止;不论有效与否都不作能力宣称 |

遗留在 `Backlog.md` 的「skill 宏轴 · 有效性验证」条目仍挂着两个前置问题(回放口径的 A/B、
场景锚定确定性工具链),**触发条件未到就不动**——连续 2 个任务集 B 不优于 A 即停手。

## 4. 配置与观测

| 键 | 缺省 | 作用 |
|---|---|---|
| `skill.auto_distill` | False | 自动宏提取总闸(恒关) |

- 轨迹字段:`skill_catalog`(本次注入了哪几条目录摘要)、`load_skill` / `replay_skill` 的工具调用记录。
- 观测口径:技能只在"被 load / 被 replay 且任务成功"时算有效,注入本身不算收益。

## 5. 明确不做

- 自动宏提取、宏缓存晋升 / 淘汰参数调优;
- skill 跨域自动晋升、知识型技能机械转录;
- 把 skill 产物塞进记忆注入块(违反三不混);
- 以"注入了技能"论证能力提升(历史负观测已证 Δsteps=0)。
