# 团队模式设计（Team Mode）— skill 三级渐进 + 常驻角色 agent

> 状态：**设计稿（未实施）**。排期在 chat-single-path-fix 之后。
> 日期：2026-09-17
> 核心理念：core 零场景零任务零知识（redline 不变）；操作封装在 tool/MCP；任务知识在 skill。代码里基本没有配置和具体任务设定，一切由 skill 和 tool/MCP 驱动。

---

## 1. 场景与动机

**驱动场景**：模拟经营类长任务（如文明游戏：种田 / 军队 / 外交）——主 agent 协调多个领域子 agent，子 agent 随任务出现、任务内常驻（记忆延续）、任务结束销毁沉淀。

**现状差距**：当前只支持"匿名 worker 一次性扇出"（dispatch → Send 并发 → 聚合回灌）。子 agent 无身份、无角色记忆、无定向派发。

## 2. 三层分离（架构第一性原理，用户原始设计意图）

| 层 | 承载 | 现状 |
|---|---|---|
| **core（引擎）** | 执行 / 编排 / 门控，零场景零任务零知识 | ✅ redline 测试守护 |
| **tool/MCP（能力）** | 操作封装（游戏操作 → MCP server：查经济 / 建军队…） | ✅ M3/M5 已落地 |
| **skill（知识）** | 任务知识：角色定义 / 范围 / 模型引用 / 策略 / 操作说明 | ⚠️ **短板**：现状仅 substeps 回放 |

换任务 = 换 skill + 换 MCP，core 不动。

## 3. skill 三级渐进 schema（第一原则：所有结构化字段可选）

**skill = 领域知识的持久化载体。形态从纯文本到结构化定义渐进；消费方式由内容形态决定，core 对 skill 内容零假设。**

| 级 | 内容 | 消费方式 | 现状 |
|---|---|---|---|
| **L0 纯文档** | 就是一段操作说明（如"代码推送与提交"步骤 + 规范） | recall 命中 → 内容注入主 agent 上下文，它读了照做 | ❌ 缺此路径（现状命中后只能回放 substeps） |
| **L1 操作序列** | + `substeps[]` | 确定性回放 / 或作为参考 | ✅ 现状 |
| **L2 角色定义** | + `role / scope / model / tools / knowledge` 段 | 建团素材（本设计核心） | ❌ 待建 |

L2 角色定义示例（参考 CrewAI agents 文件格式，但 markdown 化）：

```markdown
# skill: 文明游戏 · 外交官
role: diplomat
scope: 负责一切对外谈判、同盟、宣战判断；关注邻国关系与威胁评估
model: executor              ← 逻辑名，不写 endpoint（可移植性红线）
tools: [game.economy, game.units]   ← 能力偏好（可选）
knowledge: |
  谈判前先查己方军力对比；同盟优先级：强邻 > 边缘国 …
```

**模型引用必须用逻辑名**（brain / executor / vision / provider 注册名），由 config 解析到实体。skill 管知识（这个角色该用哪类模型），config 管基础设施（逻辑名指向哪个 endpoint）。

## 4. 团队模式架构

```
任务 + 初始角色文档（可选，普通输入）
  ↓
主 agent（大脑）检索 skill 角色库 → 调 spawn_team(roles=[...])
  （duty/模型引用来自 skill L2；名单怎么组合、上几个角色 = LLM 判断）
  ↓ core 实例化（逻辑名→provider 解析，L2 门控照挂：task_done/verify/escalate）
主 agent 定向派发：dispatch(items=[{"agent": "diplomat", "desc": "..."}])
  ↓ 角色调 game MCP 工具操作；共享黑板（WorldModel 分区 view(scope)）
角色常驻（任务内，各自滚动摘要记忆）· 结果回灌主 agent
  ↓ 任务结束
销毁 → Curator 提炼角色经验 → skill knowledge 段进化（复用现有 Meta-loop / N=3 晋升门）
```

三个关键约束（红线对齐）：
1. **LLM 生成职责，不选模型**——spawn 只含 name + duty + 模型逻辑名引用；不做模型实时路由（N6 红线延续）
2. **duty → instructions 由 L2 模板包装**——通用 agent 系统提示 + duty 注入，内核零场景硬编码
3. **spawn_team / dispatch 同机制**——都是元工具 + StopAtTools 交回编排层，复用 M7 派发回收骨架

graph 扩展（不动骨架）：state 加 `roles: {name → {instructions, history}}`；`_sub` 节点按 Send payload 的 `agent` 字段取角色持久上下文（无字段维持匿名并发）。

## 5. 模型能力现实评估（2026-09-17，务实为本）

> 用户判断：**当前模型能力并不能很好执行这套设计，这是现实。** 设计因此以"降级链 + 探针门"为一等公民，而不是假设模型撑得住。

