# 沙箱与权限控制 · 设计

> **状态**：✅ **S0 / S1 / S2 已实施并合入 main**（2026-09-27，commit `fc2db16` / `f64d5e8`；验收用例 `tests/test_policy_paths.py` / `test_approval_gate.py` / `test_approvals_e2e.py`）。未实现项（S3 / MCP 收口 / `deny_read_roots` / mcp.json 过渡期注释）已收割至 `Backlog.md`。本文归档至 `implemented/`。
> **适用范围**：agent 获得「操作电脑」能力（命令执行 / 文件读写 / 键鼠 / 进程）之后的能力边界。
> **本文＝安全与沙箱（S0-S3）唯一来源**。
> **实施计划与审计记录**：不在本文件（见内部计划，含证据与实测结论）。

---

## 1. 设计原则

1. **锁必须在被锁的东西够不着的地方** —— 边界不能由被约束方自己承载。
2. **同机上 agent 能读到的秘密都不是秘密** —— 本机的 token、文件权限都不构成边界；真正的边界只有「人不在环就做不了」或「OS 级隔离」。
3. **默认拒绝** —— 危险能力默认关闭，开启需要显式动作。
4. **人在环是最后一道** —— 任何权限变更都必须有一次不可省略的人工动作。

---

## 2. 威胁模型（两条，分开解）

| | 威胁 | 典型形态 | 由谁解决 |
|---|---|---|---|
| **T1** | 越界 / 误操作 | 无边界写入冲掉用户数据；命令打错范围；无人拦 | S0 + S1 + S2（≈ 原 L0 + L1） |
| **T2** | 恶意代码 / prompt injection | 不可信内容诱导执行攻击代码、窃取本地凭据、横向移动 | 仅 OS 隔离（S3 ≈ 原 L2） |

T1 是当前主要风险且防护成本低；T2 需要 OS 级隔离，后置。

**防护对象（正面定义）**：模型失控导致的文件系统副作用——幻觉 / 误操作 / 提示注入让 agent 写坏用户文件、删错目录、泄露敏感数据。

### 2.1 明确不防（非目标）

- ❌ 恶意插件（同进程信任模型；声明式 `permissions` 只是审计与审批触发器，**不是隔离边界**；插件准入靠人工审查 manifest + 代码）
- ❌ 网络隔离 / 进程可见性（单机桌面 agent 本就要上网和控设备，隔离与产品目标冲突；DSH 同样明确放弃）
- ❌ 提示注入本身（靠纪律文件与轨迹审计）
- ❌ 追求全盘防读（工作区外读放行已覆盖主要面，凭证目录黑名单是可选增强）

### 2.2 裁决原则：不做命令字符串启发式预检

对齐 DSH 实证结论：**无法理解 expansion / subprocesses / symlinks 的含义，让内核（或工具层硬校验）做最终裁决，拒绝后归类并给升级通道，是唯一可信信号。**

Python 代码静态扫描同理不可靠 → 只做**结果侧限制**（路径围栏），不做**意图侧识别**（危险度打分 / 命令解析）。

理由（三条，与 §10 的「不做」清单同源）：
1. **拦「话」不拦「手」** —— 事故来自**作用范围**而非命令名（实证：覆盖用户配置的命令是 `pytest`，完全无害；要删数据也不需 `rm`：`Remove-Item` / `python -c shutil.rmtree` / `rd /s /q` / base64 二次解释）。
2. **命令是唯一无法结构化校验的入口** —— 文件工具有 `path` 可判；`shell_exec` 入参是**字符串**，校验它等于写 shell 解析器 + 意图分类器，两者都不完备且会随模型变强而贬值。
3. **黑名单制造假安全** —— 人以为有防护 → 敢继续 `full_access=true`，比不做更危险。

**替代**：路径 / 范围边界（允许根）+ 危险动作整条要人点头 + 可选**绊线**（只记审计与提示、不阻断，明码标价"抓惯性手滑，抓不住刻意"）。粒度是**范围**，不是内容。

