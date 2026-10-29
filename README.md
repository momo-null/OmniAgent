<a name="top"></a>

# OmniAgent · 能聊、能记、能干活的常驻 AI 伙伴

**中文** | [English](#english)

> 跑在自己电脑上的 AI 伙伴：桌面端陪你聊、跨会话记得你、能替你在 QQ / 飞书 / 企微 里说话、按点把活干了，需要时还能直接操作这台电脑或一台安卓机。
>
> 它不是界面演示：记忆（原子记忆 → 场景块 → 画像）、语音收发、多 IM 通道、定时任务、本地模型、MCP、插件、人工审批都是打通的完整链路。做不到的部分写在[边界](#边界诚实说明)里，不吹。

## 30 秒看懂

| 你想要 | 它实际做的 |
|---|---|
| 有个能连续聊的助手 | 桌面端 / 浏览器单页对话，流式输出、工具过程可见、可打断、可排队、可给单个任务开「完全访问」 |
| 它记得我 | 每轮对话自动沉淀：原子记忆库 → 场景块 → 用户画像；下次开口就能召回，画像可手改 |
| 帮我回消息 | QQ（NapCat）、飞书、企微常驻通道，以**主人的身份**代答；群聊需 @，支持语音进 / 语音出、图片理解 |
| 到点自己干活 | cron 定时任务，跑完把结果推回指定通道，Web 里可增删改查 + 看运行记录 |
| 真的动手 | 键鼠 / 截图 / 屏幕观测（本机），或 ADB 操作安卓机；文件读写检索、网页抓取、联网搜索开箱可用 |
| 用我自己的模型 | 云端 API 与本地 llama.cpp（GGUF）混用；可按槽位给子 agent 配不同模型，含本地视觉模型 |
| 我要接自己的东西 | MCP server（stdio / streamable HTTP）、插件（写一个就能加能力）、新增 IM 通道 = 一个适配器文件 |

## 三个典型用法

### 1. 当日常助手（最快路径）

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat      # 后端 :8000 + 前端 :5173
```

打开 `http://localhost:5173` → **设置 → 模型** 填 API endpoint 与 key（或改成本地 GGUF）→ 直接聊。
真密钥只存 `~/.omniagent/config.yaml`；仓库里只有 `config.example.yaml`（无任何明文密钥）。

不想开两个窗口就用桌面端（见[快速开始](#快速开始)）。

### 2. 让它替你在 QQ 上说话

1. 装 [NapCat](https://github.com/NapNeko/NapCatQQ)，开启 HTTP（默认 `:3000`）与 WebSocket 服务（默认 `:3001`）；
2. `~/.omniagent/config.yaml` 的 `channels.napcat` 填 `enabled` / `http_url` / `ws_url` / `token`；
3. 起网关：`python -m gateway.channel_gateway`（桌面端会自动拉起，不用手动跑）；
4. 私聊直接回；群聊**必须 @ 它**才响应（`require_at`），语音进来会用语音回、图片会真的看懂。

外部通道默认**纯对话**（`allow_tools: false`，模型侧连工具清单都没有）：别人给你发消息可以聊天，但碰不到你的文件和电脑。飞书 / 企微同构，各自一段配置即可。

### 3. 让它定时干活并回消息

Web「定时任务」页新建（或改 `~/.omniagent/cron/jobs.json`，热加载不用重启）：写一句 objective + cron 表达式 + 完成条件，选 `notify_channel` 指定结果推到哪个通道。运行记录与失败原因都能在页面看到。

## 功能清单

| 功能 | 说明 | 默认状态 |
|---|---|---|
| 桌面 / Web 对话 | 流式回答、思考与工具步骤可见、随时打断、消息排队、产物可开文件夹 | 开箱 |
| 长期记忆 | 会话 → 原子记忆（`memory/brain.db`）→ 场景块 → 用户画像；相关性 + 重要度 + 新近度融合召回 | 开箱 |
| 人格 / 角色卡 | 多角色切换，人格随会话持久化，并作为记忆召回的过滤维度 | 开箱 |
| 项目与任务 | 项目 = 工作目录 + 项目级知识，任务 = 一次会话；右键改名、保存到项目、删除、打开文件夹 | 开箱 |
| IM 通道 | QQ / 飞书 / 企微，群 @ 触发、私聊白名单、语音对语音、图片多模态、跨会话回看消息；QQ 侧「想回才回」（模型显式发消息，独白不外发） | 需配通道 |
| 定时任务 | cron 触发 + 错过补偿 + 结果推送 + 运行记录 | 需跑网关 |
| 语音 | 本地 SenseVoice 转写 + Kokoro 合成；开启后 Web 输入框按住说话，QQ 语音自动转写并语音回复 | 插件默认关 |
| 看图 | 截图 / QQ 图片作为当轮图片输入直传模型；可选 OCR（RapidOCR）、可选 SoM 标注点击 | 开箱（模型需支持视觉） |
| 操作本机 | 键鼠、截图、屏幕观测 | `plugins.host` 默认关 |
| 操作安卓 | uiautomator2 / ADB：触控、按键、层级树、截图、文件 | `plugins.emulator` 默认关 |
| 文件与联网 | 文件读 / 写 / 改 / 列举 / 递归检索；HTML 抓取转文本；无密钥联网搜索 | 开箱 |
| 技能库 | 手写技能按需加载，项目级 / 全局两级，可一键提升为全局 | 开箱 |
| 模型自由 | 云端 API + 本地 llama.cpp；多槽位模型目录，子 agent 可用不同（含本地快模型） | 开箱 |
| 扩展 | MCP server、插件（`plugins/`）、新 IM 通道（`gateway/channels/`） | 开箱 |
| 安全 | 敏感工具可要求人工审批（Web 弹窗，含审计日志）；外部通道默认无工具；每任务「完全访问」开关 | 开箱 |

## 快速开始

### Windows 一键

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat                        # 后端 :8000 + 前端 :5173
python -m gateway.channel_gateway      # 可选：IM 通道 + 定时任务
.\start_all.bat stop                   # 停掉
```

首次启动后请到 Web「设置」页配置模型端点与 API key，否则对话无法调用模型（后端日志出现「配置文件未找到」属正常）。

### 桌面应用（推荐日常使用）

```bash
cd desktop
npm install          # 首次；.npmrc 已配国内镜像
npm run dev          # vite + 后端 + 网关 + Electron 一键起
npm run dist         # 打包 NSIS 安装包 + portable exe（desktop/dist/）
```

也可以回到仓库根目录跑 `pack_desktop.bat`（顺带先构建 `web/dist`）。桌面端会自动拉起后端与网关，关窗口缩到托盘（托盘可重启后端 / 看日志 / 退出）。打包产物是**壳**，后端与 `.venv` 仍指向本仓库（用 `OMNIAGENT_HOME` 或 `omniagent.home` 指定路径）。

### 几个容易踩的坑

- dev server 监听 IPv6 `::1`，访问用 `http://localhost:5173`（`127.0.0.1` 可能连不上）。
- 后端无窗口运行是刻意的，日志在 `backend.log` / `backend.err`；**不要给 uvicorn 加 `--reload`**（Windows 下双进程抢端口会随机挂起）。
- 默认 `runtime.backend: host`（操作本机屏幕）；安卓需自备 `adb_serial`。
- 派发默认开启，单模型也能用；要分层就在 `~/.omniagent/models.json` 声明槽位（如 `worker` / `researcher`），再在 `config.yaml` 的 `runtime.dispatch.agents` 里列出参与派发的槽。

## 配置与数据在哪

| 路径 | 内容 |
|---|---|
| `~/.omniagent/config.yaml` | 模型端点 / API key、通道凭据、运行时开关（真密钥只在这） |
| `~/.omniagent/models.json` | 模型目录与槽位（本地 GGUF、上下文、端口、视觉侧注） |
| `~/.omniagent/cron/jobs.json` | 定时任务（改文件即时生效） |
| `~/.omniagent/memory/` | 原子记忆库 `brain.db` + 全局用户画像 `user_profile.md` |
| `~/.omniagent/projects/<project>/` | 会话历史 `*.jsonl`、场景块、技能、项目级 `AGENTS.md` |
| `~/.omniagent/tasks/<task>/` | 单次任务的轨迹、世界模型、收集结果、任务级技能 |

界面上：项目右键可**重命名**（只改显示名，不动目录与归属）/ 删除（级联删任务）；任务右键可改名、保存到项目、打开文件夹、删除。

## 边界（诚实说明）

- 个人学习项目，Windows 优先、单机部署，**不保证稳定性与体验**；使用后果自负。
- 记忆机制已真机验证，**长期有效性仍在观察**；不是"过目不忘"。
- 技能自动蒸馏默认关闭，早期试点未观察到步数收益，A/B 仍待补测。
- OCR / SoM 是"模型直读图片"的补充手段，不构成通用屏幕理解。
- 本地模型的效果取决于你选的 GGUF 与显存，不做质量承诺。
- 外部通道默认无工具、群聊要 @，这是安全设计而非功能缺失。

## 技术架构（给开发者）

一句话链路：**Agent 循环**（OpenAI Agents SDK `Runner`）→ **工具层**（插件 / capability / MCP，平级可插拔）→ **编排**（LangGraph `Send` 扇出 + 子任务板）→ **知识层**（世界模型 / 技能 / 记忆 / 三层 `AGENTS.md` 注入）→ **FastAPI 后端** → **React 前端 / Electron 壳 / 常驻网关（IM + cron）**。

| 层 | 技术 |
|---|---|
| Agent 循环 | OpenAI Agents SDK（`Runner` + 流式事件 + StopAtTools 收尾门控） |
| 状态机编排 | LangGraph（`Send` 并发扇出、`update_state` 软注入） |
| 后端 | Python + FastAPI / uvicorn |
| 前端 | TypeScript + React 18 + Vite + MUI + Tailwind（自研 store） |
| 桌面壳 | Electron（拉起后端 + 网关，托盘常驻） |
| 本地推理 | llama.cpp `llama-server`（多槽位、`--mmproj` 视觉） |
| 语音 / 视觉 | SenseVoice + Kokoro（ONNX）；RapidOCR；SoM 标注 |

**核心红线**：内核零场景硬编码——加能力 = 写工具 / 插件，绝不把某个 app / 游戏 / 业务的判断塞进 `omni_core/`。`scripts/review_lint.py`（R1 屏幕字段泄露 / R2 具体工具名分支 / R3 缺抽象方法）由 CI 守护，任何场景分支都必须抽成工具。

**目录地图**

| 路径 | 作用 |
|---|---|
| `omni_core/brain/` | 模型客户端、prompt、SDK 循环与工具 schema |
| `omni_core/local/` | 世界模型、观测、ToolLoop、技能、轨迹、任务/项目仓储、记忆执行 |
| `omni_core/tools/` | 工具运行时：策略 / 审批、capability 注册、MCP、shell、技能、工作区 |
| `omni_core/orchestration/` | 多 agent 编排（LangGraph 扇出 + 子任务板） |
| `plugins/` | 能力插件：`filesystem` `web` `messaging` `host` `emulator` `voice` `ocr` `vision` |
| `gateway/` | 常驻网关：`channels/`（napcat / feishu / wecom）、派发队列、cron 调度 |
| `backend/` | HTTP 服务与 API 路由；`web/dist` 存在时同源承载前端 |
| `web/` | 前端单页（对话 / 定时任务 / 技能与工具 / 设置） |
| `desktop/` | Electron 壳：自动拉起后端 + 网关、托盘、打包 |
| `model_hub/` `llm_runtime/` | 模型元数据与 llama-server 生命周期 / 推理后端 |
| `scripts/` | 红线 lint、真机验证与基准脚本 |
| `tests/` | 单测与回归 |

**文档导航**

| 文档 | 角色 |
|---|---|
| [`doc/agent-control-product-design.html`](doc/agent-control-product-design.html) | 产品设计（定位 / 原则 / 选型 / 对标） |
| [`doc/agent-control-arch-design.html`](doc/agent-control-arch-design.html) | 架构图集 N1–N10 |
| [`doc/agent-control-master-spec.md`](doc/agent-control-master-spec.md) | 架构决策权威（单一事实来源） |
| [`doc/plans/implemented/message-gateway-2026-10-28.md`](doc/plans/implemented/message-gateway-2026-10-28.md) | 消息通道机制（协议 / 能力门 / 场合自述） |
| [`doc/plans/memory-architecture.md`](doc/plans/memory-architecture.md) | 记忆架构（L0–L3） |
| [`spec/coding_standard.md`](spec/coding_standard.md) | 工程规范 |

> 以上设计文档均为中文。许可证见 [LICENSE](LICENSE)。

<a name="english"></a>

---

# OmniAgent · A Resident AI Companion That Chats, Remembers and Gets Things Done

[中文](#top) | **English**

> An AI companion that runs on your own machine: it chats on the desktop app, remembers you across sessions, speaks on your behalf in QQ / Feishu / WeCom, runs scheduled jobs, and can operate this computer or an Android device when needed.
>
> This is not a UI mock: memory (atomic memories → scene blocks → profile), voice in/out, multi-channel IM, cron, local models, MCP, plugins and human approval are all wired end to end. What it cannot do is listed under [Boundaries](#boundaries-honest-notes).

## At a Glance

| What you want | What it actually does |
|---|---|
| A continuous assistant | Desktop / browser chat with streaming output, visible tool steps, interrupt & queue, per-task "full access" toggle |
| It remembers me | Every turn feeds an atomic memory store → scene blocks → user profile; recalled next time, profile editable by hand |
| Reply for me | Always-on channels for QQ (NapCat), Feishu, WeCom — replies **as you**; groups require an @ mention; voice-in/voice-out; image understanding |
| Work on schedule | Cron jobs with result push-back to a channel, plus a web UI for CRUD and run history |
| Actually act | Keyboard/mouse/screenshot on this machine, or ADB on Android; file read/write/search, web fetch and search work out of the box |
| Use my own models | Cloud APIs and local llama.cpp (GGUF) side by side; per-slot models for sub-agents, including a local vision model |
| Extend it | MCP servers (stdio / streamable HTTP), plugins (one file = one capability), new IM channel = one adapter file |

## Three Typical Setups

### 1. Daily driver (fastest path)

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat      # backend :8000 + frontend :5173
```

Open `http://localhost:5173` → **Settings → Models**, enter your endpoint and API key (or switch to a local GGUF), then chat. Real credentials live only in `~/.omniagent/config.yaml`; the repo ships `config.example.yaml` with no plaintext secrets.

### 2. Speak for you on QQ

1. Install [NapCat](https://github.com/NapNeko/NapCatQQ) with HTTP (`:3000`) and WebSocket (`:3001`) enabled;
2. Fill `channels.napcat` in `~/.omniagent/config.yaml` (`enabled` / `http_url` / `ws_url` / `token`);
3. Start the gateway: `python -m gateway.channel_gateway` (the desktop app spawns it for you);
4. Private chats reply directly; group chats respond only when mentioned (`require_at`). Voice messages are transcribed and answered with voice; images are genuinely understood.

External channels are **chat-only** by default (`allow_tools: false`, the model never sees a tool schema): people can talk to it, but cannot touch your files or machine. Feishu / WeCom are structurally identical — one config block each.

### 3. Scheduled work with delivery

Create a job in the web UI (or edit `~/.omniagent/cron/jobs.json` — hot-reloaded): one-line objective + cron expression + optional completion check + `notify_channel` for delivery. Run history and failure reasons are visible in the UI.

## Feature List

| Feature | Notes | Default |
|---|---|---|
| Desktop / web chat | Streaming, visible reasoning and tool steps, interrupt, message queue, open task folder | On |
| Long-term memory | Sessions → atomic memory (`memory/brain.db`) → scene blocks → profile; recall ranks by relevance + salience + recency | On |
| Personas | Multiple character cards; persona persists per session and filters memory recall | On |
| Projects & tasks | Project = workspace + project knowledge, task = one session; rename / save-to-project / delete / open folder | On |
| IM channels | QQ / Feishu / WeCom, group @ trigger, private allow-list, voice replies, image input, cross-session lookback; on QQ the model speaks only when it chooses to (explicit send, no inner-monologue leakage) | Needs config |
| Cron jobs | Trigger + missed-run catch-up + result delivery + run history | Needs gateway |
| Voice | Local SenseVoice STT + Kokoro TTS; hold-to-talk in the web UI, QQ voice auto-transcribed and answered with voice | Plugin off |
| Vision | Screenshots / QQ images sent to a multimodal model; optional OCR (RapidOCR), optional SoM clicking | On (model dependent) |
| Control this PC | Keyboard, mouse, screenshot, screen observation | `plugins.host` off |
| Control Android | uiautomator2 / ADB: touch, keys, view hierarchy, screenshot, files | `plugins.emulator` off |
| Files & web | Read / write / edit / list / recursive grep; HTML→text fetch; keyless web search | On |
| Skills | Hand-written skills loaded on demand, project & global tiers, promote to global | On |
| Model freedom | Cloud APIs + local llama.cpp; multi-slot catalog; per-sub-agent models | On |
| Extensibility | MCP servers, plugins (`plugins/`), new IM channel (`gateway/channels/`) | On |
| Safety | Approval prompts for sensitive tools (with audit log), tool-less external channels, per-task full-access toggle | On |

## Quick Start

### Windows, one click

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat                        # backend :8000 + frontend :5173
python -m gateway.channel_gateway      # optional: IM channels + cron
.\start_all.bat stop                   # stop everything
```

Configure the model endpoint and API key in Settings before the first conversation ("config file not found" in backend logs is normal on a fresh setup).

### Desktop app (recommended for daily use)

```bash
cd desktop
npm install          # first run; China CDN mirror preconfigured
npm run dev          # vite + backend + gateway + Electron
npm run dist         # NSIS installer + portable exe (desktop/dist/)
```

Or run `pack_desktop.bat` from the repo root (builds `web/dist` first). The shell auto-spawns backend and gateway and minimizes to the tray. Packages ship the **shell only**; backend code and `.venv` still point at this repo (`OMNIAGENT_HOME` or an `omniagent.home` file overrides the path).

### Gotchas

- The dev server listens on IPv6 `::1` — use `http://localhost:5173` (`127.0.0.1` may fail).
- The backend is intentionally windowless; logs go to `backend.log` / `backend.err`. **Do not add `--reload` to uvicorn** (the reloader double-binds the port on Windows and hangs).
- Default `runtime.backend: host` (this machine's screen); Android needs your own `adb_serial`.
- Dispatch is on by default and works with a single model. To tier it: declare slots in `~/.omniagent/models.json` (`worker` / `researcher`), then list them in `runtime.dispatch.agents`.

## Where Config and Data Live

| Path | Contents |
|---|---|
| `~/.omniagent/config.yaml` | Model endpoints / API keys, channel credentials, runtime switches |
| `~/.omniagent/models.json` | Model catalog and slots (local GGUF, context, port, vision sidecar) |
| `~/.omniagent/cron/jobs.json` | Cron jobs (hot-reloaded) |
| `~/.omniagent/memory/` | Atomic memory store `brain.db` + global profile `user_profile.md` |
| `~/.omniagent/projects/<project>/` | Session `*.jsonl`, scene blocks, skills, project `AGENTS.md` |
| `~/.omniagent/tasks/<task>/` | Trajectory, world model, collected results, task-level skills |

In the UI: right-click a project to rename (display name only) or delete (cascades to its tasks); right-click a task to rename, save to project, open folder, or delete.

## Boundaries (honest notes)

- Personal learning project, Windows-first, single machine, **no stability or UX guarantee**; use at your own risk.
- Memory mechanism is live-verified, **long-term effectiveness still under observation**.
- Automatic skill distillation is off by default: an early pilot showed no step savings, A/B still pending.
- OCR / SoM complement model-native image reading; they are not general screen understanding.
- Local model quality depends on the GGUF and your VRAM; no promises.
- Tool-less external channels and @-only group replies are deliberate safety choices, not missing features.

## Architecture (for developers)

One line: **agent loop** (OpenAI Agents SDK `Runner`) → **tool layer** (plugins / capabilities / MCP, peers) → **orchestration** (LangGraph `Send` fan-out + subtask board) → **knowledge layer** (world model / skills / memory / three-tier `AGENTS.md` injection) → **FastAPI backend** → **React frontend / Electron shell / always-on gateway (IM + cron)**.

| Layer | Tech |
|---|---|
| Agent loop | OpenAI Agents SDK (`Runner`, streaming, StopAtTools gating) |
| Orchestration | LangGraph (`Send` fan-out, `update_state` soft injection) |
| Backend | Python + FastAPI / uvicorn |
| Frontend | TypeScript + React 18 + Vite + MUI + Tailwind (hand-rolled store) |
| Desktop shell | Electron (spawns backend + gateway, tray-resident) |
| Local inference | llama.cpp `llama-server` (multi-slot, `--mmproj` vision) |
| Voice / vision | SenseVoice + Kokoro (ONNX); RapidOCR; SoM markup |

**Core red line**: zero scene-specific logic in the kernel — adding a capability means writing a tool / plugin, never hard-coding an app, game or business branch into `omni_core/`. `scripts/review_lint.py` (R1 screen-field leakage / R2 concrete-tool branching / R3 missing abstract methods) runs in CI.

**Repository map**

| Path | Purpose |
|---|---|
| `omni_core/brain/` | Model clients, prompts, SDK loop and tool schemas |
| `omni_core/local/` | World model, observation, ToolLoop, skills, trajectory, task/project stores, memory execution |
| `omni_core/tools/` | Tool runtime: policy / approvals, capability registry, MCP, shell, skills, workspace |
| `omni_core/orchestration/` | Multi-agent orchestration (LangGraph fan-out + subtask board) |
| `plugins/` | Capability plugins: `filesystem` `web` `messaging` `host` `emulator` `voice` `ocr` `vision` |
| `gateway/` | Always-on gateway: `channels/` (napcat / feishu / wecom), dispatch queue, cron scheduler |
| `backend/` | HTTP service and API routes; serves `web/dist` same-origin when present |
| `web/` | Single-page frontend (chat / cron / skills & tools / settings) |
| `desktop/` | Electron shell: spawns backend + gateway, tray, packaging |
| `model_hub/` `llm_runtime/` | Model metadata and llama-server lifecycle / inference backends |
| `scripts/` | Red-line lint, real-device verification and benchmarks |
| `tests/` | Unit and regression tests |

**Documentation**

| Document | Role |
|---|---|
| [`doc/agent-control-product-design.html`](doc/agent-control-product-design.html) | Product design (positioning / principles / tech choices) |
| [`doc/agent-control-arch-design.html`](doc/agent-control-arch-design.html) | Architecture diagrams N1–N10 |
| [`doc/agent-control-master-spec.md`](doc/agent-control-master-spec.md) | Authoritative architecture decisions |
| [`doc/plans/implemented/message-gateway-2026-10-28.md`](doc/plans/implemented/message-gateway-2026-10-28.md) | Message-channel design (protocol / tool gate / narration) |
| [`doc/plans/memory-architecture.md`](doc/plans/memory-architecture.md) | Memory architecture (L0–L3) |
| [`spec/coding_standard.md`](spec/coding_standard.md) | Engineering standard |

> The design documents above are written in Chinese. License: [LICENSE](LICENSE).
