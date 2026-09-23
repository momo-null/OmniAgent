# OmniAgent X3 控制层架构总规（Master Spec）

> **状态**：本文为 OmniAgent **X3** 架构的**权威固化（consolidated single source of truth）**。
> **范围**：桌面 AI agent 控制层重构——从「程序化固定管线」转向「LLM 大脑 + 本地执行器」两层分级，含模拟器验证轨道、知识自学、权重级自升级、前端重构。
> **架构要点**：统一 `/chat` 单入口（无 `/run`）；三通道解耦（planner / executor / vision 各自独立可配模型），online_only = executor 由主模型兼任（仍是双 agent 双层，不退化）。多 agent 为**去分层**结构（主 agent 默认自己做；仅互不依赖的并行子任务才由 `dispatch` 扇出），详见 `doc/plans/multi-agent-redesign-2026-09-13.md`。红线扫描目录为 `omni_core/`。

---

## 0. 状态基线（诚实）

| 项 | 状态 | 说明 |
|----|------|------|
| MVP 闭环（大脑决策 + 工具调用） | ✅ 已验证 | 计算器场景证成 observe→决策→retry 闭环（IME 坑归知识，不硬编码） |
| 两层分级（本地 qwen3.5-4b 执行器） | ✅ 已接入 | M3b 落地：大脑规划 + 本地 4B 执行 + escalation 升级回流 |
| llama-server 封装 | ✅ 跑通 | M0 修复：llama.cpp 升级 b10107 + Qwen3.5 thinking 关 + ctx 8192 |
| ModelHub 联通 | ✅ 联通 | M0 完成，warmup≈7.6s |
| 旧 LoRA 训练链 | ✅ 曾端到端跑通 | minesweeper 合并产物为证；GGUF 当年生成后被删（占空间） |
| EmulatorBackend(u2/ADB) | ✅ 实现 | M1 落地，GUI 设备真机闭环验证通过（首个端到端场景） |
| Trajectory / Telemetry / Verify / State | ✅ 实现 | M4a 地基全部完成（79 passed），commit fc145fe + 8c6e48e |
| world-model 持久化 + checkpoint | ✅ 实现 | M4b.1 落地（commit dbe71e8）：save/load + checkpoint JSON + merge_progress |
| skill 库 + N=3 晋升门 | ✅ 实现 | M4b.2 落地（commit a5fdaa0，107 passed）：SkillLibrary + from_run_record + 连续3次→active |
| Curator（触发式维护） | ✅ 实现 | M4b.3 落地（commit 0d21f47）：prune + refine_world_model + review_candidate_skills + flag_low_quality，`tool_loop._finish` 末尾触发 |
| M6 子任务状态机（SubtaskStore） | ✅ 已落地 | `task_store.py`：RLock + 原子写 + 白名单 update；`claim`/`claim_next` 原子 CAS（pending→running），落 `tasks/<task_id>/subtasks.json`；WorldModel 管世界认知，TaskStore 管「谁在做什么、做到哪」 |
| M7 去分层多 agent 编排 | ✅ 已落地 | `graph.py`：通用 agent 注册表 + LangGraph `Send` 并发扇出；主 agent 调 `dispatch` 元工具产出派发计划，框架负责扇出/join；`max_rounds` 防无限派发；删 `orchestration/policy.py`（升级改子 agent 自报 + 主 agent 决断）；`test_m2_orchestration.py` 重写为新图测试 |
| meta-loop 权重级升级 | 🧊 未启用 | 仓库内无训练链代码；知识级三层已完成，权重级不启用（说明见 §6.3） |

---

## 1. 核心架构立场（原则层）

- **两层分级 agent**：在线强模型（大脑=规划/约束/自反思）+ 本地 qwen3.5-4b（快执行器 + VLM 跑 tool-loop）。
- **控制流归大脑**：程序退化为工具运行时；OCR / 视觉 = 感知工具，click/type/template = 执行工具，由模型自主决策调用。感知工具三角：`observe`(u2层级树) / `ocr_screenshot`(EasyOCR GPU) / `vision_describe`(4B-vl)，三件并列工具、大脑自主选择，无程序化优先级。
- **知识自学、无插件**：world-model + skill 库 = 知识库，agent 运行时自学；游戏专属知识不预置。
- **功能统一（非前端合并）**：模型服务（llama-server）现在给 agent 本地执行器**供推理**——旧范式里「模型服务」与「agent 推理」是两条无关线，新范式后端统一。
- **诚实边界**：自学习只长「知识/策略」，不长「硬件/工具精度」（权重级训练除外，见 §6.3）。

---

## 2. 两层执行架构

### 2.1 大脑层（在线 LLM）
- **接口**：OpenAI 兼容客户端（httpx）；供应商 **OpenAI 兼容供应商**，`base_url` 与 `model` 见 config 示例（demo-model），api_key 走 env `OMNI_BRAIN_API_KEY`（不落库）。
- **上下文 schema / System-2 / 子目标 JSON**：`{objective, subtasks:[str], done_when:str, constraints?:str}`。
- **vision 开关**：起步 `vision=False`（本地 VLM 看懂→文本摘要回传，省钱快）；保留 `vision=True` 升级通道（本地连续搞不定某界面时回传截图直看）。