---

## 3. 四层防御总览（S0-S3）

| 层 | 内容 | 一句话 |
|---|---|---|
| **S0** | 权限模式档位（standard / read_only ⊕ per-task `full_access`；详见 §4） | 决定"默认有多松" |
| **S1** | `PathPolicy` 应用层路径围栏 | 决定"这条路走不走得通" |
| **S2** | 审批流（SSE 推卡片 + REST 决议 + 挂起） | 决定"例外谁点头" |
| **S3** | Docker runner（首选）/ Windows ACL（备选） | 硬边界（远期） |

**与 §8 实施批次的对应**：S0 + S1 的静态部分 ≈ **L0（第一批）**；S2 ≈ **L1 粗闸门（第二批）**；S3 ≈ **L2 OS 级隔离（后置）**。

**接入点（在工具层，不在具体插件）**：S2 审批拦截统一注入在 `omni_core/tools/base.py` 的 `@function_tool` 装饰器内（插件作者无感知、无法绕过）；S1 路径围栏双挂点——绝对拒绝区在 `workspace.resolve_path` 路径漏斗、允许根判定在写类工具入口（§5.2）。

---

## 4. S0 —— 权限模式（全局档位 ⊕ per-task 完全访问）

> **与 capability-unit 重构的对账（2026-09-27）**：本节制定于插件化之前（原意「重组 full_access / groups / disabled」）。重构后 `groups`/`disabled` 已删、`shell_exec` 定为内核 builtin 常开（不可关）、文件读写不拆分。故 S0 重定义为**运行时策略档位**（决定围栏与审批「默认有多松」），**不动插件 / 环境开关面**——那已经是用户的人工动作，与权限档位正交。

**有效形态 = 全局档位 ⊕ per-task `full_access`**：

| 维度 | standard（默认） | read_only | full_access=true（per-task） |
|---|---|---|---|
| 危险动作（exec / actuate / 根外写入） | S2 审批卡，人点头才执行 | **自动拒绝**（不弹卡，fail-closed + 审计） | 直接放行（不弹卡） |
| 读 / 观察类工具 | 放行（受 §5 绝对拒绝区约束） | 放行（同左） | 放行（同左） |
| §5 绝对拒绝区 | **始终拒绝** | 始终拒绝 | **仍然拒绝** |
| 典型场景 | 日常 | 挂机实验 / 不可信内容 / 演示 | 信任任务的一次性放权 |

决策与理由：

1. **全局「免审批」档刻意不做**。免审批只以 per-task `full_access` 存在（每次任务一次显式人工动作，前端开关与确认弹窗已具备）。常开的免审批档会制造惯性——§2.2「黑名单制造假安全」的对偶：人忘了它开着。
2. **`full_access` ≠ 无边界**。绝对拒绝区（OmniAgent 自身配置 / 记忆 / 审计，见 §5.1）在 `full_access` 下**仍然生效**——否则 agent 可改写自己的权限载体，直接违反 §9 验收 2「自提权路径不可用」。前端「完全访问」确认弹窗需补此边界文案。
3. **档位变更不热生效（L0 该条的落地方式）**：ToolLoop 每次 run 已做运行级配置快照（阶段 0.5），`security.*` 随快照进入 run——改档位**不影响进行中的 run，下次 run 生效**。环内必有人工动作（设置页改动即人点头），且载体（`~/.omniagent/**`）在绝对拒绝区内 agent 触不到，满足 L0 意图。`full_access` 是 per-task 请求参数（发送消息时人工携带），维持任务级热开关不变。

配置（缺省进 `config.CONTEXT_DEFAULTS`，Web 设置页「安全」区落 `~/.omniagent/config.yaml`）：

