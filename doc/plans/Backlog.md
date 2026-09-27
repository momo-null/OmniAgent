# OmniAgent · 未实现计划与遗留问题汇总（Backlog）

> **本文档为唯一前向规划文档**，整合自原 `plans/future/` 未实现计划与全部 plan 中散落的遗留/开放问题。
> 历史 plan 已全部归档至 `plans/implemented/`（标注「已失效」），以本文件 + `doc/agent-control-master-spec.md` + `doc/agent-control-arch-*.html` 为权威来源。

## 一、未实现 / 待办计划（原 `plans/future/`）

### OmniAgent X3 · M5 meta-loop 规划（自升级闭环）
- 来源：`plans/implemented/agent-control-m5-plan-2026-07-27.md`
- 状态：🧊 **封存（2026-09-18 定调）**——四层清账：skill 级（M4b.2+K0 注入）/ world-model 级（M4b.1）/ 策略级（escalation）已完成；权重级 X1 已验证后封存（知识层稳态优先，解封条件=K5 判据持续不可达）。自升级主线转 K 系列（见 `doc/plans/知识级自升级_完整设计_K系列_定稿.md`；原引用 `doc/knowledge-upgrade-design.md` 文件不存在，2026-09-19 已修正）
- **补充（2026-09-19）**：K5 稳态判据已落地并出数（四信号可查、介入频率 0.667、`converged=false`）。
  解封条件「K5 判据持续不可达」**尚未满足**——K5 刚出数，需积累稳态数据后再评估，**维持封存**。
- 概要：M4 把「**数据在产 + 知识可沉淀**」这条链路打通了：轨迹落盘、world-model 持久化、skill 库 + N=3 晋升、Curator 自动策展。

### K 系列遗留缺口（K2–K5 主干已落地，以下为明确未做项）
- 来源：`doc/plans/知识级自升级_完整设计_K系列_定稿.md` §2「已知缺口」+ §5.6.6 / §6.1 / §7.1 / §8.1
- 状态：🟡 **待办**（不影响闭环运行，属增强项；主干已移至「三、已完成」）
- 明细：
    1. 回溯过滤（清除 C 判脏记忆）— §5.6.6
    2. 准入升级 → `approved_success` — §5.6.6（当前仍 `plain_success`）
    3. V3 ablation 结论 — §6（脚本就绪，需 ≥20 次同域对照人工长跑）
    4. 多域分区 — §8（v1 为单域常量 `single-domain(v1)`）
    5. K6 LLM 蒸馏增强 — §2（可选远期，M5 语料供给；依赖 K3 通过后决议）
    6. 真实自然收敛验证 — §8（K5 仅模拟法验证过降频触发，需长期积累至判据自然满足）
- 已闭环（2026-09-19）：
    - ~~K4 真机 5 例纠偏入库验证~~ ✅ 已完成——开启 `corrective_source` 后 3 个任务 /
      5 条纠偏全部入库，且正常认可零误采。
    - ~~K5 收敛降频真机触发~~ ✅ 已完成——模拟收敛态（dedup 0.92 / 介入 0.025）→
      判 `converged=true` → 纠偏任务**未产生 rollout**（已跳过蒸馏），
      Curator 日志 `已收敛：跳过蒸馏（低频维护）` 可见；验证后信号已复原。
    - ~~C₃ 人工抽检校准~~ ✅ 已完成（2026-09-26）——`PUT /signals/calibration` 写入端点
      （收样本→`calibrate_c1c2`→写 `c1_calibration_error`）+ `/signals/summary` 透传
      `calibration_samples`，≤10% 采信分级判定，真机验证字段由 null 变为可写可读。
    - 注：真实「自然收敛」（非模拟）仍未发生，需长期积累。

### 基础工具组安全增强 · L3 注入硬隔离（2026-09-19 新增；2026-09-27 状态更新）
- 来源：本次会话新增 `filesystem`/`shell`/`web` 基础工具组（commit `6c5f46a`）
    + L1 间接提示注入防范（web 内容标注「外部不可信数据」+ `prompt.py` 纪律 9 禁泄露密钥）。
