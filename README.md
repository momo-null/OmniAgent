<a name="top"></a>

# OmniAgent X3 · 通用 Agent 内核

**中文** | [English](#english)

> **OmniAgent** 是一个**通用** Agent 内核：主 agent + graph 自主派发（主 agent 决定派不派、派给哪个执行单元槽位），模型来自多槽模型目录（`~/.omniagent/models.json`）。模型通过调用**工具**完成任务，能做什么完全由工具清单决定，内核不绑定任何场景；GUI / 设备操控只是当前用得最多的场景，而非 agent 的预设身份。
>
> **构建方式（诚实说明）**：ReAct 循环与 function-call 协议由 **OpenAI Agents SDK**（`Runner`）承载，多 agent 状态机编排由 **LangGraph** 承载。本项目自研的是 **L2 编排层**（收尾门控 / 升级判定 / 预算与墙钟 / 上下文管理）、**工具运行时与插件层**、**知识层**（世界模型 / 技能 / 全局记忆）与 **执行后端**（设备 / GUI）。因此本项目的贡献是「编排 + 工具 + 知识层」，**不是 agent 循环本身**。
>
> **技术底座**：`OpenAI Agents SDK` · `LangGraph` · `MCP` · 本地模型生命周期管理 · 知识层（技能库 / 用户画像；记忆轴按 TencentDB Agent Memory 移植重建中）。
>
> **版本 X3**：主 agent 规划并执行；可按槽位为子 agent 配置不同模型（含本地快模型做执行与视觉感知）。
> ⚠️ 免责声明：个人学习项目，不保证稳定性与体验，使用产生的一切后果自行承担。

## 能力与边界（先看这个 · 30 秒了解本项目到哪一步）

**已实现并真机验证**
- 主 agent 执行循环（OpenAI Agents SDK）+ 自主派发：互不依赖的子任务可派给按槽位配置的执行单元。
- 通用工具运行时：加能力 = 写工具 / Provider，内核零场景硬编码（红线 lint + CI 守护）。
- 本地模型生命周期管理、MCP 接入、GUI / 设备操控执行后端。
- 会话记录增量落盘 + 重放合并（崩溃不丢过程流）；项目级 AGENTS.md 三层纪律注入（global → project → task）；技能库（人工维护 + 按需加载）。
  （skill 提取链修订：录像式机械转录整链已于 2026-10-03 A9 删除；2026-10-04 skill 轴以**可重放宏缓存**形态保留但默认关闭——结构化提取 + 回放执行器，`skill.auto_distill` 默认关，有效性验证未做，见下。）

**尚未验证（诚实边界）**
- 记忆轴处于**重构期**：新管线按 **TencentDB Agent Memory** 移植重建（设计见 `doc/plans/memory-rag-design.md`），有效性验证未开始。
- **skill 宏轴（2026-10-04 保留，默认关）有效性未验证**：hachimi 真机试点已收口——机制层跑通（蒸馏链 5/5 成功、entry_id 精确分结构），但**注入技能序列零步数收益**（Δsteps=0，简单 UI 任务）⇒ 价值全系于回放执行器（跳过每步 LLM 决策），收益 A/B 待在 Omni 上以游戏脚本 / 确定性工具链场景补测；`skill.auto_distill` 默认关。
- 单域、小样本，非生产级。
- skill 驱动的角色化团队（team-mode）**规划中**：将复用派发底座的 `agent` 字段与执行单元注册表（见 `doc/plans/`），尚未实现。

## 文档导航（权威来源）

| 文档 | 角色 |
|------|------|
| [`doc/agent-control-product-design.html`](doc/agent-control-product-design.html) | 产品设计（定位 / 原则 / 技术选型 / 行业对标 / 维护约定） |
| [`doc/agent-control-arch-design.html`](doc/agent-control-arch-design.html) | 架构设计（图集 N1–N10） |
| [`doc/agent-control-master-spec.md`](doc/agent-control-master-spec.md) | **架构决策权威（单一事实来源）** |
| [`spec/coding_standard.md`](spec/coding_standard.md) | 工程规范（X3 现行编码规范） |

> ⚠️ 上述设计文档均为中文。

## 1. 产品定位

- **它是什么**：通用 Agent 内核（运行时）。控制流归大脑，程序退化为工具运行时（ToolLoop 薄壳）；ReAct 循环由 OpenAI Agents SDK 承载。
- **能力 == 工具**：加能力 = 写工具 / Provider，绝不改内核。GUI / 设备操控是经工具清单动态注入的能力，而非 agent 身份。

> 详细定位、目标用户、与旧版差异见 **产品设计 §1**。

## 2. 核心原则与红线

七条不可偏移的原则，是技术选型总依据，也是代码审计「嗅觉测试」：

1. **控制流归大脑** — 模型规划并决策调工具，程序退化为工具运行时。
2. **知识自学，无插件** — world-model + skill 由运行时观测积累，属运行时知识非硬编码。
3. **能力 == 工具（红线）** — 内核只负责通用闭环，加能力 = 写工具 / Provider。
4. **模型无关** — 能力声明来自模型元数据，动态注入提示词，换模型零代码改动。
5. **派发可选** — 是否分层、分层用哪些模型，由模型目录配置决定；不配置即单主 agent 形态。
6. **诚实边界** — 自学习只长「知识 / 策略」，不长「硬件 / 工具精度」；新 skill 须连续成功 N 次才晋升。
7. **超长任务连续性** — 无状态大脑 + 持久 world-model + 子目标检查点，使小时 / 跨天任务不丢目标、崩溃可续。

> **红线**：改动若给 `omni_core/` 核心加了针对某 app / 游戏 / 任务的判断 → 必须抽成工具 / skill。`omni_core/` 核心零场景硬编码（不得出现任何具体 app / 游戏 / 业务的专有 token）；CI 由 `scripts/review_lint.py` 守护（R1 屏幕字段泄露 / R2 具体工具名分支 / R3 缺抽象方法）。详见 **产品设计 §2** 与 **master-spec**。

## 3. 技术选型

| 层 | 技术 |
|----|------|
| Agent 循环 | **OpenAI Agents SDK**（`Runner` 驱动 ReAct 循环 + 流式事件） |
| 状态机编排 | **LangGraph**（`Send` 并发扇出 + `update_state` 软注入） |
| 后端 | Python + FastAPI / uvicorn（`backend/`） |
| 本地推理 | llama.cpp（`llama-server`），本地模型作子 agent 槽位模型 / VLM |
| 前端 | TypeScript + React 18 + Vite + MUI + Tailwind（单页 Shell，自研 store） |
| 视觉 | SoM（Set-of-Marks）+ EasyOCR + YOLO 降级 |

> 选型理由与行业对标（OSWorld / Codex 模式等）见 **产品设计 §3–§4**。

## 架构速览

两层 + Escalation 的通用 Agent Runtime：`Brain（在线规划）` → `Tool Runtime（SDK Runner 内层循环 + LangGraph 编排）` → `tool 插件层（device / vision / python / mcp 平级）` → `执行后端（devices/，L1，内核外）` → `Environment`。知识层旁路：`技能库 + 用户画像 + AGENTS.md 纪律 → 注入消费`（记忆轴按 TencentDB Agent Memory 移植重建中，见 `doc/plans/memory-rag-design.md`）。

> 全部架构图（分层框架 / MCP 解耦 / ReAct 闭环 / Vision SoM / 知识层（TAM）/ 两层执行 Escalation / 上下文管理 / 持久化检查点 / Project-Task 模型 / 前端设计）见 **架构设计 N1–N10**。

## 目录地图

| 路径 | 作用 |
|------|------|
| `omni_core/brain/` | 模型客户端 + 工具 schema + providers + SDK 循环（`sdk_loop.py`） |
| `omni_core/local/` | 世界模型 / 观测 / ToolLoop（`loop/` 五 Mixin）/ skill / trajectory / telemetry / states |
| `omni_core/tools/` | 四跳隔离工具插件层（device / vision / python / mcp 平级） |
| `omni_core/orchestration/` | 通用多 agent 编排（LangGraph `Send` 扇出） |
| `devices/` | 执行后端 L1（EmulatorBackend u2/ADB · HostBackend pyautogui），已从内核迁出 |
| `llm_runtime/` | 模型无关推理后端（server / llama / api） |
| `model_hub/` | 模型元数据 + llama-server 启停 / 端口 / `--mmproj` 探测 |
| `backend/` | 后端 HTTP 服务 + API 路由（`api/routers/` 按域拆分） |
| `web/` | 前端（Chat / SkillsAndTools / Settings 三页，自研 store） |
| `tests/` | 测试 + 红线守护 |

## 快速开始

```bash
python -m venv .venv && .\.venv\Scripts\activate && pip install -r requirements.txt
cd web && npm install && cd ..    # 前端依赖需手动装一次（脚本只提示不自动装）
.\start_all.bat        # 后端 :8000 + 前端 :5173 一键起
# 可选：为 worker 等槽位配置本地模型——llama-server 加载本地 VLM GGUF（默认 :8085）
```

- 真 API key 与三通道配置仅存 `~/.omniagent/config.yaml`，仓库 `config.yaml` 已去明文；**首次启动后请先到 Web「设置」页配置模型端点与 API key**，否则对话无法调用模型（后端日志出现「配置文件未找到」属正常）。
- 默认 `runtime.backend: host`（宿主机屏幕）；emulator 需自备 `adb_serial`。
- 派发（dispatch）默认开启，单模型即可用（主 agent 自派发，并发与上下文隔离仍有价值）；要分层就两步：在 `~/.omniagent/models.json` 的 `defaults` 里声明槽位（如 `worker` / `researcher`），再在 `~/.omniagent/config.yaml` 的 `runtime.dispatch.agents` 里显式列出参与派发的槽（首个为默认槽）。
- 前端访问用 `http://localhost:5173`（dev server 监听 IPv6 `::1`，`127.0.0.1` 可能连不上）；后端无窗口运行是刻意的（日志见 `backend.log` / `backend.err`），且**不要给 uvicorn 加 `--reload`**（Windows 下双进程抢绑端口会随机挂起）。

## 红线守护

`scripts/review_lint.py` 扫描 `omni_core/` 与 `devices/`，检测内核是否写死领域假设（R1 屏幕字段泄露 / R2 具体工具名分支 / R3 缺抽象方法），守护「内核零场景硬编码」。任何改动若给核心加场景判断，必须抽成工具 / skill。

CI（`.github/workflows/redline-lint.yml`）在每次 push / PR 跑 `python scripts/review_lint.py --strict`（纯标准库、秒级反馈）；**刻意不跑 pytest**（测试依赖 torch / easyocr / opencv / pyautogui 等原生包）。

<a name="english"></a>

---

# OmniAgent X3 · A General-purpose Agent Kernel

[中文](#top) | **English**

> **OmniAgent** is a **general-purpose** agent kernel: a main agent + graph-driven autonomous dispatch (the main agent decides whether and which executor slot to dispatch to), with models drawn from a multi-slot model catalog (`~/.omniagent/models.json`). The model completes tasks by calling **tools**; what it can do is fully determined by the tool list, and the kernel binds to no particular scenario. GUI / device control is merely the most-used application today, not the agent's predefined identity.
>
> **How it is built (honest note)**: the ReAct loop and the function-call protocol are delegated to the **OpenAI Agents SDK** (`Runner`), and multi-agent state-machine orchestration to **LangGraph**. What this project builds itself is the **L2 orchestration layer** (termination gating / escalation decisions / budget & wall-clock / context management), the **tool runtime & plugin layer**, the **knowledge layer** (world model / skills / global memory) and the **execution backends** (device / GUI). So the contribution here is "orchestration + tools + knowledge layer", **not the agent loop itself**.
>
> **Version X3**: the main agent plans and executes; sub-agents can be configured with per-slot models (including a local fast model for execution and visual perception).
> ⚠️ Disclaimer: a personal learning project; stability and UX are not guaranteed; use at your own risk.

## Capability & Boundary (read this first · a 30-second overview of where the project stands)

**Implemented and verified on real hardware**
- Main-agent execution loop (OpenAI Agents SDK) + autonomous dispatch: independent subtasks can be delegated to executor units configured per slot.
- General tool runtime: adding a capability = writing a tool / provider; zero scene-specific logic in the kernel (enforced by a red-line lint + CI).
- Local model lifecycle management, MCP integration, GUI / device execution backends.
- Session records are persisted incrementally with replay coalescing (crash-safe); three-layer AGENTS.md discipline injection (global → project → task); skill library (manually maintained + on-demand loading).
  (Skill-chain revision: the mechanical-transcription chain was deleted on 2026-10-03 (A9); on 2026-10-04 the skill axis was retained as a **replayable macro cache** — default off — with structural extraction + replay executor, `skill.auto_distill` off by default, effectiveness unverified, see below.)

**Not yet verified (honest caveat)**
- The **memory axis is under reconstruction**: the new pipeline is a direct port of **TencentDB Agent Memory** (design in `doc/plans/memory-rag-design.md`). Effectiveness validation has not started.
- **Skill macro axis (retained 2026-10-04, default off) effectiveness unverified**: the hachimi real-device pilot closed with the mechanism validated end-to-end (distillation chain 5/5 runs, entry_id correctly distinguishing structural variants) but **zero step savings from injecting the skill sequence** (Δsteps=0 on a simple UI task) ⇒ value hinges entirely on the replay executor (skipping per-step LLM decisions); the A/B is still pending on Omni in game-script / deterministic-toolchain scenarios; `skill.auto_distill` is off by default.
- Single domain, small sample size; not production-grade.
- A skill-driven role-based team (team-mode) is **planned**: it will reuse the dispatch foundation's `agent` field and executor registry (see `doc/plans/`); not yet implemented.

## Documentation (authoritative sources)

| Document | Role |
|----------|------|
| [`doc/agent-control-product-design.html`](doc/agent-control-product-design.html) | Product design (positioning / principles / tech choices / industry comparison / conventions) |
| [`doc/agent-control-arch-design.html`](doc/agent-control-arch-design.html) | Architecture design (diagrams N1–N10) |
| [`doc/agent-control-master-spec.md`](doc/agent-control-master-spec.md) | **Authoritative architecture decisions (single source of truth)** |
| [`spec/coding_standard.md`](spec/coding_standard.md) | Engineering standard (current X3 coding conventions) |

> ⚠️ The design documents listed above are written in Chinese.

## 1. Positioning

- **What it is**: a general-purpose agent kernel (runtime). Control flow lives in the brain; the program degrades to a tool runtime (a thin ToolLoop shell). The ReAct loop itself is provided by the OpenAI Agents SDK.
- **Capability == tool**: adding a capability = writing a tool / provider — never modifying the kernel. GUI / device control is a capability dynamically injected via the tool list, not the agent's identity.

> See **Product Design §1** for detailed positioning, target users and differences from earlier versions.

## 2. Core Principles & Red Lines

Seven non-negotiable principles — the basis for all technical choices and a "smell test" for code review:

1. **Control flow lives in the brain** — the model plans and decides which tools to call; the program is only a tool runtime.
2. **Knowledge self-learned, no plugins** — the world model + skills are accumulated from runtime observation: runtime knowledge, not hard-coding.
3. **Capability == tool (red line)** — the kernel only handles the general loop; adding a capability = writing a tool / provider.
4. **Model-agnostic** — capability declarations come from model metadata and are injected into prompts dynamically; swapping models requires zero code changes.
5. **Dispatch is optional** — whether to tier and which models each tier uses are decided by the model catalog; unconfigured, the runtime is a single main agent.
6. **Honest boundary** — self-learning only grows "knowledge / strategy", never "hardware / tool precision"; a new skill must succeed N times in a row before being promoted.
7. **Ultra-long task continuity** — a stateless brain + a persistent world model + sub-goal checkpoints let hour- or day-scale tasks keep their goal and resume after a crash.

> **Red line**: if a change adds an app / game / task-specific branch to `omni_core/`, it must be extracted into a tool / skill. `omni_core/` must contain zero scene-specific hard-coding (no proprietary token of any specific app / game / business); this is guarded in CI by `scripts/review_lint.py` (R1 screen-field leakage / R2 concrete-tool branching / R3 missing abstract methods). See **Product Design §2** and **master-spec**.

## 3. Tech Stack

| Layer | Technology |
|-------|------------|
| Agent loop | **OpenAI Agents SDK** (`Runner` drives the ReAct loop + streaming events) |
| State-machine orchestration | **LangGraph** (`Send` fan-out + `update_state` soft injection) |
| Backend | Python + FastAPI / uvicorn (`backend/`) |
| Local inference | llama.cpp (`llama-server`); the local model serves sub-agent slots / VLM |
| Frontend | TypeScript + React 18 + Vite + MUI + Tailwind (single-page shell, hand-rolled store) |
| Vision | SoM (Set-of-Marks) + EasyOCR + YOLO fallback |

> Rationale and industry comparison (OSWorld / Codex patterns, etc.) in **Product Design §3–§4**.

## Architecture Overview

A two-tier + Escalation general-purpose agent runtime: `Brain (online planning)` → `Tool Runtime (SDK Runner inner loop + LangGraph orchestration)` → `tool plugin layer (device / vision / python / mcp, all peers)` → `execution backend (devices/, L1, outside the kernel)` → `Environment`. Knowledge-layer side path: `skill library + user profile + AGENTS.md discipline → injection` (the memory axis is being re-built as a TencentDB Agent Memory port, see `doc/plans/memory-rag-design.md`).

> All architecture diagrams (layered framework / MCP decoupling / ReAct loop / Vision SoM / knowledge layer (TAM) / two-tier execution escalation / context management / persistence checkpoints / Project-Task model / frontend design) are in **Architecture Design N1–N10**.

## Repository Map

| Path | Purpose |
|------|---------|
| `omni_core/brain/` | Model clients + tool schema + providers + SDK loop (`sdk_loop.py`) |
| `omni_core/local/` | World model / observation / ToolLoop (`loop/`, five mixins) / skill / trajectory / telemetry / states |
| `omni_core/tools/` | Four-hop isolated tool plugin layer (device / vision / python / mcp, all peers) |
| `omni_core/orchestration/` | General multi-agent orchestration (LangGraph `Send` fan-out) |
| `devices/` | Execution backend L1 (EmulatorBackend u2/ADB · HostBackend pyautogui), moved out of the kernel |
| `llm_runtime/` | Model-agnostic inference backend (server / llama / api) |
| `model_hub/` | Model metadata + llama-server start/stop / ports / `--mmproj` detection |
| `backend/` | Backend HTTP service + API routes (`api/routers/`, split by domain) |
| `web/` | Frontend (Chat / SkillsAndTools / Settings pages, hand-rolled store) |
| `tests/` | Tests + red-line guards |

## Quick Start

```bash
python -m venv .venv && .\.venv\Scripts\activate && pip install -r requirements.txt
cd web && npm install && cd ..    # frontend deps must be installed once (the script only warns)
.\start_all.bat        # starts backend :8000 + frontend :5173
# optional: configure a local model for the worker slot — llama-server loading a local VLM GGUF (default :8085)
```

- The real API key and three-channel config live only in `~/.omniagent/config.yaml`; the repo's `config.yaml` carries no plaintext secrets. **On first launch, configure your model endpoint and API key in the Web Settings page** before chatting ("config file not found" in backend logs is normal on a fresh setup).
- Default `runtime.backend: host` (host machine screen); emulator mode requires your own `adb_serial`.
- Dispatch is enabled by default and works with a single model (the main agent self-dispatches; concurrency and context isolation still add value). To tier it up: declare slots in the `defaults` section of `~/.omniagent/models.json` (e.g. `worker` / `researcher`), then explicitly list the dispatch-enabled slots in `runtime.dispatch.agents` of `~/.omniagent/config.yaml` (the first one is the default slot).
- Open the frontend at `http://localhost:5173` (the dev server listens on IPv6 `::1`; `127.0.0.1` may not connect). The backend intentionally runs windowless (see `backend.log` / `backend.err`), and **do not add `--reload` to uvicorn** (on Windows the reloader double-binds the port and connections randomly hang).

## Red-Line Guard

`scripts/review_lint.py` scans `omni_core/` and `devices/` for hard-coded domain assumptions (R1 screen-field leakage / R2 concrete-tool branching / R3 missing abstract methods), guarding "zero scene-specific hard-coding in the kernel". Any change that adds a scene branch to the core must be extracted into a tool / skill.

CI (`.github/workflows/redline-lint.yml`) runs `python scripts/review_lint.py --strict` on every push / PR (pure stdlib, sub-second feedback). It deliberately does **not** run pytest (the tests depend on native packages such as torch / easyocr / opencv / pyautogui).
