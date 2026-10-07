# OmniAgent · 未实现计划与遗留问题汇总（Backlog）

> **本文档只保留仍需行动、验证或等待触发条件的事项**。完全完成且无后续工作的条目直接删除；历史由 Git 与 `doc/plans/implemented/` 追溯。
> 当前架构事实以 `doc/agent-control-master-spec.md`、`doc/end-to-end-dataflow.md` 与 `doc/agent-control-arch-*.html` 为准。

## 待办

### skill 宏轴 · 有效性验证（memory-architecture.md §7.4）
- 来源：`doc/plans/memory-architecture.md` §7.4（skill 宏轴有效性验证）
- 状态：🟡 **实现已落地（2026-10-04 生产者于 2026-10-05 重接至任务收尾）；真机试点已收口——机制层跑通
  （蒸馏链 5/5 成功、entry_id 正确分结构），但注入技能形态零步数收益
  （Δsteps=0 负观测）**。⇒ 价值全系于 `replay_skill` 回放执行器（跳过每步 LLM 决策），不在注入参考。
- 待办：
    1. **收益验证必须走回放口径**：A/B 对照中 B 组命中技能须实际调用 `replay_skill` 硬重放
      （execution-verified），不得用"注入技能序列"当治疗臂——早期试点已证该形态无可测收益。
    2. **场景锚定确定性工具链 / 脚本化例程类**：shell/file 固定例程（LLM 跑通一次后程序化重放），
      不要用简单 UI 点屏任务（T2 类已证是收益最难点）。
    3. 负结果纪律：连续 2 个任务集 B 不优于 A 即停手（沿用早期试点口径）；淘汰参数
      （`skill.evict_*`）待真机数据校准。

### 团队模式（Team Mode）· 角色型常驻子 agent
- 来源：`doc/plans/team-mode-design.md`
- 状态：🟡 **设计稿，未实施**。当前只有匿名 worker 的 `dispatch → Send → 聚合`；没有角色身份、角色持久上下文、定向派发或 `spawn_team`。
- 已有基础：skill 目录注入与 `load_skill` 已能加载完整纯文档内容（原设计 L0 缺口已由 T2.4 收口）；M10 匿名 worker 扇出可作为 MT-1 保底。
- 待办：
    1. MT-0 模型能力探针与通过阈值；不过门则停在匿名 worker 或角色统一使用主模型。
    2. L2 角色 schema（`role/scope/model/tools/knowledge`）解析，模型只引用逻辑名。
    3. MT-2 静态团队：固定角色、定向派发、任务内角色历史与滚动摘要。
    4. MT-3 动态建团：`spawn_team` 元工具 + graph roles state；MT-4 异步多角色协作远期再议。
- 红线：角色/领域知识只在 skill，能力只在 tool/MCP，core 不出现具体场景、角色或任务知识。

### 基础工具安全增强 · L3 注入硬隔离
- 剩余风险：网页和其他工具输出仍依赖模型遵守“不把外部数据当指令”的软纪律，尚无内容级硬隔离。
- 待办候选：
    1. **`full_access` 下的 shell 高危操作确认**：对删除、格式化等高危命令仍强制 HITL。该方案与 `full_access` 免审批语义冲突，实施前需先做产品裁决。
    2. **工具输出指令性筛查**：用小模型或通用规则识别 `web_fetch` / `web_search` 返回中的操控性指令，命中后告警或隔离。是否实施取决于真实注入事件频率。
- 红线：方案必须通用，不得加入场景词表或业务规则。