### 2.2 本地层（qwen3.5-4b-vl）
- **部署形态**：`llama-server`（OpenAI 兼容 HTTP，`external/llama/llama-server.exe`）。**分工**：`llm_runtime/server_backend.py` = OpenAI 兼容客户端（消费侧，`base_url` 默认 `:8085` 但被调用处覆盖为实际端口）；**server 进程启停 / 端口分配 / `--mmproj` / 健康轮询 / `validate_model` 全在 `model_hub/manager.py`**。`router_llm.py`（/v1 网关）读 `manager.processes[name]["port"]` 实际端口传给客户端，**不读 `config.llm_runtime.backend`**。本地 executor 复用 `BrainClient` 同款客户端指向 localhost。
- **tool-loop**：observe→组 payload→brain.chat→dispatch→observe；`task_done` 结束；本地硬校验 `expected` 命中即成功；卡住升级给大脑重规划。
- **视觉通道（已核实）**：`<models_dir>\qwen3_5_4B\mmproj-BF16.gguf` 存在 → 视觉需要 `--mmproj`（`manager._auto_detect_mmproj` 自动挂同目录 mmproj 文件）。
- **可升级**：本地执行器可由 **finetuned GGUF 替换**（见 §6.3 权重级自升级）。

---

## 3. 执行后端抽象（ExecutionBackend）

- **`HostBackend`**：pyautogui + mss（本机），保留。
- **`EmulatorBackend`**：**Android 模拟器 + uiautomator2(ADB)**。API 映射（/Go 通用设备控制范式 → Python `u2`）：
  - `DumpHierarchy()` → `d.dump_hierarchy()`：结构化 UI 层级 XML（元素 bounds/text/resource-id/class），直接喂 world-model + SoM。
  - `set_fastinput_ime(True)` + `send_keys(text)` → **IME 无关文本输入**（根治本机 IME 坑）。
  - `ByResourceID / ByXPath` + `exists() / click()` → 可靠元素操作。
  - `screenshot()` → PIL 图，喂本地 VLM / 大脑 vision。
  - `press_keycode / shell / app_start` → 按键 / shell / 启动。
- **配置**：`config.yaml` 增 `runtime.backend: host|emulator` + `emulator.adb_serial`。
- **前提**：模拟器开 USB/网络调试、`adb connect` 可达；首次 `u2.connect` 推 atx-agent。

---

## 4. 全工具链（M2）

- **SoM 编号点击**：截图上叠加编号/字母包围框（模板匹配 + YOLO + 粗网格）→ VLM 引框号而非原始坐标。
- **模板/精灵匹配**：游戏图标逐帧像素一致，近完美，替代 VLM 易错部分。
- **拖拽 / 滚动 / 长按** 等手势原语。
- **vision 通道**：截图像素→本地 VLM 决策 / 或回传大脑 vision=True 直看。

---

## 5. world-model（运行时学习，✅ M4b.1 已落地，commit dbe71e8）

### 5.1 分层记忆（Hermes 风格，Markdown 落盘 `tasks/<task_id>/world_model.md`）

> **落盘位置（Plan C 生效，2026-08-01）**：世界模型跟 task 走，落 `~/.omniagent/tasks/<task_id>/world_model.md`（per-task 世界状态）。不再有 `app` / `workspace` 分区；无工作目录时由 `auto_project_id()` 兜底 project，但资产仍落 task 目录。详见 §14。
- 长效语义记忆 / 工作记忆 / 情景日志。

### 5.2 情景日志 / 轨迹 schema 与留存（★已定）
- **轨迹 schema**：扁平 `state/action/result`（每步带 `verified` / `retry` 质量信号），**无旧链 `reward` 字段**；设计 §6.2 从轨迹提炼 skill 的数据契约（质量信号替代 reward，见 M5 §3.2）。
- **三级留存策略（窗口已确认）**：

| 层级 | 内容 | 保留策略 | 理由 |
|------|------|----------|------|
| 原始轨迹 `trajectory.jsonl` | 每次运行完整日志 | 滚动窗口 **30 天 / 或 N 次成功运行** 自动 prune（**采集常开**） | 写入零成本；超期 raw 价值衰减 |
| SFT 快照 `dataset_round_N.jsonl` | 每次训练时过滤后的数据集 | **不可变版本快照，永久保留** | 已按质量信号（verified+retry==0）过滤、体积小、真 SFT 语料 |
| 失败轨迹 `failures/` | 低质量（task_failed / 高 retry）/ 卡住片段 | 短窗口 **14 天** 后 prune | 只供 Curator 分析卡点，非 SFT 材料 |
| 训练产物 `*.gguf` | finetuned 执行器 | 版本化保留 **当前 + 上一版**，更早 prune | 受限硬件下省空间 |