```yaml
security:
  mode: standard           # standard | read_only
  allow_write_roots: []    # 额外允许写入的根（绝对路径列表）
  approval:
    wait_seconds: 600      # 审批等待上限（秒）；0 = 无限等；超时按拒绝处理
  audit: true              # 审计落盘开关
```

**风险类别（S2 的判定输入）**——工具**自声明**，内核零工具名字面量（红线 R2）：

| 声明方式 | 类别 | 典型工具面 | standard 档行为 |
|---|---|---|---|
| 装饰器 `risk="exec"`（前置门） | 命令执行 | `shell_exec` | 每次审批（无内容级豁免，§2.2） |
| 装饰器 `risk="actuate"`（前置门） | 键鼠 / 设备注入 | host `press/click/type`、emulator `tap_by_id/tap_text/press_keycode/launch_app` 等 | 每任务首次审批；卡片「本任务不再询问」**默认勾选** |
| 装饰器 `risk="network"`（档位执行） | 网络出站 | `web_fetch` / `web_search` | standard 放行（web 插件开关＝常驻同意）；read_only **拒**（§6.5 已裁决） |
| 工具入口 `policy.ensure_writable(p)`（入口门） | 文件写入 | filesystem `write_file/edit_file/mkdir` | 根内静默放行；根外审批 |
| 未声明 | 纯 / 读 | `read_file/list_dir/search_content/observe`、`local_model` 等 | 不拦截（§5 路径漏斗仍生效） |

> `actuate` 若逐次审批会让 GUI 任务不可用（一次任务几十张卡）；任务级一次同意是「粗闸门」在键鼠面上的粒度取舍。
> `exec` 逐次审批不可豁免：命令字符串无结构可判（§2.2），宽窄只能由人掌握（或整任务 `full_access`）。
> `network` 的不对称是**已裁决**（2026-09-27，§6.5）：网络面是唯一「出站」面，其常驻同意由 web 插件开关给出，standard 不加每任务卡；read_only 作为全局收紧覆盖之（GET 查询串即可外带数据，read_only 的「零副作用」承诺必须含网络）。升级路径：不可信内容任务增多时改为任务级首卡，只动一行策略映射。

---

## 5. S1 —— `PathPolicy` 应用层路径围栏

**定位（诚实，维持原判）**：应用层围栏对 `shell_exec` **不是硬边界**（命令字符串里 `echo x > ...` 不经路径检查）。它拦的是**走结构化路径入口的高频路径**（filesystem 全部工具、emulator adb 文件工具等）——首期目标拦 90% 无恶意误操作；能同时约束文件工具与命令执行的硬边界仍只有 S3（OS 级隔离）。对齐 DSH SAFETY.md：*"sandboxing reduces risk, does not guarantee isolation"*。

### 5.1 三个动作面