### 沙箱与权限 · 增强遗留项
- 来源：`doc/plans/implemented/sandbox-permission-design.md`
- 2026-10-02 设计决策：**S1 路径围栏撤销**（绝对拒绝区 / 按名写拒 / read_only 写禁先删）。
- 2026-10-03 设计决策（最终态）：**S1 整体删除、写入不设防**——含允许根（`security.allow_write_roots`）与 `ensure_writable` 写门（filesystem 插件调用链一并拆除，设置页「允许写入的根」区块删除）。个人助手定位下写不做路径门；安全面只剩 S0 档位（read_only 拒 exec/network/actuate）+ S2 危险动作审批 + 审计。旧「写经审批卡」口径作废。
- 待办：
    1. **S3 OS 级硬隔离**：Docker 容器 runner 首选（Windows ACL 备选）。🧊 远期；触发条件是开放外部用户或接入不可信第三方任务。这是同时约束文件工具与 `shell_exec` 的硬边界。
    2. **MCP 执行类工具审批收口**：当前 MCP 工具由 SDK 原生派发，不进入统一审批门。首个有写/执行副作用的第三方 MCP server 接入前，按既定路线将 `list_tools()` 自注册转为带 gate 的 FunctionTool（`source="mcp"`）。
    3. **`security.deny_read_roots`**：可选的凭证目录读取黑名单 glob。读路径当前完全放开（S1 撤销后无读网关）；若凭证泄漏成为实际风险再立项。

### 画像/角色卡/记忆 · 遗留项
- 来源：`doc/plans/memory-architecture.md`
- 待办：
    1. **检索层 / RAG**（§0）：设计已立（2026-10-04，`doc/plans/memory-rag-design.md`——仅 memory 轴，skill 严格排除、两轴正交是硬约束；参照 TencentDB Agent Memory 的本地 SQLite/FTS5 形态与三预算口径）。**触发条件**：project fact_index + 全局 MEMORY.md 合计 > 100 条，或注入预算出现可观截断——满足前不动工（早期试点实测：池子 1 条时排序无差别）。落地切片：S1 FTS5 → S2 向量臂 → S3 RRF+三预算，每步独立可回退。
    2. **多角色记忆档位 1-3**（§5.3）：档位 1 = per-role 记忆隔离（`memory/roles/<role>/`，
       低复杂度按需启用）；档位 2 = 记忆膨胀后轻量检索（SQLite + sqlite-vec）；档位 3 = 真
       多 agent 运行时。设计先行、均为增量可逆，启用时机 = 需要角色各自持久记忆 / 模型成本显著下降。

### 长任务 2.x 与收尾遗留
- 当前实现入口：`omni_core/local/loop/`、`omni_core/brain/sdk_loop.py`、`backend/api/routers/chat_runtime.py`
- 待办：
    1. **daemon 长任务实测（仅剩真机验证）**：`task.json` 的 `_meta.task_mode="daemon"`、daemon 收尾转 `PAUSED`、`/api/runtime/wake` 续跑及对应单测均已落地；待跑带真实步数上限的跨块续航与无人值守观察。
    2. **EV-1 事件溯源会话**：会话轨迹真源化、历史派生、请求指纹快照与崩溃状态合成；是否立项由实机数据决定。
    3. **SA-1 完整版**：T5.1 MVP 已具备 wake 端点与 daemon/paused 续跑；尚缺 Agent 发现、定向消息、中断等配套控制工具。
    4. **O7 空间层**：在 task 私有与全局之间加入 `projects/<slug>/skills/`、`projects/<slug>/memory/`，形成「任务 > 空间 > 全局」三级合并。
    5. **前端待办清单投影**：实现 todo 元工具结果的前端清单视图。
    6. **顺序依赖测试基线（暂缓）**：单进程按序执行时，`TOOL_REGISTRY` 等进程级全局会造成污染；该问题不阻断当前功能，按用户决定不作为近期主线。

### 性能与体验优化 · 剩余项
- 待办/观察：
    1. **O6 收紧子 agent 上下文**：`omni_core/local/loop/graph_runner.py` 的 `_exec_runtime_ctx` 仍注入 `global_skills_dir`，`omni_core/brain/prompt.py` 仍渲染该字段。是否让 worker 只保留 `task_dir` / `task_skills_dir` 尚待设计拍板。
    2. **O2 外部工程侧编码**：外部桥接脚本不在本仓库；其 UTF-8 统一需在外部工程完成。
    3. **MCP `isError` 标记**：服务端异常文本可能伪装成业务观察返回；当前模型可自愈，维持观察，只有高频复现时才改 MCP 工具层。
- 注：O7 空间层与上条「长任务优化」第 4 项为同一项，不重复列。