- **采集常开**：agent 本来就在跑任务，仅多写一份 JSONL，近零成本；既是未来 finetune 语料，也是 Curator 失败分析原料。

---

## 6. skill 库 + 自升级 meta-loop（M4 / M5）

### 6.1 skill 库（✅ M4b.2 已落地，commit a5fdaa0）
- **格式**：Hermes 风格 `SKILL.md`（Markdown + YAML frontmatter，对齐 agentskills.io 开放标准；与 WorkBuddy 自身 SKILL.md 同构可复用）。
- **录制与晋升**：从轨迹提炼候选 skill（`from_run_record` 提取 candidate）；**晋升门 N=3**（`_check_promotion` 连续成功 3 次才晋升可信，失败归零）。同名合并累计 `success_count`。
- **存储（Plan C 生效，2026-08-01）**：通用 skill 存 `~/.omniagent/skills/<skill>.md`（跨 project 共享）；task 私有 skill 存 `~/.omniagent/tasks/<task_id>/skills/<skill>.md`。recall 合并两层、task 优先。Curator（§6.2）提炼的**通用** skill（带 `scope: global` 标签）落全局，实现「技能升级跨项目复利」；原 `data/skills/<app>/` 与 `app` 分区已随 Plan C 废弃。���

### 6.2 Background Curator（✅ M4b.3 已落地，commit 0d21f47）
- 静默维护进程（**非决策者**，不介入任务执行）：成功动作串→候选 skill（走 N=3 门）；反��卡点→capability_card 补丁。`tool_loop._finish` 末尾自动触发 `Curator.run_once`。
- **运行形态（已定）**：**触发式**——每次顶层任务完成后跑一次；**Phase1 不挂周期性后台定时器**（避免并发写 world-model/skill 的锁复杂度）。四件维护：`prune_trajectories`（raw 30天/failures 14天）+ `refine_world_model`（去重陈旧 facts）+ `review_candidate_skills`（提取 candidate + N=3 晋升判定）+ `flag_low_quality`（高 retry / 高 intervention / 失败 → 标 `excluded_from_sft`）。

### 6.3 meta-loop 四层
1. **skill 级**：合成可复用连招。
2. **world-model 级**：纠正 / 精炼界面理解。
3. **策略级**：大脑反思自身规划失败。
4. **权重级（本地执行器 finetune）**：当前**未启用**（仓库内无训练链代码）。知识级三层（①②③）管*行为适配*，权重级管*执行器能力适配*，二者互补非互斥。

---

### 6.4 知识层自升级（K 系列）✅ 已落地（2026-09-19）

> 自升级主线为**知识级**。本节为索引，
> 完整设计权威见 `doc/plans/知识级自升级_完整设计_K系列_定稿.md`。

**闭环四步**（与 M4 的 Curator 触发式维护同轴，挂载在 `tool_loop._finish`）：

```plain
任务轨迹落盘 → Curator 蒸馏成 rollout → 合并进全局 MEMORY.md → 下次任务弱注入消费
```

- **K0 知识层接通**：`memory/` 子系统（`rollouts/<task_id>.md` + `MEMORY.md` +
  `memory_summary.md` + `merged.json`）+ Curator 第五件维护（蒸馏）+ 弱注入（默认关）。
- **K1 运行积累与可视化**：真机开启注入；memory REST API + 前端「记忆」Tab + 右栏学习提示。
- **K2 信号基础设施**：三实体一致率模型 —— **A**（系统 `success`）/ **B**（模型自报结论）/
  **C**（真值：交互段 C₁ = 用户下一条消息裁决、自主段 C₂ = 终态观测评审）。
  派生 `tasks/<tid>/<run_id>.signal.json` 与 `memory/signals_aggregate.json`；
  端点 `GET /signals`、`/signals/summary`。用于量化「假成功」（A 说成功、C 说没有）。
- **K3 有效性裁决**：`scripts/review_rollouts.py`（蒸馏抽检）+ `scripts/effectiveness.py`
  （ablation 聚合，判据：步数降 ≥15% 且成功率不降）。
- **K4 纠偏采集**：蒸馏第三来源 `## user_corrections`（复用 C₁，双层开关默认关）。
- **K5 稳态运营**：四信号（蒸馏去重命中率 / skill 晋升率 / 步数方差 / 人工介入频率）+
  域收敛判据 → 收敛后 Curator 自动降频（跳过蒸馏）；`GET /signals/steady` + 前端「稳态」Tab。

**红线**：内核零场景硬编码；能力默认关、需显式开启；`trajectory.jsonl` 只读（信号层不写任务原始数据）；
`PUT /memory` 不校验内容；对外口径统一。

**诚实边界**：K2 / K4 / K5 已真机验证；**K3 的 V3 ablation 尚未跑出结论**
（需 ≥20 次同域对照长跑），故不宣称「记忆注入已证明有效」。