- 状态：🟡 **部分完成（2026-09-27 更新）**——L1 仅为软兜底（靠模型不自服从工具内指令），
  对默认开启的 `filesystem` 写入**不能完全杜绝**被恶意网页诱导改写；需 L3 硬隔离。
- 候选方案（按成本，L3 至少取其一，且须保持「内核通用、不写死场景」红线）：
    1. ~~**文件系统写操作沙箱化**~~ ✅ **已由沙箱 S0-S2 落地（2026-09-27）**：`security.allow_write_roots`
       允许根 + `ensure_writable` 根外写入审批 + 任务 tmp 隐式白名单，越界写经人点头或拒绝
       （见 `plans/implemented/sandbox-permission-design.md` §5）。
    2. **shell 强制二次确认**：即使开启 `完全访问`，每次 `shell_exec` 的高危命令（rm/del/格式化等）
       经 HITL 确认才执行。（standard 档已由 S2 逐次审批覆盖；`full_access` 豁免为设计裁决 §4——
       本条仅指「full_access 下仍确认高危命令」，与该裁决有张力，做前先议。）
    3. **工具输出指令性筛查**：用小模型/规则对 `web_fetch`/`web_search` 返回做「是否含操控性指令」
       预筛，命中则先告警或隔离再交给大脑。（未做）
- 取舍：~~个人单域项目，先取 (1) 性价比最高~~ (1) 已落地；(2) 与 full_access 设计裁决冲突、暂缓；(3) 视注入事件频率再议。
- 关联：L1 实现见 `plugins/web/plugin.py` `_UNTRUSTED_BANNER` 与 `omni_core/brain/prompt.py` 纪律 9。

### 沙箱与权限 · S0-S2 未实现项（2026-09-27 收割自 sandbox-permission-design.md，已归档 implemented/）
- 来源：`plans/implemented/sandbox-permission-design.md`（S0/S1/S2 已实施，见该文状态行）
- 明细：
    1. **S3 OS 级硬隔离**（§7）：Docker 容器 runner 首选（Windows ACL 备选）。🧊 远期——
       触发条件：开放给外部用户，或接入不可信内容（网页、第三方任务）时启动。
       这是唯一能同时约束「文件工具 + shell_exec」的硬边界。
    2. **MCP 执行类工具审批收口**（§6.5）：v1 MCP 工具经 SDK 原生派发、不进审批门。
       触发 = 第一个**执行类**（有写/执行副作用）第三方 MCP server 真要接入时；
       路线已钉死：自注册转调（`list_tools()` 包成 FunctionTool + gate，`source="mcp"`），不包 SDK 对象。
    3. **`security.deny_read_roots`**（§5.1 可选增强）：额外凭证目录读黑名单 glob（如 `.ssh/**`），默认空，未实现。
    4. **mcp.json 示例补过渡期纪律注释**（§6.5）：收口前 mcp.json 只接只读类 server（搜索/文档类），
       执行类不接——写进示例注释即可，非代码。（小项，未做）

### 画像/角色卡/记忆 · 未实现项（2026-09-27 收割自 profile-character-memory-design.md，已归档 implemented/）
- 来源：`plans/implemented/profile-character-memory-design.md`（P0 已实施并真机验证，见该文状态行）
- 明细：
    1. **K2 准入升级 → `approved_success`**（§4.3）：与上方「K 系列遗留缺口」第 2 条同源；
       前置 = C1 标注样本 ≥20 条，先攒数据再切开关。
    2. **检索层 / RAG**（§0）：设计已拒，仅当触发条件满足再立项——记忆总量突破全量注入
       token 预算（memory_summary 20K 截断不够用），或进入多角色需按 role_scope 筛选。
       记忆 schema 已预留 `来源 / 标签 / scope` 字段，届时可直接建索引不返工存储。
    3. **多角色记忆档位 1-3**（§5.3）：档位 1 = per-role 记忆隔离（`memory/roles/<role>/`，
       低复杂度按需启用）；档位 2 = 记忆膨胀后轻量检索（SQLite + sqlite-vec）；档位 3 = 真
       多 agent 运行时。设计先行、均为增量可逆，启用时机 = 需要角色各自持久记忆 / 模型成本显著下降。