1. **绝对拒绝区**（不可配置；standard / read_only / full_access 一律拒绝）：
   - `~/.omniagent/**` **读写全拒**（含列目录 / 检索 / 建目录）——权限载体（config.yaml、plugins/*.yaml、task.json 的 full_access）、记忆（memory/、user_profile.md）、审计（audit/）都在这里。读也拒是降注入窃取面（原则 2 已承认这非硬边界）。
     **唯一例外**：当前任务的 `tasks/<task_id>/tmp/**`——agent 的合法工作区（写脚本 / 截图 / 产物），隐式允许根。
   - **全盘写拒绝**（不问地点）：`config.yaml` / `config*.yaml` / `mcp.json`（权限载体 wherever they sit）；`AGENTS.md`（F4.1 纪律文件；该检查从 filesystem 插件收编进 policy，插件内重复判断删除——反兼容，无用户）。
   - 可选增强：`security.deny_read_roots`（额外凭证目录 glob，默认空，如 `.ssh/**`）。
2. **允许根（写入放行区）**：当前任务 tmp（隐式、永远允许）+ `security.allow_write_roots`（用户配置，绝对路径列表）。根外写入 → 交 S2 审批（**不是直接拒**——个人助手整理用户文件是正当需求，人点头即放行）。
3. **shell 不在 S1 内**：`shell_exec` 只接受命令字符串（§2.2 不做内容判），其范围风险由 S2 整条审批承担。

### 5.2 实现挂点（两处，零魔法）

| 挂点 | 覆盖面 | 机制 |
|---|---|---|
| **路径漏斗**：`workspace.resolve_path` 内调 `policy.guard_path(p)` | 一切经它解析路径的工具（filesystem 六件、emulator adb 工具等），**读写两侧的绝对拒绝区零遗漏、工具零改动** | 命中区抛 `PolicyRefusal` → 统一出口 |
| **写入口**：写类工具在**任何副作用之前**显式调 `policy.ensure_writable(p)`（`write_file` / `edit_file` / `mkdir` 三处） | 允许根判定：根内通过；根外**阻塞**走 S2 审批，批准后继续执行 | 阻塞式（sink 同步等待），拒绝抛 `PolicyRefusal` |

- **编码规约**：`ensure_writable` 必须在首个副作用前调用——阻塞式设计因此不需要「批准后重入函数」。
- **统一出口**：`base.py::function_tool` 包裹层捕 `PolicyRefusal` → 结构化结果回模型 + 审计。拒绝形状（S1 与 S2 共用契约）：

```json
{"ok": false, "denied": true, "policy": "S1", "rule": "self_carrier",
 "error": "路径受保护（OmniAgent 自身配置/记忆），已拒绝: <path>",
 "hint": "不要重试或绕过；改用任务目录，或向用户说明需要人工处理"}
```

- S2 侧 `rule` 取值：`user_deny` / `timeout` / `cancelled` / `mode_read_only`。`hint` 的职责是让模型**换方向**而不是换姿势重试。

---

## 6. S2 —— 审批流

**已定（2026-09-24，维持）**：`full_access=true` → **不弹审批卡**，危险动作直接放行。人工动作落在**开启开关这一次**（显式授权），而非每动作一次——这正是该开关的原意（全权时别打断）。`full_access=false`（默认）→ 危险动作进审批。本节补齐状态机、注入点、API、挂起恢复、超时语义与前端交互。

### 6.1 组件分层（内核不碰 SSE）

```
omni_core/tools/policy.py        # 门本体：risk 前置门 + ensure_writable + ApprovalSink 协议
                                 # + SecurityContext（ContextVar）+ 审计（纯标准库，可上机复用）
backend/services/approvals.py    # SseApprovalSink：SSE 推卡 + REST 决议 + per-task 记忆 + 超时
backend/api/routers/approvals_api.py   # POST /api/runtime/approvals/{id}/decision
web Chat 页                      # 顶置审批卡（通用三风险标签，零插件特判——前端红线）
```

- 内核定义 `ApprovalSink` 协议：`request(card) -> Decision`（**阻塞**）/ `remembered(task_id, key) -> bool` / `remember(task_id, key)`。未注册 sink（单测 / 脚本直调工具）→ `AutoDenySink`：立即拒绝 + 审计，**fail-closed 且不挂测试**（既有直调 shell 等工具的用例随本批次改为显式注册放行 sink）。
- **注入点（维持 §3 原判）**：`base.py::function_tool` 装饰器内用 `functools.wraps` 包裹业务函数后再交 SDK——schema 生成不受影响，插件 / 环境作者零感知、无法绕过。已核实 SDK 执行模型（`agents/tool.py`）：同步工具经 `asyncio.to_thread` 调用 → 门内 `Event.wait` 阻塞的是工作线程，事件循环与流式打字机不受阻。
- **诚实边界（v1 范围）**：MCP 工具由 SDK `MCPServer` 原生派发、不经本装饰器 → **v1 不进审批**；其 per-server 启用开关即人工动作（默认拒绝）。收口路径：未来在 `build_mcp_servers` 产物外包同一层 gate，不阻塞主线。
- **运行期上下文**：`SecurityContext`（task_id / full_access / 档位 / 允许根）由 `graph_runner` 在 run 起始与 `workspace.set_task` 同点绑定（ContextVar 沿 asyncio 传播——现有 task tmp 机制同款）；未绑定（直调）回退 `AutoDenySink` + config 档位。

### 6.2 状态机与数据流

```
              ┌───────────┐   approve（REST，含 remember）→ 执行工具 → 审计 decision=approved
  工具线程 ──→  pending   │   deny     → 返回拒绝结果（rule=user_deny）
 （阻塞等待）  │ Event.wait│   timeout  → 返回拒绝结果（rule=timeout，fail-closed）
              └───────────┘   task stop→ 返回拒绝结果（rule=cancelled）
```

- **请求卡片**：`{approval_id, task_id, run_id, tool, unit, risk, arguments, created_at}`；ApprovalStore 为后端内存 dict（按 task 归属，任务终态清空——与 outbox 同生命周期哲学）。
- **推送**：SSE 新事件 `approval`（推卡）与 `approval_resolved`（`{id, outcome}`）；同时并入 `GET /api/runtime/live` 快照新增的 `approvals` 通道（与 thinking/message/toolcall 同机制、按 id upsert）。
- **决议**：`POST /api/runtime/approvals/{approval_id}/decision`，body `{action: "approve"|"deny", remember?: bool}`；沿用现有 `server.auth_token` 鉴权面（L0 API 最小鉴权）。
- **记忆语义**：`remember=true` 记 per-task 键——exec / actuate 按 **risk 类级**（一次同意覆盖该类全部工具；键鼠任务若按 (tool, risk) 记会每个工具一张卡，不合理）；write 按目标根。**仅内存、仅本任务**（不落盘、不跨任务、任务终态清空）：每个任务都从严格起步，符合原则 4。
- **超时**：`security.approval.wait_seconds`（默认 600s，每张卡自创建独立计时）到期按**拒绝**处理（fail-closed），审计 outcome=timeout；0 = 无限等。挂机实验把等待调短或直接用 read_only 档。前端卡片副文案展示「N 秒未处理将自动拒绝」。
- **审批等待不计入墙钟（实现要求）**：墙钟在 chunk 边界按「启动至今」结算（`sdk_loop.py` chunk 循环），不杀进行中的调用但**会计时**——若把审批等待算进去，worker 子任务（`escalation.wallclock_sec` 缺省 120s）里一次 600s 的人工等待会让子任务在**批准后**被误判墙钟超时。实现：`SecurityContext` 累加每次门的等待秒数，墙钟结算扣除之（主链 `long_task.wallclock_sec` 缺省 0 不受影响）。
- **/stop 与任务终态**：该 task 全部 pending 卡置 cancelled（线程放行、工具收到拒绝结果、审计留痕）——不允许任务结束后还有卡悬着。

