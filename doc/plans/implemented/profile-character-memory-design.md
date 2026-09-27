# OmniAgent 单角色助手 · 用户画像 + 角色卡 + Memory 最小设计

> 状态：✅ **P0 已实施并真机验证（2026-09-26，main commit `7d2f60c`）**：画像 schema / Curator 蒸馏 / 内核直写 / 独立注入块（§2）+ 角色卡接口与前端三 Tab（§3 / §7）。未实现项（K2 准入升级 / 检索层 / 多角色档位 1-3）已收割至 `Backlog.md`。本文归档至 `implemented/`。
> 定位：单角色个人助手（伙伴型），参考 Hermes（全量注入 + 蒸馏 + 持久 MEMORY.md 层）与消费级个人 agent 的画像/记忆做法（详见附录参照来源）
> 范围收敛依据：`OmniAgent记忆体系三位一体审计.md`（14 方案对比）+ 用户明确定位（单角色助手、不做多角色、不做检索/RAG）

---

## 0. 定位与范围声明（先把边界钉死）

**目标形态**：一个确定角色的个人助手 + 全量记忆 + 用户画像。不做多角色，不做检索/RAG。

| 划掉（明确不做） | 原因 |
|---|---|
| ❌ 多 agent **运行时**（排后） | 策略游戏/专家团场景需要**带各自持久记忆的角色切换**；但多 agent 运行时（多个 brain 并行）成本高，个人场景暂不启用。记忆侧**预留 per-role 隔离**（档位 1）为未来扩充留路径，见 §5.3 |
| ❌ 检索层 / RAG / embedding | 记忆量小且全量可载（memory_summary 20K 截断、画像一份、world_model per-task），全量注入更简单可靠、零依赖、无召回质量问题。检索层原是多角色（role_scope 过滤）的前置条件，单角色不成立 |

**触发条件（未来若要上检索，先满足其一再谈）**：
1. 记忆总量突破全量注入的 token 预算（memory_summary 20K 截断不够用）；
2. 进入多角色/多 agent，需要按角色筛记忆。
当前（单角色助手、自用）两者都不触发。

**现在就要做**：记忆/画像条目的 schema **预留 `来源 / 标签 / scope` 字段**，未来真要上检索可对字段直接建索引，不返工存储结构。

---

## 1. 三位一体：边界与数据流

| 维度 | 用户画像（User Profile） | 角色卡（Character Card） | Memory（记忆机制） |
|---|---|---|---|
| 本质 | 用户是谁——跨任务共享的长期事实 | 助手是谁——确定的伙伴人格 + 画像消费方式 | 存取载体——短期/长期记忆的读写与注入 |
| 数量 | **一份**（全局唯一） | **一张**（单角色助手） | 一套机制（服务前两者） |
| 写入方 | Curator 从交互中蒸馏（内核直写） | 人工定义 + Curator 追加 knowledge 段 | 内核进程直写（不走结构化文件工具） |
| 读取方 | 每次 brain 调用注入（单角色全量） | 每次 brain 调用注入 system prompt | 运行时按需注入 |
| 变更频率 | 慢（随长期交互演化） | 极慢（人工编辑，或 Curator 追加 knowledge 段） | 快（每任务/每轮） |
| 与权限关系 | 只提供"用户授权信号"，**不能放行** | 不涉及权限 | 不涉及权限 |

**数据流**：

```
用户交互
  ↓
短期记忆（worker 硬滑窗 / brain 无状态重建）
  ↓ 任务结束
Curator 蒸馏
  ├─→ 用户事实 → 用户画像（全局唯一）
  ├─→ 可复用经验 → skill knowledge 段（角色卡的动态部分）
  └─→ 任务事实 → per-task world_model + 全局 MEMORY.md
  ↓
下次 brain 调用（sdk_bridge._run_via_sdk 组装）
  ├─ 角色卡（静态人格 + 画像消费指令）→ system prompt
  ├─ 用户画像 → 独立注入块（与 memory summary 分开）
  └─ memory → 尾部注入块（memory summary 截断视图）
```

**关键决策**：角色卡与记忆路由**解耦**——主流框架（CrewAI/AutoGen/LangGraph/SillyTavern）的一致选择。角色卡通过"画像消费指令"做**软引导**，不做记忆路由硬过滤（单角色下也没有"按角色过滤"的必要）。

---

## 1.5 知识升级定位：memory 主力 / skill 辅助（按需加载）