### 长任务优化 1.x 收官 · 2.x 与收尾遗留（2026-09-22 整理）
- 来源：`plans/implemented/OmniAgent 长任务优化 · 定稿留档（2026-09-20）.md`（决策 D1–D7 与红线永久有效）
- 状态：🟢 1.x 已全量落地（Batch-1 + Phase-2/3/4 + SA1 + Batch-5 + B1/B2 修正，验收全绿）；🧊 2.x 未立项（按 D4 依赖实机数据决议）
- 明细（未做项）：
    1. **daemon 模式长任务实测**（最近一步，B1/B2 修复后已具备条件）：task.json 增 `_meta: {"task_mode": "daemon"}`，跑带步数上限的长任务验证跨块续航至真实步数上限 + 无人值守观察（前置条件见 `plans/b1-fix-exec-plan-2026-09-22.md` 附节）
    2. EV-1 事件溯源会话：会话轨迹真源化 / 历史派生 / 压缩仅表层遮蔽 / 请求指纹快照 / 崩溃状态合成（10–20 人天，实机数据决议立项）
    3. SA-1 完整版：后台可续跑 Agent + 配套控制工具（发现 / 消息 / 中断），对齐 D5 架构终态（5–10 人天）
    4. O7 空间层：task 私有与全局之间加 `projects/<slug>/skills/` + `projects/<slug>/memory/` 第三层，「任务 > 空间 > 全局」三级合并（3–5 人天）
    5. 前端配套迭代：工具开关交互、待办清单投影、技能目录展示（后端接口已全部就绪）
    6. 回归测试豁免基线清理：存量失败用例单独排期（`test_m3_tools` 系列、`test_states`、`test_execution_backend`、`test_m4d_sdk_transport` 等，失败集以 Batch-1 基线为准）
- 注：KJ-1 已由 F4.1b + F4.2 提前落地，不列。

### 性能与体验优化分析 · 遗留未做项（2026-09-19 基线，部分完成）
- 来源：`plans/implemented/OmniAgent-性能与体验优化分析-2026-09-19.md`
- 状态：🟡 部分完成——O5 已落（T4.6 知识注入落轨迹）；O4 已被 T2.4（技能目录 + load_skill 按需加载）替代实现；§2.1 子 agent 事故修复、§6 当次修复均完成
- 明细（未做项，按优先级）：
    1. **O1 收尾（P0 验收）**：MCP 工具一等函数**实机实证**——`connect()` 修复后代码前置已就绪，跑新任务验证 agent 弃用 `run_python` 旁路、schema dump/回读消失；若仍走旁路，再深挖暴露链路 vs 行为引导
    2. **O3（P1 轻量）**：`prompt.py` 的 `capability_block` 补视觉兜底纪律——「已连接提供结构化状态的集成（如 MCP）时，优先用它获取状态/操作，视觉仅兜底」，零风险、不硬编码领域
    3. **O6（轻量）**：收紧子 agent 上下文——`tool_loop.py` 的 `_exec_runtime_ctx` 移除 `global_skills_dir`，子 agent 只保留 `task_dir` / `task_skills_dir` 最小角色锚点（依赖原文档 §2.2 边界主张拍板）
    4. O2（外部工程侧）：RimWorld 桥接脚本 `mcp_call.py` 统一 UTF-8 编码（解 GBK 解码 / 编码双向 bug，`PYTHONIOENCODING=utf-8` + 响应 charset 探测）——不在本仓库，完成后与 O1 实证合并验收
    5. MCP isError 标记（观察项）：「Object reference not set」类服务端异常文本伪装成业务观察返回，现有错误三分法（协议/业务/真失败）无法区分——当前模型可自愈、维持观察；若实机复现频繁，需 MCP 工具层给该类异常打 isError 标记（来源：F4.1b 复跑观察结论 + `plans/implemented/2026-09-20-tool-selection-issues.md` 问题七延伸）
- 注：O7 空间层与上条「长任务优化 2.x」第 4 项为同一项，不重复列。