### 6.3 前端交互（具体）

1. **顶置审批卡**（输入框上方，不随时间线滚走；Alert warning 样式）：
   - 标题行：风险标签（**通用三词**：命令执行 / 写入文件 / 键鼠操作，由 risk 映射——前端不认识具体工具）+ 工具名 + unit；
   - 参数块：等宽字体展示关键参数——exec=完整命令串、write=目标路径、actuate=动作参数；完整 JSON arguments 可折叠查看（人依据事实点头，§2.2）；
   - 选项：☐ 本任务内不再询问（actuate 默认勾选；exec / write 默认不勾）；
   - 按钮：[拒绝] [允许]；副文案：「N 秒未处理将自动拒绝」（wait_seconds=0 时显示「将一直等待」）。
2. **决议后**：卡片收起；时间线里出现常规 toolcall 卡（result = 拒绝 JSON 或执行结果）——**持久记录自然落位，不新增消息类型**；`approval_resolved` 用于多端卡片同步收起。
3. **刷新 / 切任务恢复**：挂载时 `/live` 的 `approvals` 通道重建顶置卡（按 approval_id 去重，与 SSE 增量共存）。这是「挂起态必须可刷新恢复」的落地（复用已实现的 live 快照机制，否则刷新 = 任务永久卡住）。
4. **并发**：fan-out 子任务可能同时多张 pending——纵向堆叠、各自独立决议、互不阻塞。
5. **等待期间其他交互不变**：可软注入（不打断，下一轮可见）；`/chat` 仍 409（审批等待期任务仍属 running）。
6. **设置页新增「安全」区**：档位 radio（标准 / 只读）、允许根列表编辑、审批等待秒数、审计只读查看（尾部 N 条）。文案注明「变更自下次运行生效」。
7. **full_access 确认弹窗**：补一句「OmniAgent 自身的配置与记忆目录始终拒绝访问（完全访问也不例外）」。