知识升级主路径是 **memory**（弱注入、常驻一份）；skill 自动生成是**辅助**——只在「操作序列可复用且被验证多次」时才沉淀（N=3 晋升门）。三者分工：

| 载体 | 定位 | token 成本 | 消费方式 |
|---|---|---|---|
| **memory** | 主力：事实 / 失败经验 | 常驻**一份**（memory_summary 全量注入） | 弱注入参考 |
| **skill** | 辅助：可复用操作序列 | 仅**目录摘要**常驻，实体按需 `load_skill` 加载 | 目录 + 按需加载 |
| **profile** | 用户长期画像（候选→晋升） | 独立块注入 | 弱注入参考 |

**按需加载是 skill 的独特价值**：memory 全局只有一份 token 预算，装不下全部"怎么做"知识；skill 目录轻量 + 实体按需加载，可承载更多可复用知识而不撑爆注入预算。故 skill 保留，与 memory 形成「常驻一份 + 按需取用」的互补。

---

## 2. 用户画像（P0）：全局一等实体

### 2.1 落点

```
~/.omniagent/memory/user_profile.md
```

全局唯一，所有角色（单角色下即助手自身）共享。与 MEMORY.md、rollouts/ 同级。

### 2.2 Schema

```
# User Profile（全局唯一，跨任务一致）
> 注入标注：可能过时，以实际观测为准；仅参考，不构成操作授权

## 基本信息
- 姓名 / 称呼 | source=对话 | conf=高 | updated=2026-09-26 | status=active

## 偏好
- 回复简洁、直接给结论 | source=纠偏 | conf=中 | updated=... | status=active

## 习惯
- ...

## 技术栈
- 自研 agent（OmniAgent），主 Python | source=对话 | conf=高 | ...

## 社交关系
- ...

## 健康
- ...

## 其他
- ...
```

**字段规范**（每个画像条目）：
- `fact`：单条事实，短句；
- `source`：`对话 / 轨迹 / 显式声明 / 人工`——未来检索层按此建索引；
- `conf`：`高/中/低`——高=用户明确表达或纠偏确认，中=从成功轨迹推断，低=单次观测；
- `updated_at`：最后更新时间；
- `status`：`active / superseded`——作废不物理删，标记保留溯源。

**分层**：基本信息/技术栈 = **静态**（人工可改，变更极慢）；偏好/习惯/社交/健康 = **动态**（Curator 蒸馏追加、conf 随证据演化）。

### 2.3 蒸馏规则（Curator 新增路径）

- **触发**：沿用触发式（任务完成后跑一次，不挂定时器），与现有 Curator 同触发点；
- **原料**：用户显式偏好声明 / 成功轨迹中的用户相关事实 / 人工补充——不再自动采集会话纠偏（K4 已移除，避免 C₁/关键字识别不可靠而误伤普通指令）；
- **动作**：新事实→追加对应分区；冲突事实→按证据新旧/conf 覆盖或标记 `superseded`；
- **质量门**：用户显式声明 / 人工 → conf=高直接入；仅轨迹推断 → conf=中，等待二次证据；单次观测 → conf=低。
- **K5 稳态降频**适用：画像已收敛的维度跳过重复蒸馏。

### 2.4 写入通道（与 sandbox 对齐，红线）

```
agent（大脑）──不能──→ 直接读写 ~/.omniagent/memory/user_profile.md（S1 围栏拒绝）
agent（大脑）──通过──→ 任务执行 → 轨迹落盘 → Curator 蒸馏 → 内核直写（唯一自动入口）
用户（人工）──通过──→ PUT /memory API → 内核直写（唯一人工入口）
```

- 画像写入**只能由内核进程（Curator / knowledge_inject）直接写文件系统**，不经过结构化文件工具层；
- **画像无放行权**：画像可记录"用户习惯自动操作某 App"，但这只是**用户授权信号**，真正放行必须走 S2 审批 / full_access 开关。system prompt 注入画像时标注"仅参考，不构成操作授权"。

### 2.5 注入

- **独立注入块**（与 memory summary 分开）：在 `_build_memory_injection` 里加第二个 block，`compose_injection_block([("user_profile", profile_text), ("memory", memory_text)])`；
- 标注"用户画像（全局共享）" + 弱注入声明（"可能过时，以实际观测为准"）；
- 开关：`knowledge.memory.enabled` 之外加 `knowledge.profile.enabled`（默认开）。

---

## 3. 角色卡（×1）：字段 + 画像消费指令