---

## 7. 旧训练链（程序化 LoRA）处置（★已更正）

- **全链曾端到端跑通**：`models/qwen_merged/minesweeper/model.safetensors`(3.7GB, Mar16) 是真实合并产物；用户确认 GGUF 当年生成后被删（占空间）。非「从未跑通/末步未完成」。
- **删除 / 弃**：`Training` 前端页（架构升级不适用）、`requirements-train.txt`。
- **归档不删**：`minesweeper` 3.7GB 合并产物（真实训练成果 + 潜在 Phase-2 离线 SFT/蒸馏基座）。
- **轨迹采集器保留**（只采不训）：未来离线 SFT 现成语料。
- **GFW 暴露面在架构升级后大幅缩小**：在线大脑=OpenAI 兼容供应商 API(无 GFW)、demo-model/ADB 控模拟器无需下模型、本地执行器仅一次性拉 base GGUF → 当年卡住的 torch/peft 训练链大头基本消失。当年踩通的镜像/路径经验可沉淀为 skill 备用。

---

## 8. 前端重构（Codex 风格，前端里程碑）

- **交互范式**：对话框下任务 + 实时进展流（SSE）+ 执行中可实时插话。单页 shell（无路由跳转，视图内部 state 切换），两栏布局（Sidebar 任务平铺 + 主区 Chat/技能工具/设置整页覆盖）。
- **页面命运（Plan C 已执行完成，2026-08-01）**：删除 `AgentControl`/`Training`/`Runtime` 整页（X2 遗留、全 404）；`ModelHub` 降级为 Settings 内「模型」Tab；新建 `Chat`/`SkillsAndTools`(Skills/Tools/MCP 三 Tab)/`Settings`(通用/模型/通道/关于) 四页；`react-router-dom` 已移除，改 React Context 自研 `taskStore`（零依赖）。
- **统一 `/chat` 单入口（2026-08-02）**：删除 `/run` 与 `runtimeApi.run`，所有交互统一走 `POST /api/runtime/chat`。一个会话 = 一个 task；首条消息后端自动建 task（落盘）并回填 `task_id`。`/chat` 内部跑双 agent 协作：先 probe planner——无 tool_calls 即纯闲聊（只走主模型，不碰 executor）；有 tool_calls 则转入 `run_task_two_layer`（planner 规划 + executor 执行）。`max_steps` 兜底 + `/stop` 中断。
- **后端对接端点**：SSE 进展流 `GET /api/runtime/stream`、模型状态 `modelApi`/LLM `llmApi`、任务 `taskApi`（`/api/runtime/tasks`）、项目 `projectApi`（`/api/runtime/projects/meta`）、技能 `skillApi`、设置 `settingsApi`（`GET/PUT /api/settings`，含三通道配置）。
- **三通道解耦（2026-08-02）**：Settings → 通道 Tab，planner(brain) / executor / vision 三通道各自独立开关 + base_url/model/api_key 配置，替换原「运行模式三态」下拉。`mode`（dual/online_only/local_only）退化为快捷预设（选 online_only 自动关 executor+vision）。内核 `tool_loop.py` 真实消费（详见 §14.3 与 `agent-control-arch-2026-07-26.html` §12.4）。

---

## 9. 里程碑（M0–M7）