### 6.4 审计留痕（L0）

- 落点：`~/.omniagent/audit/YYYY-MM.jsonl`（月轮转；位于绝对拒绝区内，agent 不可触达 / 篡改）。
- 条目：`{ts, task_id, run_id, tool, unit, risk, arguments(截断 2k), decision: approved|user_deny|timeout|cancelled|denied_s1|mode_read_only|auto_deny, rule, cwd, wait_ms}`。
- 记录**每次门的干预**（审批 / 拒绝 / 超时 / 档位拒）；根内静默放行不记（防刷屏）。读取走 `GET /api/runtime/audit`（设置页消费）。

### 6.5 已裁决（2026-09-27 讨论，原「开放问题」收口）

- **network 类别：采纳「半档」方案**。`risk="network"` 落地（`web_fetch`/`web_search` 声明），standard 档放行（web 插件开关＝常驻同意，不加每任务卡），read_only 档拒——补齐 read_only「零副作用」的语义洞（web 工具虽只 GET，查询串即可外带数据）。升级路径：不可信内容任务增多时改为任务级首卡（改一行策略映射）。审计不另加：轨迹已含每次调用的完整 URL，外带取证走轨迹。
- **审计 UI：两段式，明确不做审计台**。①门干预同步推现有 debug 通道（`kind="audit"`），右栏运行日志实时可见（随 S2 批次，几行代码）；②设置页安全区只读表（尾 200 条 + decision 筛选，随 S0 批次）。按 task / 时间 / 工具的深度过滤与轨迹跳转**不做**——单用户低频，深度排查直接 grep jsonl，防工具化漂移（同 §10 精神）。
- **MCP 收口：v1 不做，触发条件与路径钉死**。触发 = 第一个**执行类**（有写 / 执行副作用）第三方 MCP server 真要接入时；届时走**自注册转调**路线——connect 后自行 `list_tools()`，把每个 MCP 工具包成普通 FunctionTool（gate + 转调 `server.call_tool`）注册进 `TOOL_REGISTRY`（`source="mcp"`），不再把 MCPServer 交给 Runner：对 SDK 契约依赖最小，且把 base.py 一直声称的「MCP 与自研工具平级」变成事实；**不走**「包 SDK 对象」的易碎路线（SDK 版本漂移风险，本仓已在 Chaquopy spike 吃过依赖面过深的亏）。过渡期纪律：收口前 mcp.json 只接只读类 server（搜索 / 文档类），执行类不接——写进 mcp.json 示例注释即可，非代码。

---

## 7. S3 —— 沙箱实现（远期）