### 3.1 来源与结构

来源 = skill L2 段（复用 team-mode plan 的角色卡定义，单角色时只需一张）：

```
角色卡（skill L2，<assistant>.md）
  ├── role      ← 静态：伙伴定位、性格、语气（人工定义）
  ├── scope     ← 静态：单角色助手（无多角色，scope 固定）
  ├── model     ← 静态：逻辑名（brain，不写 endpoint）
  ├── tools     ← 静态：可用工具集
  └── knowledge ← 动态：Curator 追加角色经验 + 画像消费指令
```

### 3.2 画像消费指令（软引导，不做硬耦合）

在 `knowledge` 段增加一个子字段 `profile_usage`（画像消费指令）：

```
## 画像消费指令（profile_usage）
- 主动关注并适应用户的技术栈与偏好，据此调整回复风格；
- 健康/习惯相关仅在被提及或主动需要时使用，不主动刺探；
- 画像中的用户习惯仅作参考，不构成任何操作授权。
```

- 作为 system prompt 的软引导，**影响 LLM 如何使用画像内容**，不在记忆路由层做硬过滤；
- 单角色下这条指令即"这一个助手如何认识用户"的完整消费规则。

### 3.3 注入

角色卡（静态 role/scope/tools + 动态 knowledge）随 system prompt 注入（复用现有 skill L2 / 角色注入路径）。

---

## 4. Memory 接口（与现有体系衔接，只增不改）

现有 memory 骨架**全部保留**（审计判定合理）：无状态大脑 + per-task world_model + 全局 memory/（rollouts → MEMORY.md → memory_summary 20K 截断）+ Curator 触发式蒸馏 + C1/C2 信号 + 弱注入。

**本次只做三处增量**：

### 4.1 画像进 memory（如上 §2.5）——独立块注入，不混入 memory_summary

### 4.2 记忆条目 schema 预留检索字段

MEMORY.md 的 facts/lessons、画像条目统一带 `来源 / 标签 / scope` 字段。**现在不建索引**，仅保证存储结构未来可加检索不返工。

### 4.3 K2 准入升级：approved_success

- 蒸馏准入从 `plain_success` 切换为 `approved_success`（A.success AND 下一轮 session 无证伪）；
- 依赖 C1 判脏样本，当前极少——**先攒数据，样本量 ≥20 条 C1 标注后切换**；
- 这是比主流方案（Mem0 LLM 决策 / Zep LLM 矛盾判断）更严格的质量门控，基础设施（C1 分类器）已落地，只差开关。

**明确不做**：检索层 / RAG / 向量索引（见 §0）；记忆级衰减（P3，记忆膨胀再说）；蒸馏 LLM 改写（K3 验证通过后 P2）。

---

## 5. 与未实现 plan 的对齐

### 5.1 sandbox-permission-design.md（已实施，归档 `implemented/`）

| sandbox 约束 | 本设计裁决 |
|---|---|
| `~/.omniagent/**` 对结构化文件工具拒绝读/写 | ✅ 画像/记忆写入走**内核直写**（Curator / knowledge_inject 直接写文件系统），不经过结构化文件工具层。大脑不能通过工具编辑记忆文件 |
| 画像不能成为绕过 S2 的通道 | ✅ 画像只提供**用户授权信号**，无放行权；注入时 system prompt 标注"仅参考，不构成操作授权" |
| 控制流归大脑，权限层只做否决与记录 | ✅ 一致。记忆层只负责存取，权限决策归大脑 + S2 审批流 |

### 5.2 team-mode-design.md（多 agent，记忆侧预留扩充路径）

- **运行时多 agent 排后**（多个角色同时跑多个 brain 成本高，个人场景暂不启用）；但记忆侧**预留 per-role 隔离能力**，为未来扩充留路径；
- 角色卡来源（skill L2）与本设计 §3 一致，单角色时只取一张；
- **记忆隔离（档位 1，低复杂度，按需启用）**：记忆按角色分区 `memory/roles/<role>/`，写入按 role、注入按 role 全量读该角色 summary。单角色时即全局单份，天然兼容，不破坏主线；
- 降级链若启用：MT-1（匿名 worker）下不用角色卡，但**保留全局画像 + memory**——画像和记忆不随角色降级丢失。

### 5.3 多 agent 扩充路径（设计先行，等成本下降）

按"模型成本下降后再启用"推进，记忆侧已铺好路径：