| 里程碑 | 内容 | 依赖 | 状态 |
|--------|------|------|------|
| **M0（硬前提）** | 调试 llama-server 端到端（启动→warmup 完成→实际端口可对话）+ ModelHub 联通。**已修**：llama.cpp 升级 b10107 + Qwen3.5 thinking 关 + ctx 8192（8GB 安全）。GGUF 在 `<models_dir>\qwen3_5_4B\`、`llama-server.exe` 在 `external/llama/`。端口由 manager 自动分配。 | 前置 | ✅ 完成 |
| **M1** | §3 执行后端抽象 + EmulatorBackend(u2) + 模拟器验证 | M0 | ✅ 完成 |
| **M2** | §4 全工具链 + SoM + vision 通道 | M1 | ✅ 完成 |
| **M2.5** | 视觉 SoM + 能力动态注入 | M2 | ✅ 完成 |
| **M3** | ToolProvider 解耦 + 通用工具库（Stage1） + 两层执行器 + GGUF 开关 | M2 | ✅ 完成 |
| **M3b** | §2.1 两层执行编排（在线 plan + 本地 4B 执行 + escalation 升级） | M3 | ✅ 完成 |
| **M4a** | Trajectory 轨迹落盘 + Telemetry 四指标 + 显式 Verification/task_done 门控 + Agent 状态机 | M3b | ✅ 完成（79 passed） |
| **M4b.1** | §5/§12.4 world-model 持久化 + 无状态大脑 + 子目标 checkpoint | M4a | ✅ 完成（commit dbe71e8） |
| **M4b.2** | §6.1 skill 库 + N=3 晋升门 + from_run_record 提取 candidate | M4b.1 | ✅ 完成（commit a5fdaa0，107 passed） |
| **M4b.3** | §6.2 Curator 触发式静默维护（prune/refine/review/flag_low_quality） | M4b.2 | ✅ 完成（commit 0d21f47，128 passed） |
| **M5** | §6.3 meta-loop 四层（权重级） | — | 🧊 未启用（知识级三层已完成，见 §6.3） |
| **M6** | 子任务领取/状态机（SubtaskStore），共享黑板 = WorldModel 改造铺垫 | M5 | ✅ 完成（`task_store.py`） |
| **M7** | 去分层通用多 agent 编排（LangGraph `Send` 扇出 + `dispatch` 元工具 + 删 `policy.py`） | M6 | ✅ 完成（`graph.py`） |

每里程碑独立可验证，逐个人工 green-light；模拟器验证贯穿 M1–M5，M6/M7 为通用多 agent 编排重设计（不限于模拟器场景）。

---

## 10. 已决开放问题（全部收口）

| # | 问题 | 决策 |
|---|------|------|
| 1 | 模拟器选型 | **Android + uiautomator2(ADB)** |
| 2 | 本地模型部署形态 | **llama-server**（OpenAI 兼容 :8085，未验证跑通） |
| 3 | world-model / skill 持久化格式 | **Hermes 风格 Markdown + YAML frontmatter**（agentskills.io） |
| 4 | Curator 频率 | **触发式，不挂周期性定时器**（Phase1） |
| 5 | 权重级升级触发 | **A：阈值 + 弹提示确认**（硬件受限、训练久） |
| 6 | 轨迹留存 | **三级：raw 30天 / failures 14天 / GGUF 留 2 版**（采集常开） |

---

## 11. 风险与诚实边界

- 本地模型吞吐/智能不足 → 两层可能退化成「大脑每步兜底」，需真实跑测定阈值。
- SoM grounding 精度是 RPG 头号失败点（OSWorld 数据）→ 模板匹配兜底必要。
- 模拟器截屏延迟 vs 实时性。
- 自学习幻觉 skill → N=3 门 + 验证必须硬。
- GFW 仍是训练链（权重级）潜在风险，但暴露面已大幅缩小。

---

## 12. 上下文与记忆管理（Context & Memory Management）

> **设计动机（2026-07-26 补充）**：两份主流评审 + 行业实证（Codex / Claude Code / Hermes）表明，「大上下文窗口」与「该不该塞满上下文」是两件事。X3 的 agent 与 Codex 类一次性 coding session 不同——**它会跑小时级甚至跨天的常驻任务**（挂机刷本、长任务链），因此不能靠「开新 session / /clear」续命，必须把「工作上下文」与「会话连续性」彻底分离。本节固化 X3 的上下文与记忆管理设计，作为 M3b（大脑压缩入口）与 M4b（世界模型落盘）的权威依据。

### 12.1 设计原则：两层上下文目标不同

| 层 | 上下文目标 | 管理策略 |
|----|-----------|---------|
| 本地 4B 执行器（worker） | 单 subtask 内自闭环 | **硬滑动窗口 `history_keep:3`**，不做摘要（4B 无摘要能力、ctx 8192 太小） |
| 在线大脑（brain / manager） | 长任务规划 + 约束 + 反思 | **质量压缩（handoff 前瞻摘要）+ token 占比主触发 + 硬上限兜底**；绝不 3 轮硬截断 |

**关键反直觉点**：**1M 上下文 ≠ 不需要压缩**。Claude 官方称其为 "context rot"；Chroma 2025 实测（18 个前沿模型）显示 200K 窗口自 50K token 起性能明显退化（lost-in-the-middle）。Codex / Claude / Hermes **全都在压**——大窗口只给跑大任务的余量，不是不压的理由。

### 12.2 大脑层上下文压缩（已有 config 落点）

**触发机制（取代单纯按轮数）**：纯按 `max_turns` 轮数触发，会在「worker 子任务吐几十 K 终端日志」时于第 N 轮前爆窗——故**主触发改为 token 占比**：

| 阈值 | 值（建议） | 作用 | 行业参照 |
|------|-----------|------|---------|
| `compress_threshold` | **0.65** | 用到窗口 65% 即压，抢在 context rot 前 | Hermes 50% 中段裁剪 |
| `hard_ceiling` | **0.85** | 到 85% 无条件压（兜底） | Hermes gateway 85% |
| `max_turns` | **16（次级兜底）** | 多小轮也压；不截断任务 | Codex 罕见兜底 |

**摘要必须是 handoff（前瞻），不是 backward summary**：含 ①已完成进展 / 关键决策 ②约束与用户偏好 ③剩余待办 ④续做所需关键数据（沿用 Codex handoff prompt 范式）。用户 / 目标原文不丢。

**持久约束块（压缩之外永远注入）**：大脑每次调用都重注入 `plan + constraints` 块（类 Hermes Layer-1 / CLAUDE.md），早期决策永不滑出窗口；压缩只更新摘要，不丢约束块。

> ⚠️ **关键约束**：brain 为**可换模型**（架构模型无关，`BrainClient` 零改动解析标准 `tool_calls`）。当前 `config.brain` 指向 OpenAI 兼容供应商（`base_url=api.example.com/v1`，`model=demo-model`）做模型通用性验证。无论换哪款，**大概率不是 1M 窗口**（前沿 1M 是 GPT-5.4 / Claude Sonnet 5 / Gemini），token 占比必须相对**当前大脑真实窗口**算。落地 `compress_threshold` / `hard_ceiling` 前须先确认其上下文上限（128K / 200K？），否则阈值定错。
> 当前 config 落点：`brain.long_task: {enabled:true, max_turns:16, compress:true}`（已落地，作用于**单大脑 run_task 模式**）；`compress_threshold` / `hard_ceiling` 待补（token 占比主触发）。

### 12.3 本地执行器层（worker hard-keep-3）

`runtime.escalation.worker_history_keep: 3` 已在 config 落地：worker 内层 loop 只保留最近 3 轮历史，按轮硬裁剪防 ctx 溢出；4B 不产出 handoff 摘要（能力所限），故硬滑窗是唯一靠谱选择。与大脑分层机制正确区分（**worker = 存活裁剪，大脑 = 质量压缩**）。

### 12.4 超长任务连续性（无状态大脑 + 持久世界模型 + 检查点）

X3 任务可连续数小时甚至跨天，带来三个 Codex 不用面对的问题：①不能靠 /clear 续命（目标全丢）；②反复压缩 = **JPEG 效应**（多次压缩后早段「为什么做这个」被磨掉，Codex 自身警告过）；③lost-in-the-middle 更严重（t=0 全局目标须活到 t=3h）。

**解法：把大部分状态根本不进窗口**，三层机制：

| 机制 | 作用 | 是否进 LLM 窗口 |
|------|------|----------------|
| **无状态大脑**（每次调用重建上下文） | 大脑每次只收「持久世界模型快照 + 当前子目标 / 升级原因」，不累积历史 → 几乎不触发压缩 | 极小（仅当前轮） |
| **持久外部记忆**（world-model 落盘） | 目标 / 约束 / 已学事实 / 进度，每次调用重注入；压缩怎么压都不丢 | 仅摘要指针注入 |
| **子目标检查点**（checkpoint 落盘） | 子目标边界把世界模型 + 当前子目标写盘；崩溃 / 重启从检查点续，不丢数小时进度 | 不进窗口 |

外加**子目标分解**：一个「刷 2 小时副本」拆成「清房间 1 / 清房间 2 …」，每个子目标给 worker 一个干净本地上下文（`history_keep:3`），只有高层进度回传大脑。单上下文永远小，总任务可无限长。

**避免 JPEG 效应**：压缩时不只产窗口内摘要，而是把关键进展**写回持久世界模型**（落盘）；窗口里只留指针。外部记忆才是连续性的真靠山（Zylos 2026 结论：压缩有损，外部记忆才是续跑靠山）。

**与现有架构的关系（已写入 config 注释，M4b.1 已编码 dbe71e8）**：两层模式下大脑本就是「无状态规划者」——`_plan` 一次 + `_reflect` 仅在 escalate 时调用，且 reflect 只收升级上下文（卡在哪、当前状态）+ 持久世界模型，**不是整个长历史**；消息每次重建，天然不累积。故 `max_turns:16` 从「主压缩触发」**降级为罕见兜底**。持久化世界模型 + 检查点已落地（`world_model.py` save/load/checkpoint/merge_progress），`_plan`/`_reflect` 无状态化（只组装 `world.summary()`），`_compress_history` 接受 world 做 `merge_progress` 回写 facts。

### 12.5 行业参照与实证

- **Codex CLI**：~95% 窗口触发；Handoff 摘要（前向交接：进展 / 决策 / 约束 / 剩余 / 续做数据）；用户消息原文保留，assistant+tool 换成摘要；最近 ~20K token 用户消息保。
- **Claude Code**：接近窗口自动压 + `/compact` 手动；三层渐进（裁工具输出 → prompt cache → 结构化摘要）；系统提示 / CLAUDE.md 压缩后从磁盘重注入。
- **Hermes**：两层（agent 内 50% 窗口 + gateway 85% 兜底）；先**规则裁剪旧工具输出**（去重 / 截断，不调 LLM）→ 再 LLM 摘要中段；`protect_last_n:20` / `protect_first_n:3`；持久 MEMORY.md 层永远注入（压缩只管活窗口）。
- **OpenCode**：阈值触发先 prune 40K 旧工具输出再 compact。
- **实证**：Chroma 2025（18 前沿模型）50K token 起退化；Claude 官方 "context rot"；Zylos 2026 长任务靠「压缩 + 结构化外部记忆 + 子 agent 委派」三层。

### 12.6 已落 config 落点

| 配置项 | 值 | 状态 |
|--------|----|----|
| `brain.long_task.enabled` | true | ✅ 已落地（单大脑 run_task 模式） |
| `brain.long_task.max_turns` | 16 | ✅ 已落地（次级兜底；注释明：本地小模型当大脑单任务时调小到 3~4） |
| `brain.long_task.compress` | true | ✅ 已落地（大脑长任务自动压缩，非 worker 3 轮硬截断） |
| `runtime.escalation.worker_history_keep` | 3 | ✅ 已落地（worker 硬滑窗） |
| `brain.long_task.compress_threshold` | 0.65 | ⏳ 待补（token 占比主触发；落地前需确认 u2 真实窗口） |
| `brain.long_task.hard_ceiling` | 0.85 | ⏳ 待补（硬上限兜底） |
| 持�� world-model 落盘 + 子目标 checkpoint | — | ✅ 已落地（M4b.1, dbe71e8：save/load current.md + checkpoint JSON + merge_progress 防 JPEG） |
| skill 库 + N=3 晋升门 | — | ✅ 已落地（M4b.2, a5fdaa0：SkillLibrary + from_run_record + 连续3次→active） |

### 12.7 红线

- 大脑不做模型路由（§2.1 原则）：上下文压缩 / 升级回流均靠结构固定 + 可量化阈值，不靠大脑实时判断。
- 能力 == 工具红线不变：上下文管理属于运行时机制，不引入场景硬编码。
- worker 永不误用大脑策略、大脑永不误用 worker 策略：**worker = 存活裁剪（硬滑窗），大脑 = 质量压缩（handoff 摘要）**，二者不可混用。

---

## 13. 参考

- **Hermes Agent**（NousResearch, MIT）：自写 SKILL.md + Background Curator + 三层记忆 + ShareGPT 轨迹导出。→ 印证 world-model+skill 自学范式，skill 格式对齐 agentskills.io。
- **UI-TARS / UI-TARS-2**（ByteDance, arXiv 2501.12326 / 2509.02544）：原生 GUI 端到端；System-2 推理；**Iterative Training w/ Reflective Online Traces**（从在线/错误轨迹学、少人工）。→ 印证 §6.3 权重级闭环，端到端训练天花板高于 prompt 包装。
- **MGA: Memory-Driven GUI Agent**（arXiv 2510.24168）：Observer(qwen-vl)→Memory→Planner→Grounding。→ 近乎本架构论文版（本地快 VLM 感知 + 远程强模型规划）。
- **OSWorld**（os-world.github.io）：真实电脑环境 369 任务，人类 72% / 最强模型 12–54%；75–94% wall time 花在规划 LLM 调用 → 印证「大脑低频、本地快执行」延迟设计。
---

## 14. 全局目录与配置覆盖

> **来源**：设计讨论锁定。原 `data/` 下散落目录整体迁移至两层 `.omniagent/` 结构；config 明文 key 移出仓库。代码路径常量随之修改（详见 `agent-control-arch-2026-07-26.html` §12）。
> **更新（2026-08-01，Plan C 已落地）**：原 `workspace` 概念与 `app` 分区已**从内核彻底删除**（北极星红线），改为 **project（路径 slug）+ 独立 task** 两实体模型。本节约 §14.1–§14.4 旧 workspace 描述作废，以下为当前生效模型。

### 14.1 两层目录模型（Plan C 生效）

| 层 | 路径 | 内容 |
|----|------|------|
| 用户全局 | `~/.omniagent/` | `config.yaml`（去明文 key + `local_model.auto_start`）、`skills/`（通用，跨 project）、`memory/`（每会话全量加载）、`projects/<path-slug>/`（会话历史 jsonl）、`tasks/<task_id>/`（任务资产）、`rules/` |
| 任务资产 | `~/.omniagent/tasks/<task_id>/` | `task.json`（元信息）、`trajectory.jsonl`、`world_model.md`、`collected.json`、`skills/`（任务私有，可选） |

- **project** = 工作目录的逻辑分组，id = 路径 slug（如 `d-AI-OmniAgent`），仅承载会话历史，**不装资产**。
- **task** = 一等实体，独立平铺在 `tasks/<task_id>/`，自身含所有资产；可关联一个 `project_id`（仅作 cwd 元数据，不影响落点）。
- 全局通用 skill / memory 跨 project 共享；task 资产跟 task 走。
- `<repo>/.omniagent/` 项目级层**已取消**（随 `app` 一起废弃）。

### 14.2 Task 归属与兜底

- **指定工作目录** → project_id = `slugify_path(绝对路径)`，会话历史落 `projects/<project_id>/`，task 资产仍落 `tasks/<task_id>/`（资产与 project 解耦）。
- **无工作目录（GUI/游戏等）** → `auto_project_id()` 自动造 `~/OmniAgent/<date>-task-N`，永远有 project 归属，避免 null 分支。
- **task 索引** `~/.omniagent/tasks/<task_id>/task.json` 存元数据，前端历史 task list 从此读。

### 14.3 Config 去 key

- 明文 API key **只存** `~/.omniagent/config.yaml`；仓库 `config.yaml` 去明文，启动时被全局覆盖。
- **三通道解耦（2026-08-02 取代 mode 三态为主控）**：planner(brain) / executor(runtime.executor) / vision(runtime.vision) 三个通道**各自独立 enable + base_url/model/api_key**，由 Web 设置面板「通道」Tab 经 `PUT /api/settings`（`router_settings.py`，白名单含 `runtime`/`brain`/`executor`/`vision`/`local_model`）写入 `~/.omniagent/config.yaml`。内核 `tool_loop.py` 消费：`executor.enabled` 决定起独立本地 4B 第二路（`tool_loop.py:147`），未启用则 executor 角色由 planner 主模型兼任（`executor_is_planner=True`，仍两层，不退化）。`/chat` 读 `config.load_config()` 合并后的 settings，即时生效（`PUT` 后 `reload_config()` 失效缓存）。
- `runtime.mode`（`dual`/`online_only`/`local_only`）**保留为快捷预设**：选中 online_only 自动把 executor/vision 置 `enabled=false`，dual 置 true。行为由三通道开关决定，`mode` 仅作批量预设。
- `local_model.auto_start`：是否自动拉起本地 llama-server。
- `runtime.trajectory.dir` / `runtime.world_model.dir`：留空即走默认项目级 `tasks/<task_id>/` 路径（由 `agents/local/runtime_paths.py` 接管）。

### 14.4 路径常量（Plan C 生效）

内核路径统一由 `agents/local/runtime_paths.py` 提供：`global_skills()` / `projects_root()` / `project_dir(pid)` / `tasks_root()` / `task_dir(tid)` / `task_trajectory(tid)` / `task_world_model(tid)` / `task_collected(tid)` / `task_json(tid)` / `slugify_path()` / `auto_project_id()`。所有 `app` / `workspace` 分区逻辑已删除，红线自检：`agents/` 全目录搜不到 `app` 作为分区键。

---

## 15. 进度追踪与下一步

> 原活动 spec `me_to_AI.md`（任务指令）/ `AI_to_ME.md`（状态同步）已于 2026-09-11 并入本文件，本文件为**唯一 spec 事实来源**。能力状态基线见 §0；本节只收**前瞻追踪项**（原 `AI_to_ME.md`「下一步 / 风险 / 关键决策」的增量）。

### 15.1 下一步

1. **M4b.3 Curator 触发式静默维护**（`prune_trajectories` + `refine_world_model` + `review_candidate_skills` + `flag_low_quality`）：任务完成后跑，不挂周期性后台定时器。✅ 已落地（2026-08-22，commit 0d21f47，128 passed）。
2. **X3 设计符合性优化（P0–P3）**（已落地）：`/stop` 置 `aborted`、agent 回包持久化、前端标题以服务端 `objective` 为主源、非 loopback 绑定无 `auth_token` 拒绝启动、补 API 集成测试。
3. 可选：跑真在线两层闭环验证，看 skill 库 + world-model 持久化在真机的产出。
4. 后续独立项（非阻塞）：管理 / 运行接口的请求级 token 鉴权、模型路径 allow-list。

### 15.2 风险补充

- 本地模型吞吐 / 智能不足 → 两层可能退化成「大脑每步兜底」，需真实跑测定阈值（同 §11）。
- SoM grounding 精度是 RPG 头号失败点 → 模板匹配兜底必要（同 §11）。
- **GFW**：新架构暴露面已大幅缩小（在线大脑 = 国内 API、u2 不下载模型、本地执行器仅一次性拉 base GGUF）；真正碰墙只剩「拉基座 GGUF」一次。

### 15.3 关键决策（固化追踪）

- 模拟器 = Android + uiautomator2/ADB（IME 无关）；本地执行器 = `llama-server`（OpenAI 兼容 `:8085`），复用 `BrainClient`。
- world-model / skill 持久化 = Hermes 风格 Markdown + frontmatter（agentskills.io 同构）；`data/skills/<app>/<skill>.md`。
- **大脑不做模型路由**（§2.1）：子任务按架构约定一律走本地 4B；升级回流靠可量化条件（verify 连败 3 / 内层 8 步 / 墙钟 120s / no_confidence / ctx 溢出）。
- **task_done 必须 verify**（§未变）：大脑调 `task_done` 时强制跑 `_verify`，通过才接受；失败→拒绝 + 回 observe 重规划（防幻觉式完成），无条件时信任大脑。
- 权重级升级（LoRA 微调）= 阈值 A 触发 + **弹提示确认**（硬件受限、训练久）；数据来源为强大脑驱动 agent 的高质量轨迹。
- 轨迹留存 = 三级（raw 30 天 / failures 14 天 / GGUF 2 版），采集常开。