### OmniAgent X3 设计符合性评估与优化计划（2026-08-08）
- 状态：✅ **已执行完成 → 已移至「三、已完成」**（原条目不再属于待办）

## 二、遗留问题 & 开放问题（收割自全部 plan）

### 来源：`plans/implemented/agent-control-m4-plan-2026-07-26.md`
#### M3b — 两层执行编排（补齐 M3 未完成的一半，M4a.2 指标成立的前提）
**缺口**：当前 `tool_loop.py` 为「单大脑驱动」——在线 GLM 每步兜底，`self.brain` 是唯一 BrainClient，`prompt.py` 无 `plan`/子目标 schema。Master Spec §2.1 定义的「在线大脑规划（低频）+ 本地 4B 高频执行」两层未落地。这导致 M4a.2 的核心指标 `Brain Intervention Rate` 在单大脑下恒 = 100%，毫无意义。**M3b 是 M4a.2 成立的前提，须先于 M4a.2 落地。**

**设计原则（manager-worker，不破红线）**：大脑**只做规划**（把目标拆成结构化子任务），所有子任务按架构约定一律走本地 4B；本地干不动时靠**可量化的升级条件**把现场交回大脑。大脑**不做模型路由**（不实时判断「该给不给 4B」），模型选择从大脑权责中拿掉，由结构固定。

- 新增 M3b 行：两层执行编排（在线大脑 plan + 本地 4B 执行 + escalation 升级）；当前 ❌ 未落地，须先于 M4a.2。

### 来源：`plans/implemented/agent-control-m5-plan-2026-07-27.md`
#### 6. 风险与开放问题
| 风险 | 说明 | 缓解 |
|---|---|---|
| 行为克隆学成「乱点」 | 无 reward 时容易把「试错 20 次后成功」学成策略 | 质量门仅采纳 verified+retry==0 步 |
| 策略注入干扰自主性 | 软约束变硬约束，大脑变笨 | ablation 验证，默认弱注入 |
| 权重训练退化 | LoRA 过拟合小样本 | 小样本 + 回滚机制 + 人确认 |
| 原始 I/O 落盘体积 | 每步存完整消息+completion 体积大 | 仅高质步落盘、定期 prune |
| 旧训练链环境 | torch/peft/GGUF 转换当年卡 GFW | 训练前先验证环境可装，失败则暂缓④ |

---

### 来源：`plans/implemented/agent-control-refactor-design-2026-07-26.md`
#### 11. 开放问题收口（2026-07-26 决策）
> 用户原 5 问全部拍板，记录如下。

1. **子目标 JSON 细节**（§3.4）：**参考现有代码动作格式**。现有 `agents/execution.py` 的动作契约是
   `{"action_type": str, "action_params": dict, "reason": str}`，动作枚举
   `click / double_click / right_click / move / drag / type / press / hotkey / wait`。
   本地工具调用直接复用这套枚举与字段（新增 `screenshot / ocr_region / template_match / click_mark / observe` 等扩展枚举，字段同构）。
   大脑下发的**子目标** JSON 沿用同构风格：`{objective, subtasks:[str], done_when:str, constraints?:str}`。
2. **在线供应商选型**（§3.1）：**已定 —— OpenAI 兼容供应商**，OpenAI 兼容端点。
    - `base_url`: `https://api.example.com/v1`
    - `model`: `demo-model`
    - `api_key`: 经环

- **通用 agent + 工具 + 自观测 + 自进化**，控制流归还 LLM。本项目据此做**完全重构**，推翻旧程序控制，不保留旧插件系统（遗留不为保留而存在）。

### 来源：`plans/implemented/refactor-agent-core-framework-design-2026-09-12.md`
- **`_run_inner` 标记废弃**：源码 `DEPRECATED(M4)` 标记 + 进程级一次性 `DeprecationWarning`，指向 `sdk_agent.run_task_sdk` + `orchestration.graph`；`_assistant_msg`（手搓 FC 序列化）同步标注 LEGACY。
- [x] 旧 `tool_loop._run_inner` 标记废弃（`DEPRECATED(M4)` + 运行时 `DeprecationWarning`）。
- **M5 遗留（需决策）**：长任务历史压缩现在只在 L2 检查点之间生效（历史主由框架