| 方案 | 隔离强度 | 成本 | 结论 |
|---|---|---|---|
| DSH `windows-acl`（降权令牌 + 目录 ACL） | 文件效果级 | 高 | 备选 |
| DSH `sandbox-local`（bwrap / Landlock / Seatbelt / ACL 三平台） | 同上 | 高 | 参考；OmniAgent 仅 Windows |
| OpenAI Codex（Landlock / Seatbelt / 容器） | 同上 | 高 | 词汇表值得抄（三档 mode） |
| **Docker 容器 runner** | 强（含网络 / 进程） | 中 | **首选**（本机已装 Docker） |
| Windows Sandbox / VM | 最强 | 极高 | 否决（冷启慢、无持久 workspace） |
| 应用层路径围栏 | 弱于 OS 级 | 低 | **S1 首期方案** |
| RestrictedPython / 静态扫描 | 伪沙箱 | 中 | 否决（公认可绕） |

---

## 8. 实施批次与优先级（L0 / L1 / L2）

### L0 —— 让边界「不可被改写」（必做，第一批）

| 要求 | 说明 |
|---|---|
| 敏感路径写入拒绝 | 用户数据根（`~/.omniagent/**`）、配置类文件（`config*.yaml`、`mcp.json`）对**所有**文件操作拒绝，含读 / 列目录 / 检索 / 建目录（不只写）。**作用域仅限结构化文件工具**（见下方 ⚠️） |
| 权限判定不落在可写载体上 | 工具启用 / 禁用、高危放行等权限字段，不能由**被控进程可写**的位置承载；退路为「启动快照 + 完整性校验 + 变更告警」 |
| 权限变更不热生效 | 权限相关变更**必须经重启**才生效，使环里必然出现一次人工动作 |
| API 最小鉴权 | 设置类端点需鉴权（含 loopback）；但见原则 2 —— 鉴权只是提高门槛，不作为兜底 |
| 审计留痕 | 危险动作落 `{时间, 工具, 参数, 工作目录, 判定, 依据}`，可回溯「谁放行了什么」 |

> 兜底项是「权限变更不热生效」：只有它把控制权交回给人。其余三项是提高门槛。

**⚠️ 该边界的作用域（重要）**：路径拒绝只对**结构化文件工具**有效（它们有 `path` 参数可判）。命令执行工具不接受路径参数、只接受**命令字符串**，因此**可绕过**该边界（例如用重定向写入）。⇒ 路径边界**仅在命令执行关闭时**才是硬边界；一旦开启即退化为绊线。

> **分层漏洞（务必记住）**：L0 的「工具层路径边界」**挡不住 `shell_exec`** —— shell 可 `echo x > ~/.omniagent/config.yaml` 绕过 `filesystem_tool` 的拒绝。
> **推论**：能同时约束「文件工具 + 命令执行」的只有 L2 / S3（OS 级隔离）。
> **故 L0 的安全前提是：命令执行保持默认关闭，只由人按需开启。**
>
> **⚠️ 2026-09-24 变更（与 `implemented/capability-unit-refactor-2026-09-24.md` 对齐）**：命令执行（`shell_exec`）已改为**内核 builtin 默认开**。故本条的「默认关闭」前提**被替换**为「默认开 + S2 审批门（危险动作要人点头）+ `full_access` 绕过」。L0 的路径边界对命令执行仍是**绊线**（非硬边界），能同时约束文件工具与命令执行的硬边界仍只有 **S3（OS 级隔离）**。

### L1 —— 粗闸门（第二批）

- 危险动作**默认要人点头**：命令执行、允许根之外的写入、键鼠模拟、`run_python`
- **允许根白名单**：文件写入限定在 workspace（配置项）
- 命令**不做内容级规则**（白名单 / 正则），只做「整条要人批」的粗门 —— 见 §2.2

### L2 —— OS 级隔离（后置，仅选型）

- 候选：容器 / 独立低权限账户 / 低完整性级别进程（选型对比见 §7）
- **触发条件**：开放给外部用户，或接入不可信内容（网页、第三方任务）时启动

### 落地批次（2026-09-27 细化，对应 §4–§6）

