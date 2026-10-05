# 技能发现层缺陷追踪 · search_skill 词面匹配失效

> **状态**：🟡 定案待实施（2026-10-05 定案：**方案 1 切词计分 + 方案 1b 目录总数提示**组合实施；
> 方案 3 目录扩容维持不做）。修复后归档至 `doc/plans/implemented/`。
> 红线沿用：方案必须通用，不得加入场景词表或业务规则。

## 1. 来源与现象

2026-10-04 Rmiworld 真机验证（task `t_1514c53a6d5e`，运行中轨迹
`2026-10-04_2352309b5a11.jsonl` step=9）：

- 模型调用 `search_skill("殖民地管理 工作优先级 开局")` → `{"total": 0, "hits": []}`；
- 项目内实际存在目标技能 `colony-management`（24 个 project 技能之一）；
- 模型随后按项目 AGENTS.md 点名直接 `load_skill("colony-management")` → 成功返回完整
  playbook 正文（step=13，`total_uses` 簿记 +1 正常）。

## 2. 根因

`omni_core/tools/skill_tool.py` `search_skill()`（约 :155）的打分是**整串子串匹配**：
把用户 query 原样作为一个子串，在 `name / description / tags / tools` 里做
`q in x` 计分。自然语言整句（多词、带空格、中文连续短语）几乎不可能作为完整子串
命中任何字段 → `total: 0`。

匹配对齐「§8.2-1 选档交模型、不做词面猜测」的原则本身没问题——问题只在
**query 预处理缺失**：没有把整句拆成可命中的词元。

## 3. 影响面

技能发现有三条路，本缺陷废掉其中一条：

| 发现路径 | 现状 |
|---|---|
| 技能目录（`<available_skills>`） | 只外显 top-3（`_CATALOG_MAX_ITEMS = 3`，效用序） |
| `search_skill` 关键词检索 | **整句查询必空（本文档追踪的缺陷）** |
| 项目 AGENTS.md 点名 + `load_skill` 按名加载 | ✅ 已验证可用（当前实际主力路径） |

**「生产出来的 skill 查找也会有问题」**：`auto_distill` 提取的新宏
（默认关，未验证）同样只有词面可发现性。当目录外显名额被早期
技能占满、且 AGENTS.md 尚未点名新 skill 时，新 macro 对模型事实上不可发现——自动产出
越多，该缺陷的边际代价越大。

**2026-10-05 再证**（task `t_0c95387c870b`，Rmiworld 23 技能）：目录照常注入 top-3
（addiction-management / animal-husbandry / base-building），但模型实际加载的
`research-management` / `colony-management` 均为**目录外技能**（靠 AGENTS.md 点名/
先验得知）；全程未调 `search_skill`。目录外技能的可发现性完全依赖点名路径。

## 4. 候选方案（按侵入度排序）

1. **查询切词计分（✅ 定案实施）**：query 按空格/标点切分成词元，
   任一词元命中即计分（命中字段加权沿用现有 name×3 / tags×2 / desc×1 / tools×1），
   多词元命中累加排序。零新依赖、改动收敛在 `search_skill` 内。
   （中文无空格切分引入 jieba 暂不做——双语料下「按标点/空格切 + desc 含词元子串」
   预期已够；实测不够再评估。）
2. **LLM 检索**：复用 §8.2-1 选档思路，小模型把整句 query 映射到技能名短名单。
   成本高、多一次调用，仅当切词方案实测仍不够时升级。
3. **目录扩容联动**：若发现层长期不修，评估调大 `_CATALOG_MAX_ITEMS` 或按
   project/global 分组外显全部技能名（上下文预算换发现率）。**维持不做**——
   目录化设计（T2.4）的本意就是控制注入预算。
4. **方案 1b · 目录总数提示（✅ 定案实施，须在方案 1 之后）**：
   `format_skill_catalog_message` 渲染末尾加一行机制提示：
   「技能库共 N 个技能，目录仅展示相关的 3 个；需要其他能力时用 search_skill 检索」。
   N 为脚本可算的簿记，零词表零语义。单独实施无意义（search_skill 坏时提示无出口）。

## 5. 验收用例

1. 对现有 24 个 project 技能（Rmiworld），用 ≥5 条自然整句中文查询
   （含「殖民地管理 工作优先级 开局」这条回归样本）→ 目标技能进入 `hits` top-3；
2. 英文技能名直查（如 `killbox`）不回归：仍能命中 `killbox-design`；
3. 无关查询（如「今天天气」）不强行凑数：允许返回 0 命中；
4. `search_skill` 现有调用方（模型工具面）schema 不变——只改内部打分，
   不改参数与返回结构；
5. 目录提示行：`format_skill_catalog_message` 输出含「共 N 个」且 N 等于
   `build_skill_catalog` 全量枚举数（外显 cap 之前）；
6. 单测落 `tests/`（mock 技能目录，隔离 home 夹具沿用 conftest）。