### 来源：`plans/implemented/retrospective-azurlane-enum-2026-07-28.md`（GUI 场景枚举去特化复盘 · 历史归档）
#### 8. 开放问题
1. `_UI_DENY` 该不该也由 agent 学（感知知识也走沉淀）？→ 是，应作为 knowledge 类 skill 动态积累，而非静态词表。
2. **纯自主基线**：删宏 + 极简 `_UI_DENY` + 零提示下，agent 能否自己从失败中长出枚举策略？需真做实验验证（本报告的后续动作）。
3. 策略生成的安全边界：agent 生成的策略若错误（如「逐个点开更好」），如何靠 verify/escalate 快速否决而不污染 skill 库？

### 来源：`plans/implemented/skill-two-tier-design-2026-07-28.md`
#### 9. runtime 去场景化（内核去遗留场景假设 · 红线硬伤）
> 背景：用户确认「A —— 这就是之前遗留必须清理」。内核当前把「手机 UI 自动化」的若干假设
> **写死在 kernel 里**，违反北极星红线（内核必须通用，绝不写死任何场景专属逻辑）。
> 好消息：抽象地基已存在（`ExecutionBackend` 抽象基类 + `brain/tools.build_registry` Provider 注册表），
> 所以去场景化 = **把残留的场景假设从 kernel 搬回 backend/provider，内核只认通用 percept/action**。

### 来源：`plans/implemented/workspace-task-refactor-plan-2026-08-01.md`
- **遗留（不在 P5 范围，建议单独立项）**：`training/gguf_converter.py` 仍用 `target_app`/`app_id` 并输出到 `./apps/<app_id>`，且 `tests/test_universal_redline.py` 已将其列为红线违规词——属训练导出脚本的场景残留，后续治理（改名 `target_project` + 调整输出结构）。

### 来源：`plans/implemented/x3-design-gap-optimization-plan-2026-08-08.md`
- 5. **测试可移植且可复现。**测试不得写真实用户主目录，也不得依赖先前遗留数据或当前

---

## 三、已完成（从「未实现 / 待办」移出，保留可追溯）

> 本节仅归档**曾列于本清单、现已完成**的条目，保留来源与完成时间以便追溯；
> 不再占用「未实现 / 待办」视野。

### OmniAgent X3 设计符合性评估与优化计划（2026-08-08）✅
- 来源：`plans/implemented/x3-design-gap-optimization-plan-2026-08-08.md`
- 完成于：**2026-08-22**（P0.1 / P0.2 / P0.3 安全门 / P0.4 / P1.1–P1.5 / P2.1–P2.3 / P3.1–P3.3 落地，pytest 163 passed、web build 通过）
- 概要：项目已落实本计划全部关键缺口，架构骨架与 X3 设计文档的可验收要求一致。
- 移出日期：2026-09-19（原列于「一、未实现 / 待办」，状态早已为完成）

### K 系列 K2–K5 · 知识级自升级 ✅
- 来源：`doc/plans/知识级自升级_完整设计_K系列_定稿.md`
- 完成于：**2026-09-19**（本次实现并真机验证）
- 范围：
    - **K2** 信号基础设施（`omni_core/local/signals.py` + `GET /signals`、`/signals/summary`）
    - **K3** 有效性裁决脚本（`scripts/review_rollouts.py` + `scripts/effectiveness.py`）
    - **K4** 纠偏采集（`curator.distill_task_memory` 第三来源 `## user_corrections`，双层红线②默认关）
    - **K5** 稳态运营（`omni_core/local/steady_state.py` + `GET /signals/steady` + SkillsAndTools「稳态」Tab）
- 验证：K2、K5 **经真机验证**（自动采集零操作、C₁/C₂ 生效、假成功可检出、四信号出数、衰减曲线渲染）；
  K3 脚本就绪、K4 单测通过（`tests/test_k2_k5.py` 11 项）。
- 注意：K3/K4 仍有未验项，K 系列遗留缺口见「一、未实现 / 待办」。