| 批次 | 内容 | 主要改动面 |
|---|---|---|
| **S1 骨架** | policy 模块 + 绝对拒绝区 + 允许根 + 审计落盘 | 新增 `omni_core/tools/policy.py`；`workspace.resolve_path` 接入漏斗；filesystem 写三件加 `ensure_writable` 并删除插件内 AGENTS.md 重复检查（收编）；`config.py` CONTEXT_DEFAULTS 增 `security.*`；`tests/test_policy_paths.py` |
| **S2 审批** | 门 + sink + REST + 前端卡 + `full_access` 转正（去 inert） | `base.py::function_tool` 包裹层；`graph_runner` 绑定 SecurityContext；`backend/services/approvals.py` + approvals 路由 + `/stream` 增 `approval`/`approval_resolved` 两事件 + `/live` 增 approvals 通道；web taskStore / Chat 顶置卡 / full_access 弹窗文案；shell 与 host/emulator 工具 `risk=` 声明、web 工具 `risk="network"`；墙钟结算扣除审批等待（§6.2）；门干预推 debug 通道（`kind="audit"`）；直调工具的既有用例改造（AutoDenySink） |
| **S0 档位 + 设置 UI** | read_only 档 + 设置页「安全」区 | 档位 radio / 允许根编辑 / 等待秒数 / 审计查看；`security.mode` 进 run 快照判定 |
| S3 | 不变（远期，Docker runner 首选） | — |

风险点（开工先核实）：① ContextVar 是否传播进 LangGraph `Send` 扇出的子任务（task tmp 已依赖同机制，预期可行，需对拍）；② `function_tool` 包裹层经 `functools.wraps` 后 SDK schema 生成不受影响（签名自省对拍）；③ 既有直调 shell 的用例（编码 / 集成）随 AutoDenySink 改造。

---

## 9. 验收标准（可测）

1. 越界写入（用户数据根、配置类文件）返回拒绝，且读路径同样被拒——`~/.omniagent/config.yaml` 的读 / 写 / 列 / 检索全拒；`tasks/<当前task>/tmp/**` 写放行
2. **自提权路径不可用**：任何试图扩大自身权限的操作，都不能在无人工动作的情况下生效（`full_access=true` 下写 `~/.omniagent/**` 仍拒；档位 / 允许根变更需人经设置页操作且不影响进行中 run）
3. 危险动作在审计记录中可回溯，含放行 / 拒绝与依据（`approved / user_deny / timeout / cancelled / denied_s1` 各至少一条用例）
4. S2 全流程：pending 卡 → 刷新页面后卡片经 `/live` 恢复 → approve 后工具实际执行；deny / 超时 / `/stop` 后工具收到结构化拒绝结果（`denied: true`）且 agent 循环继续（不崩、不重试死循环）
5. 档位行为：`full_access=true` 时 shell 不弹卡直接执行；`read_only` 时 shell 自动拒绝且审计 `rule=mode_read_only`
6. actuate：同任务第二次键鼠调用不再弹卡（remember 生效）；新任务恢复弹卡
7. 上述各条有对应用例（`tests/test_policy_paths.py` / `tests/test_approval_gate.py`），可纳入回归；`scripts/review_lint.py --strict` 通过（policy 模块零工具名字面量 / 零场景 token）

---

## 10. 明确不做（防贬值投入）

以下三类都是「替模型思考」的近似 —— 随模型能力提升会持续贬值，且维护成本高（理由见 §2.2）：

- 意图分类器 / 危险度打分模型
- 命令级白名单 / 正则黑名单（易绕过，维护无底洞）
- 「再叫一个模型判断是否危险后自动放行」

判据：**消失的是「写规则」的工程，增长的是「造反馈 + 立约束 + 验产出」的工程。**

---

## 11. 与核心设计的关系

- **红线（内核零场景硬编码）**：权限是**运行时配置**，不进内核；内核不出现任何业务 / 场景语义的权限判断
- **工具＝平级插件**：权限粒度为「组 / 工具名」，不引入业务语义
- **控制流归大脑**：权限层只做**否决**与**记录**，不替大脑做策略选择