| 环节 | 依赖能力 | 现实风险 | 评估 |
|---|---|---|---|
| 主 agent 建团（spawn_team 结构化输出） | 在线大模型的结构化输出稳定性 | 低——与现有 plan 工具同型，已验证可跑 | 可行 |
| 主 agent 多轮定向协调 | 长程规划 + 黑板状态理解 | 中——轮次多了上下文 rot；靠滚动摘要缓解 | 谨慎乐观 |
| **子 agent 角色执行**（本地 4B） | 角色 fidelity + 多轮记忆遵循 + 工具调用准确率 | **高——最大瓶颈**。项目历史真机暴露的假成功 / 空转问题说明连单 agent 任务执行都欠稳 | **先探针，不假设** |
| 角色记忆（滚动摘要） | 小模型摘要质量 | 中——摘要失败退化为硬滑窗（已有兜底） | 可降级 |

## 6. 降级链（每一级都是完整可用形态）

```
MT-3 动态建团（LLM 读文档涌现编排）        ← 目标态
  ↓ 建团输出不稳
MT-2 静态团队（skill 预定义固定名单，主 agent 只做定向派发）
  ↓ 小模型角色执行撑不住
MT-2b 角色全用主模型（成本换能力，按角色在 skill 里把 model 指向 brain）
  ↓ 不需要角色记忆
MT-1 匿名 worker 扇出（现状 dispatch）      ← 已有，保底
```

**降级不需要改代码**：全部通过 skill 内容 / config 调整实现（这正是三层分离的红利——降级是数据层操作）。

## 7. 里程碑（每步有验证门，不过门走降级链）

- **MT-0 模型能力探针**（纯验证脚本，零产品代码）：
  - 探针 A（主模型）：给一份游戏角色文档 + spawn_team 工具 schema，采样 N 次看建团输出的结构稳定性（字段完整率 / 角色粒度合理性）
  - 探针 B（本地小模型）：角色化 instructions（duty 注入）+ 3 轮记忆 + 工具调用，测任务完成率 / 角色行为偏航率
  - **不过门 → 直接停在 MT-1/MT-2b，不投入 MT-3**
- **MT-1 skill 数据层**：L0 注入路径 + L2 schema 解析（无团队执行，纯 recall/格式）
- **MT-2 静态团队**：skill 固定名单 + 定向派发 + 角色滚动摘要记忆
- **MT-3 动态建团**：spawn_team 元工具 + graph roles state
- **MT-4（远期）异步并行**：同时派多角色，黑板感知（v1 串行栅栏，复用 max_rounds）

## 8. 对标调研结论（2026-09-17 核实）

| 对标 | 机制 | 与本设计关系 |
|---|---|---|
| **CrewAI** | agents 文件化（role/goal/backstory + llm + tools） | L2 schema 直接参考；但 Crew 是静态配置，无涌现编排 |
| **LangGraph supervisor** | 星型拓扑 + team_members 动态更新 + per-agent Store 记忆 | graph 扩展方向与官方演进一致（我们用 WorldModel 分区替代 Store，更轻） |
| **DyLAN**（COLM 2024） | AIS 无监督选团（+25% 准确率实证）+ 动态拓扑 + In-Situ Learning | 动态选团有效性有数据背书；本设计更激进一步（角色定义本身涌现） |
| **Voyager** | 验证入库 + embedding 检索 + skill 组合复用（3.3x 实证） | 与 Curator / N=3 晋升门 / recall 同构；本设计 skill 为 markdown 人可读写，更可运维 |
| MetaGPT | SOP 硬编码进代码 | **反面教材**：换领域要改代码；本设计理念与其相反 |

**组合独特性**：三层分离（core 零知识 + MCP 能力 + skill 知识）+ 渐进 schema（L0 纯文本也合法）+ 涌现编排，现有工作无完全一致组合——最接近的是 Voyager 的引擎/skill 分离（但单 agent）扩展到多 agent。

## 9. 风险表

| 风险 | 缓解 |
|---|---|
| 小模型角色执行不稳（最大风险） | MT-0 探针门 + 降级链（MT-2b 主模型替代 / MT-1 匿名 worker 保底） |
| 建团质量差（角色重叠 / 粒度失衡） | L2 上限护栏（≤6 角色，config 可配）；skill knowledge 段给参考角色划分 |
| 主 agent 协调轮次膨胀 | max_rounds 既有兜底；黑板摘要注入（不灌全量） |
| 角色 history 无限增长 | 滚动摘要 + 最近 N 轮（对齐大脑压缩策略；失败退化硬滑窗） |
| skill schema 漂移（Curator 写坏 L2 段） | L2 解析宽容（坏字段忽略不拒载）+ Curator 只追加 knowledge 段（不碰结构字段） |

## 10. 待定项

- [ ] MT-0 探针的采样规模与通过阈值（跑探针时定）
- [ ] spawn_team 是否允许任务中途追加角色（倾向允许，append-only）
- [ ] 角色间是否需要直接通信（v1 纯黑板，远期再看）