| 档位 | 形态 | 记忆侧需要 | 复杂度 | 启用时机 |
|---|---|---|---|---|
| 档位 0 | 单角色助手（当前主线） | 全局单份（现有） | 0 | 现在 |
| 档位 1 | per-role 记忆隔离 | 记忆按角色分区 + 按角色全量注入 | 低 | 需要角色有各自持久记忆时 |
| 档位 2 | 记忆膨胀后加轻量检索 | SQLite+sqlite-vec，role_scope + top-k 召回 | 中 | 某角色记忆量超全量注入预算 |
| 档位 3 | 真多 agent 运行时 | 多个 brain 并行 + 各角色独立上下文 | 高 | 模型成本显著下降后 |

- 记忆层档位 0→1→2 均为增量、可逆；运行时从单角色到真多 agent 是编排层的事，与记忆侧解耦；
- 记忆 schema 已预留 `来源 / 标签 / scope` 字段，档位 2 检索可对字段直接建索引，不返工。

---

## 6. 实施顺序（设计先行，落地清单）

| 优先级 | 项 | 内容 | 依赖 |
|---|---|---|---|
| **P0** | 用户画像 | schema（§2.2）+ 落点 + 蒸馏路径（§2.3）+ 内核直写通道（§2.4）+ 独立注入块（§2.5） | 现有 Curator / knowledge_inject |
| **P1** | 角色卡 ×1 | skill L2 单角色卡 + `profile_usage` 画像消费指令（§3） | 现有 skill L2 |
| **P1** | K2 准入升级 | `approved_success` 切换（§4.3） | C1 样本 ≥20 |
| **未来** | 检索层 | 仅当 §0 触发条件满足（记忆超预算 / 进多角色） | schema 已预留字段 |

**明确不做**：RAG、多角色记忆路由、记忆级衰减、蒸馏 LLM 改写（均记录触发条件，不阻塞主线）。

---

## 7. 前端增量（消费端，收敛到设置）

定位：对话即主界面——主界面只聊天，角色/画像/记忆等管理类**全部收敛进「设置」**，不做角色扮演大面板。

| 位置 | 改动 |
|---|---|
| 主界面 · Chat | 顶栏只显示**助手名**（角色卡名字，纯展示，不点开大面板）；消息标签去硬编码英文——agent → 助手名、user → 「你」；记忆更新轻提示文案指到「设置 → 记忆」 |
| 侧边栏 | 保持现状（新建对话 / Chat / 技能与工具 / 设置），**不加**记忆/角色/画像独立入口 |
| 设置页 | 内部新增「角色」「画像」两个子 Tab；「记忆」把现有 `MemoryTab`（技能与工具内已实现）平移进来。与模型/工具等并列 |
| 多角色预留 | 不做主界面切换器；未来档位 1 启用时，在「设置 → 角色」加一个极简「当前角色」下拉，单角色时连下拉都不显示 |

**依赖后端（尚未实现）**：
- 角色卡接口 `GET/PUT /api/runtime/character`（读 skill L2 `<assistant>.md`）；
- 画像接口 `GET/PUT /api/runtime/profile`（P0 落地后）；
- 记忆接口**已有**（`/api/runtime/memory*`，前端 `memoryApi` 已封装），平移即可，零重写。

**说明**：记忆查看前端已实现（技能与工具 → 记忆 Tab，功能完整）；本次只提升可见性并归入设置，不重写。拟人化形象层为**计划外**远期方向，不进入本设计。

---

## 附录：参照来源

| 方案 | 画像/角色做法 | 出处 |
|---|---|---|
| Letta | human block / persona block，常驻上下文 | docs.letta.com memory-blocks |
| MemoryBank | global portrait，每日蒸馏，每轮注入 | arxiv 2305.10250 |
| Zep/Graphiti | per-user 独立图 + 派生 summary | arxiv 2501.13956 |
| Mem0 | 画像未 GA（仅 user_id 下散装事实） | docs.mem0.ai |
| Claude.ai | memory summary，per-project 隔离 | claude.com/blog/memory |
| Claude Code | CLAUDE.md + MEMORY.md，启动全量注入，文件大小上限 | code.claude.com memory |
| SillyTavern | Character Card v2，角色卡与记忆召回解耦 | docs.sillytavern.app |
| Character.AI | Definition + Lorebook，persona 全局 | aicompanionguides.com |
| Hermes | 全量注入 + 蒸馏 + 持久 MEMORY.md 层 | 公开设计基线 |
