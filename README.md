<a name="top"></a>

# OmniAgent · 能聊、能记、能干活的常驻 AI 伙伴

**中文** | [English](#english)

> 跑在自己电脑上的 AI 伙伴：桌面端陪你聊、跨会话记得你和身边每个人、能替你在 QQ / 飞书 / 企微 里说话、按点把活干了、用你自己的音色回语音（被点歌还能清唱一句），需要时还能直接操作这台电脑或一台安卓机。
>
> 它不是界面演示：记忆（原子记忆 → 场景块 / 日记 → 画像 → 关系卡）、常驻语音引擎服务、多 IM 通道、定时任务、本地模型、MCP、插件、人工审批都是打通的完整链路。做不到的部分写在[边界](#边界诚实说明)里，不吹。

## 30 秒看懂

| 你想要 | 它实际做的 |
|---|---|
| 有个能连续聊的助手 | 桌面端 / 浏览器单页对话，流式输出、思考与工具过程可见、可打断、可排队；每条会话带 `chat / task` 取向胶囊（取向与会话状态解绑、按会话记忆）；可给单个任务开「完全访问」 |
| 它记得我 | 每轮自动沉淀：原子记忆库 → 场景块（含**她自己的日记 / 月记**）→ 用户画像；召回由模型自取（`memory_recall` / `conversation_search` / `read_scene`），画像与角色卡都可手改 |
| 它也记得身边每个人 | 关系卡 `memory/relations/<uid>.md`：按人存一份三行印象（最近交手 / 他对我的态度 / 我对他的态度），QQ 被 @ 或私聊时随场合自述注入 |
| 帮我回消息 | QQ（NapCat）、飞书、企微常驻通道，以**主人的身份**代答；群聊三层闸门 + 自然存在感（被点名 / 主动接话 / 旁听），支持语音进 / 语音出、图片理解 |
| 到点自己干活 | cron 定时任务，跑完把结果推回指定通道，Web 里可增删改查 + 看运行记录 |
| 用我自己的声音说话 | 语音引擎是**一个独立常驻服务**（OpenAI 音频兼容端点）：SenseVoice 转写 + GPT-SoVITS 微调合成；Web 按住说话 / 免提 VAD / 播报中说话即打断 |
| 点首歌 | 群成员点歌名 → 曲库里有就直接清唱回一条；库外可开在线曲库：搜歌 → 下载 → 分离 → 副歌 → 定 key → 入库 → 唱（总闸默认关） |
| 真的动手 | 键鼠 / 截图 / 屏幕观测（本机），或 ADB 操作安卓机；文件读写检索、网页抓取、联网搜索开箱可用；`shell_exec` 按探测得到的 shell 自我闸门 |
| 用我自己的模型 | 云端 API 与本地 llama.cpp（GGUF）混用；端点唯一真源是 `~/.omniagent/models.json`，可按槽位给子 agent 配不同模型，含本地视觉模型 |
| 我要接自己的东西 | MCP server（`~/.omniagent/mcp.json`）、插件（写一个就能加能力）、新增 IM 通道 = 一个适配器文件 + 一份通道 yaml |

## 四个典型用法

### 1. 当日常助手（最快路径）

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat      # 三个窗口：后端 :8000 + 语音引擎 :18742 + 前端 :5173
```

打开 `http://localhost:5173` → **设置 → 模型** 填 API provider 与 key（或选本地 GGUF）→ 直接聊。
设置页写的是 `~/.omniagent/models.json`；仓库里不含任何明文密钥，每份配置的带注释模板都在 [`examples/`](examples/README.md)。

不想开三个窗口就用桌面端（见[快速开始](#快速开始)）。

### 2. 让它替你在 QQ 上说话

**一通道一文件**：`~/.omniagent/channels/<name>.yaml`（主配置里的 `channels:` 段已不再读取），聊天路由表是同目录的 `routes.yaml`。

1. 装 [NapCat](https://github.com/NapNeko/NapCatQQ)，开启 HTTP（默认 `:3000`）与 WebSocket 服务（默认 `:3001`）；
2. 抄 `examples/channels/napcat.yaml` 建 `~/.omniagent/channels/napcat.yaml`：`enabled` / `http_url` / `ws_url` / `token`，群聊闸门 `reply_groups` / `allow_groups` / `require_at`，私聊白名单 `allow_users`；
3. 起网关：`python -m gateway.channel_gateway`（桌面端会自动拉起，不用手动跑）；
4. 私聊直接回；群聊**默认必须 @ 它**才响应。语音进来会转写成 `[语音] <内容>`，**要不要用语音回由模型决定**（`msg_send_voice` vs `msg_send_text`，通道不自动改发送形式），图片真的看懂。

想让她「像个人一样在群里待着」，就在同一份 yaml 里开 `group_presence`：`enabled_groups`（缺省全关）、`talk_value`（话痨度）、`thread_hot_min`（自己发过言后的跟进期）、`quiet_hours`（安静时段）、`aliases`（没打 @ 喊名字也算）。**回不回是通道侧算出来的**：一轮里她没调发送工具就什么都不会发，内心独白不外泄，这类「静默轮」也不会再喂回下一轮上下文。

外部通道默认**纯对话**（`allow_tools: false`，模型侧连工具清单都没有）：别人给你发消息可以聊天，但碰不到你的文件和电脑。飞书 / 企微同构，各自一份 yaml 即可。

### 3. 让它定时干活并回消息

Web「定时任务」页新建（或改 `~/.omniagent/cron/jobs.json`，热加载不用重启，容忍 `//` 注释；逐字段说明见 `examples/cron/jobs.json`）：写一句 objective + cron 表达式 + 完成条件，选 `notify_channel` 指定结果推到哪个通道。运行记录与失败原因都能在页面看到。

### 4. 让她开口说话、唱一句

语音拆成**服务端 / 调用端两份配置**，换引擎只改端点，代码与前端都不用动：

```bash
# 引擎侧：模型路径、音色、空闲回收、端口（留空 = 自动探测，一般不用改）
copy examples\voice_service.yaml %USERPROFILE%\.omniagent\voice_service.yaml
# 调用端：只描述「把请求发到哪」
copy examples\plugins\voice.yaml %USERPROFILE%\.omniagent\plugins\voice.yaml
```

- 服务默认只听回环、按需懒加载、空闲释放模型（进程不退、监听常在）；要放到局域网就改 `service_host` **并务必设 `service_token`**。
- 一个音色 = `voices/<名字>.wav`（参考音频）+ 同名 `.txt`（韵律参考文本）；音色本体在微调权重里，wav 只提供音色与韵律参考。
- 唱歌的在线曲库总闸 `sing_online` **默认关**：开着时库外点歌先应下、后台跑完整条管线再补投，失败也会回话；关着就维持「这首我还不会唱」。站点源插件放 `~/.omniagent/song_sources/`，不入公开仓。
- 设置页入口：**设置 → 模型** 里的语音引擎卡片（端点 + 服务配置 + 探活 + 试听）。插件本身默认 `enabled: false`。

## 功能清单

| 功能 | 说明 | 默认状态 |
|---|---|---|
| 桌面 / Web 对话 | 流式回答、思考与工具步骤可见、随时打断、消息排队、产物可开文件夹；会话级 `chat / task` 取向胶囊 | 开箱 |
| 界面形态 | 设置与技能工具是**同一层整屏遮罩 + 左导航分组表单**（外观 / 通用 / 伙伴 / 安全 / 模型 / 编排 / 关于），右侧 dock 可拖宽并按机器记忆；图标全走 Lucide | 开箱 |
| 长期记忆 | L1 原子（`memory/brain.db`，一条原子一行索引）→ L2 场景块 + 日记 / 月记 → L3 用户画像；L0 逐字会话索引；相关性绝对化打分 + 同主题去冗余，**由模型自取而非自动注入** | 开箱 |
| 关系卡 | 按 uid 全局存一份、卡内记「在哪儿遇到」；QQ 点名 / 私聊时注入 ≤200 字三行印象，无卡零注入 | 需开 QQ 通道 |
| 角色卡 | 单角色 `character.md`，设置里直接改、自下一次运行起生效；人格只写在角色卡里，通道文案只交代处境与发送机制，不重复扮演 | 开箱 |
| 项目与任务 | 项目 = 工作目录 + 项目级知识，任务 = 一次会话；右键改名、保存到项目、删除、打开文件夹 | 开箱 |
| IM 通道 | QQ / 飞书 / 企微，群 @ 触发、私聊白名单、语音对语音、图片多模态、跨会话回看消息、群自然存在感（T0 点名 / T1 主动 / T2 旁听，独白不外发、静默轮不进下轮上下文） | 需配通道 |
| 定时任务 | cron 触发 + 错过补偿 + 结果推送 + 运行记录 | 需跑网关 |
| 语音 | 独立常驻引擎服务（OpenAI 音频兼容）：SenseVoice 转写 + GPT-SoVITS v2 微调合成；Web 按住说话 / 免提 VAD / 播报中说话即打断；QQ 语音自动转写并可用语音回复 | 插件默认关 |
| 唱歌 | 清唱点歌：RVC SVC 声库 + 本地曲库；在线曲库（搜 → 下 → 分离 → 副歌 → 定 key → 入库 → 唱）异步补投 | 在线闸门默认关 |
| 看图 | 截图 / QQ 图片作为当轮图片输入直传模型；可选 OCR（RapidOCR）、可选 SoM 标注点击 | 开箱（模型需支持视觉） |
| 操作本机 | 键鼠、截图、屏幕观测 | `plugins/host` 默认关 |
| 操作安卓 | uiautomator2 / ADB：触控、按键、层级树、截图、文件 | `plugins/emulator` 默认关 |
| 文件与联网 | 文件读 / 写 / 改 / 列举 / 递归检索；HTML 抓取转文本；无密钥联网搜索；`shell_exec` 探测不到可用 shell 就不注册这个工具 | 开箱 |
| 技能库 | 手写技能按需加载，项目级 / 全局两级，可一键提升为全局 | 开箱 |
| 模型自由 | 云端 API + 本地 llama.cpp；`models.json` 多槽位目录，子 agent 可用不同（含本地快模型） | 开箱 |
| 纪律文件 | 三层 `AGENTS.md`（全局 / 项目 / 任务）注入 system prompt；全局那份由 `GET/PUT /api/runtime/instructions` 读写，改动自下一次运行生效 | 开箱 |
| 扩展 | MCP server、插件（`plugins/`）、新 IM 通道（`gateway/channels/`） | 开箱 |
| 安全 | 敏感工具可要求人工审批（Web 弹窗，含审计日志）；外部通道默认无工具；每任务「完全访问」开关 | 开箱 |

## 快速开始

### Windows 一键

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat                        # 后端 :8000 + 语音引擎 :18742 + 前端 :5173
python -m gateway.channel_gateway      # 可选：IM 通道 + 定时任务（另开一个窗口）
.\start_all.bat stop                   # 停掉 :8000 / :18742 / :5173
```

首次启动后请到 Web「设置 → 模型」配置 provider 与默认模型，否则对话无法调用模型（`models.json` 没有 `main` 槽时，链路口直接报「大脑未配置」而不是半可用）。语音、操作本机、安卓都是**默认关闭**的插件，要用再开。

### 桌面应用（推荐日常使用）

```bash
cd desktop
npm install          # 首次；.npmrc 已配国内镜像
npm run dev          # vite + 后端 + 网关 + 语音服务 + Electron 一键起
npm run dist         # 打包 NSIS 安装包 + portable exe（desktop/dist/）
```

也可以回到仓库根目录跑 `pack_desktop.bat`（顺带先构建 `web/dist`）。桌面端会自动拉起后端、网关与语音引擎服务（崩溃会重启、退出把整棵进程树带走），关窗口缩到托盘（托盘可重启后端 / 看日志 / 退出）。打包产物是**壳**，后端与 `.venv` 仍指向本仓库（用 `OMNIAGENT_HOME` 或 `omniagent.home` 指定路径）。

### 几个容易踩的坑

- dev server 监听 IPv6 `::1`，访问用 `http://localhost:5173`（`127.0.0.1` 可能连不上）。
- `start_all.bat` 起的是**三个可见窗口**（Backend / Voice / Web），输出直接摆在眼前；桌面端则后台拉起、日志落文件。**不要给 uvicorn 加 `--reload`**（Windows 下双进程抢端口会随机挂起）。
- 端点只认 `~/.omniagent/models.json`：主配置里残留的旧端点键已不再读取，改 `config.yaml` 不会换模型。
- 语音服务与主进程解耦：服务没起来时语音调用明确失败，文字链路不受影响；GPT-SoVITS 运行时在 `external/GPT-SoVITS/.venv`（独立 venv），不在主 venv。
- 操作本机 / 安卓要显式建插件配置（`~/.omniagent/plugins/host.yaml` 等）；安卓需自备 `adb_serial`。
- 派发默认开启，单模型也能用；要分层就在 `~/.omniagent/models.json` 声明槽位（如 `worker` / `researcher`），再在 `config.yaml` 的 `runtime.dispatch.agents` 里列出参与派发的槽。

## 配置与数据在哪

唯一持久化根是 `~/.omniagent/`，按「谁来读」分文件——连接信息这类部署事实不再往主配置里堆：

| 路径 | 内容 | 谁来读 |
|---|---|---|
| `~/.omniagent/models.json` | **模型端点唯一真源**：provider 目录 + `defaults` 槽位（本地 GGUF、上下文、端口、视觉侧注） | 大脑 / 网关派发 / 记忆链路 |
| `~/.omniagent/config.yaml` | `brain`（推理模式、长任务参数）、`runtime`（派发、历史窗口等）、`security`（审批策略） | core |
| `~/.omniagent/channels/<name>.yaml` | 通道连接与闸门（NapCat 地址 / token、群 @ 规则、`group_presence`）；`routes.yaml` 是聊天路由表 | 网关 |
| `~/.omniagent/plugins/<name>.yaml` | 插件调用端配置（`enabled`、语音 `base_url` 等） | 插件 |
| `~/.omniagent/voice_service.yaml` | 语音引擎服务：端口 / token / 空闲回收 / 模型路径 / 音色 / 唱歌闸门 | 语音引擎服务 |
| `~/.omniagent/mcp.json` | MCP server 清单（stdio / streamable HTTP） | 工具运行时 |
| `~/.omniagent/cron/jobs.json` | 定时任务（改文件即时生效） | 网关调度器 |
| `~/.omniagent/character.md` | 助手角色卡（人格，随 system prompt 注入，设置页可改） | 注入层 |
| `~/.omniagent/AGENTS.md` | 全局纪律文件（跨任务共享；不存在则零注入） | 注入层 |
| `~/.omniagent/memory/` | 原子记忆库 `brain.db`（L1 atoms + L0 逐字索引）+ 全局画像 `user_profile.md` + 关系卡 `relations/<uid>.md` | 记忆层 |
| `~/.omniagent/models/` | 本地模型资产：`sense-voice/`、`gptsovits/`（`weights/` + `voices/`）、`sing/`（RVC 声库） | 语音服务 |
| `~/.omniagent/song_sources/` | 在线曲库站点源（**不入公开仓**） | 唱歌管线 |
| `~/.omniagent/projects/<project>/` | 会话历史 `*.jsonl`、场景块（含 `diary-*` / 月记）、技能、项目级 `AGENTS.md` | 知识层 |
| `~/.omniagent/tasks/<task>/` | 单次任务的轨迹、世界模型、收集结果、任务级技能 | 运行态 |

上表前七行的每份配置，在仓库根 [`examples/`](examples/README.md) 里都有一份**逐键带注释的同名模板**（`examples/models.json`、`examples/config.yaml`、`examples/channels/*.yaml`、`examples/plugins/voice.yaml`、`examples/voice_service.yaml`、`examples/mcp.json`、`examples/cron/jobs.json`）：按同名路径抄进 `~/.omniagent/` 就是一份可读的初始配置，里面写清了每个键谁读、取值范围、改了要不要重启。示例不含明文密钥与本机绝对路径；`*.json` 三份容忍 `//` 注释，但 Web 面板保存会整体重写把它们抹掉——所以注释的家在 `examples/`，不在运行文件里。

界面上：项目右键可**重命名**（只改显示名，不动目录与归属）/ 删除（级联删任务）；任务右键可改名、保存到项目、打开文件夹、删除。

## 边界（诚实说明）

- 个人学习项目，Windows 优先、单机部署，**不保证稳定性与体验**；使用后果自负。
- 记忆机制已真机验证，**长期有效性仍在观察**；召回验收阈值是在很小的语料上校准的，目前仍有指标未达标——这是刻意不调线的，语料长起来才重测。不是"过目不忘"。
- **记忆层不是原创**：L0–L3 分层、多文件场景块与巩固周期移植自开源项目 **TencentDB Agent Memory**（上游 TypeScript 实现，本仓库以 Python + SQLite FTS5 重写）。哪里改了、哪里丢了、哪些是自创，摘要见「技术架构」一节的移植偏差表，逐条登记见 [`doc/plans/memory-recall-2026-11-02.md`](doc/plans/memory-recall-2026-11-02.md) §3。
- 群聊轮不产 L1 原子（防记忆投毒），所以「按人记住」只能靠关系卡这一条登记在案的特例：只在 QQ 单一说话人的路径注入、≤200 字、不进检索、**无上限无 TTL**。覆盖率与「旧印象被当成近期事实」都仍在观察，暂不动机制。
- 唱歌走的是「分离离线缓存 + 歌声转换在线」：库内点歌十几秒级，库外整条管线是分钟级（下载占大头），音质取决于声库素材与显存；在线曲库闸门默认关，站点源不入公开仓。
- 语音服务默认只听回环；放到局域网而不设 token，等于让任何人白嫖你的显卡。
- 技能自动蒸馏默认关闭，早期试点未观察到步数收益，A/B 仍待补测。
- OCR / SoM 是"模型直读图片"的补充手段，不构成通用屏幕理解。
- 本地模型的效果取决于你选的 GGUF 与显存，不做质量承诺。
- 外部通道默认无工具、群聊要 @，这是安全设计而非功能缺失。
- 界面受一条硬约束：**必须能完全用浏览器操作**，不依赖 Electron 特有能力；设备级显示偏好（字号、图标浓度、面板宽度）只存本机 `localStorage`。亮色主题刻意暂缓——配色按暗色校准，仍有硬编码色值未收进 token。

## 技术架构（给开发者）

一句话链路：**Agent 循环**（OpenAI Agents SDK `Runner`）→ **工具层**（插件 / capability / MCP，平级可插拔）→ **编排**（LangGraph `Send` 扇出 + 子任务板）→ **知识层**（世界模型 / 技能 / 记忆 / 三层 `AGENTS.md` 注入）→ **FastAPI 后端** → **React 前端 / Electron 壳 / 常驻网关（IM + cron）/ 常驻语音引擎服务**。

| 层 | 技术 |
|---|---|
| Agent 循环 | OpenAI Agents SDK（`Runner` + 流式事件 + StopAtTools 收尾门控） |
| 状态机编排 | LangGraph（`Send` 并发扇出、`update_state` 软注入） |
| 后端 | Python + FastAPI / uvicorn |
| 前端 | TypeScript + React 18 + Vite + MUI + Tailwind（自研 store）、Lucide 图标 |
| 桌面壳 | Electron（拉起后端 + 网关 + 语音服务，托盘常驻） |
| 本地推理 | llama.cpp `llama-server`（多槽位、`--mmproj` 视觉） |
| 语音 | 常驻服务 `plugins/voice/engine_service.py`（OpenAI 音频兼容、懒加载、空闲释放）；STT = SenseVoice（sherpa-onnx int8）；TTS = GPT-SoVITS v2 微调（`external/GPT-SoVITS` 独立 venv 的子进程） |
| 唱歌 | RVC SVC 常驻 worker + PyMSS BS-Roformer 分离（一次性子进程）+ ffprobe 时长筛查 |
| 视觉 | RapidOCR（ONNX）；SoM 标注 |
| 记忆存储 | SQLite（`brain.db`：atoms + `atoms_fts` + L0 逐字索引）+ JSONL 场景块 + Markdown 画像 / 关系卡 |

**核心红线**：内核零场景硬编码——加能力 = 写工具 / 插件，绝不把某个 app / 游戏 / 业务的判断塞进 `omni_core/`。内核对语音零 import、语音插件不注册任何 agent 工具；「回不回话」是通道侧的纯计算，不交给模型猜。`scripts/review_lint.py`（R1 屏幕字段泄露 / R2 具体工具名分支 / R3 缺抽象方法）由 CI 守护（`.github/workflows/redline-lint.yml`），任何场景分支都必须抽成工具。

**记忆层出处与移植偏差**

记忆轴（L0–L3 分层 / 多文件场景块 / 巩固周期）移植自开源项目 **TencentDB Agent Memory**：上游是 TypeScript 实现，本仓库用 Python + SQLite FTS5 重写，参照件 clone 在 `external/TencentDB-Agent-Memory`（不入 git）。**2026-11-06 起上游定位降级为「机制参考与出处标注」**——上游的目标函数是多租户隔离，本仓库是单用户多语境，判据只能是「对单用户个人助理是否有用」，不以「上游这么做/没有」作为取舍理由。逐条取舍只登记在 [`doc/plans/memory-recall-2026-11-02.md`](doc/plans/memory-recall-2026-11-02.md) §3 与 `doc/plans/implemented/memory-architecture.md` 末尾的部件溯源表，本节只做摘要，不在两处各写一半。

| 机制 | 上游 | 本仓库 | 性质 |
|---|---|---|---|
| L1 存储 | 双库 | `brain.db` 单库 + FTS5 `simple` 分词（2026-10-28 收敛） | 本地重写的取舍 |
| L1 相关性打分 | 绝对分 `rel/(1+rel)` | 曾自创候选集线性归一，已回对齐。实测绝对分落点集中在 0.5–0.6，`threshold 0.3` 几乎挡不住任何弱命中（上游同样），真正控量的是 k + 去冗余 | 实现偏差 → 已对齐 |
| L1 进 prompt | 自动注入已下线，改两个检索工具按需（每轮合计 ≤3 次，理由是破坏 prompt cache） | 曾每轮自动注入 8 条，2026-11-02 决策全局下线、改由模型自取 | 方向性偏离 → 已对齐 |
| 记忆块位置 | stable / dynamic 拆分 + 渐进披露 | 曾自创「每步重发记忆块」的 Model 包装；静态内容每步重发 = 反复 priming，与工具调用写成文本的生成退化相关，2026-10-07 移除并写成 `review_lint` 红线 | 自创 → 已删并上锁 |
| 写入去重候选 | FTS / 向量近邻 top5 + 一次批量 LLM 判 store/update/merge/skip | 曾用 `content[:12]` 精确短语探针，33 条 active 实测候选命中 0/33（即去重从未触发），现改为 FTS 全文近邻 top5 | 实质降级 → 已修复 |
| L2 场景块进料 | 只吃 L1 原子增量（`queryMemoryRecords({updatedAfter: cursor})` → `extract(memories)`），且块尺寸在代码里**零硬检查**，靠 1500 字符/文件、400 字/核心叙事的 prompt 预算加 read+write 整体重写来收敛 | 曾直接喂「会话要点 + 世界状态」原文，每 900s 一轮：同一天的叙事被日记与场景块各记一遍，块曾被 `scene_edit` 一路加到 8081 字（当时全库唯一超 4000 的一块）。2026-11-05 改回 L1 原子增量 + `last_l2_atom_ts` 游标；2026-11-06 废除 `origin` 分组取料（多租户隔离语义不适配单用户语境，`origin` 只作溯源），改为**一池增量** + 复合游标（`updated + record_id`，同戳不丢料） | 实质偏离 → 已对齐后按单用户取向再修订 |
| 场合信号 / 圈层语境 | 上游无此概念（单会话无「圈子」） | 2026-11-06 自建：`<current-context>` ≤200 字符场合信号 + `circle_note` 圈层语境摘要 + `w_ctx`/`w_bg` 场合与共同背景加权（**加权不过滤**，跨场合记忆照样可召回）；存储一池不拆分 | 自创，保留 |
| 叙事检索 | 上游无叙事资产层 | 日记/月记/关系卡经 `narrative_index`（只存指针不存正文）+ `narrative_search`/`narrative_read` 按需检索读取；>7 天旧日记不再被导航窗口截断；关系卡自动注入已删除、改自取 | 自创，保留 |
| 日记 / 月记按日统一 | 上游无此概念（单会话） | 2026-11-06 起按**日期**全局一篇，落 `~/.omniagent/memory/diary/`：素材跨项目聚合、一次生成；`narrative_index` 的 `doc_id` 为裸文件名、`project_id` 不再作键；旧按项目日记合并归档（正文零丢失）。项目 `scene_blocks/` 只留工作主题块 | 自创，对齐 §3.3 空间键表 |
| 观察进料道 | 上游无此概念 | 非对话来源（主动阅读/检索/工具产出）经唯一入口 `capture_observation()` 落 `l0_messages(source_kind=observation)`，带 `provenance`/`observed_at`/`review_after`（出处与时效，到期由巩固期复核）；升格为事实必经巩固期价值判/四态 | 自创，保留（采集器实现为远期） |
| 叙事与观察的出口 | 上游无此概念（无跨场合叙事 / 外部观察来源） | 2026-11-06 自建：跨场合叙事正文、未升格观察、关系卡评价不得直接转述到对外消息；允许以第一人称见闻表达（「我看到 / 我最近在关注」），禁止当作客观事实断言；约束落在工具层与规则文本，不靠检索侧过滤（§6.5 / §14.4） | 自创，保留 |
| L2 块上限与读取 | 无上限、read 工具返回全文 | 自加 4000 硬上限，但只挂在 `scene_write`、`scene_edit` 漏守，且 read 侧截断到 4000（超限块"只看得见前半"，agent 无从精简）。现补成 write/edit 两路同守（只拦"编辑后更长且超上限"），读侧取消截断 | 自创，已补一致 |
| 衰减 / 遗忘 | 没有 decay / dormant / archive，只有默认关闭的 `retentionDays` 硬删 | 曾自创 salience 衰减 + dormant/archive 状态机（默认关、从未启用，`access_count` 保留作簿记） | 自创 → 已决定删除 |
| 关系卡 / 群聊多人 | 单会话语义，没有 peer 概念 | 为 IM 群场景自加：按 uid 全局一卡；2026-11-06 起改为**可检索资产**（`narrative_search`/`narrative_read` 按需自取），通道自动注入已删除 | 自创，保留 |
| 收尾意图门 | 无此机制 | 畸形工具调用曾被当作普通文本输出、导致任务静默假成功，故对纯文本收尾加一次语义复核（仅 oneshot 成功路径、每 run ≤2 次、判定异常 fail-open） | 自创，保留 |

结论是**不换方案，也不整包接入上游**：它是单会话语义，缺 peer 与群聊多人的概念，接进来还要额外拖进一套 Node 服务栈。

**目录地图**

| 路径 | 作用 |
|---|---|
| `omni_core/brain/` | 模型客户端与路由（端点来自 `models.json`）、prompt、SDK 循环与工具 schema |
| `omni_core/local/` | 世界模型、观测、ToolLoop、技能、轨迹、任务/项目仓储、记忆执行与巩固 |
| `omni_core/tools/` | 工具运行时：策略 / 审批、capability 注册、MCP、shell、技能、工作区 |
| `omni_core/orchestration/` | 多 agent 编排（LangGraph 扇出 + 子任务板） |
| `plugins/` | 能力插件：`filesystem` `web` `messaging` `host` `emulator` `voice` `ocr` `vision`（一插件一份 `plugin.json` + 一份用户级 yaml） |
| `gateway/` | 常驻网关：`channels/`（napcat / feishu / wecom）、派发队列、群存在感、cron 调度 |
| `backend/` | HTTP 服务与 API 路由；`web/dist` 存在时同源承载前端 |
| `web/` | 前端单页（对话 / 定时任务 + 遮罩式设置与技能工具） |
| `desktop/` | Electron 壳：自动拉起后端 + 网关 + 语音服务、托盘、打包 |
| `external/` | 上游引擎 clone（GPT-SoVITS 及其独立 venv），不进 git |
| `model_hub/` `llm_runtime/` | 模型元数据与 llama-server 生命周期 / 推理后端 |
| `scripts/` | 红线 lint、真机验证与基准脚本（`verify_memory_recall.py` 等） |
| `tests/` | 单测与回归 |

**文档导航**

| 文档 | 角色 |
|---|---|
| [`doc/agent-control-product-design.html`](doc/agent-control-product-design.html) | 产品设计（定位 / 原则 / 选型 / 对标） |
| [`doc/agent-control-arch-design.html`](doc/agent-control-arch-design.html) | 架构图集 N1–N10 |
| [`doc/agent-control-master-spec.md`](doc/agent-control-master-spec.md) | 架构决策权威（单一事实来源） |
| [`doc/plans/implemented/memory-architecture.md`](doc/plans/implemented/memory-architecture.md) | 记忆架构现行事实源（L0–L3 / 日记 / 关系卡 / 不变量 / 明确不做） |
| [`doc/plans/memory-recall-2026-11-02.md`](doc/plans/memory-recall-2026-11-02.md) | 召回诊断与验收实测值 |
| [`doc/plans/implemented/message-gateway-2026-10-28.md`](doc/plans/implemented/message-gateway-2026-10-28.md) | 消息通道机制（协议 / 能力门 / 场合自述） |
| [`doc/plans/implemented/group-presence-2026-10-30.md`](doc/plans/implemented/group-presence-2026-10-30.md) | 群自然存在感（T0/T1/T2 分层、闸门顺序、参数与状态划分） |
| [`doc/plans/implemented/voice-2026-10-31.md`](doc/plans/implemented/voice-2026-10-31.md) | 语音交互与引擎（对讲式 / 免提 / 接口契约冻结） |
| [`doc/plans/sing-2026-11-02.md`](doc/plans/sing-2026-11-02.md) | 唱歌选型与实施（TTS / SVC / SVS 分野、管线、闸门与实测） |
| [`doc/plans/remote-client-contract-2026-10-30.md`](doc/plans/remote-client-contract-2026-10-30.md) | 远程客户端契约（含取向与通道解绑的定案段） |
| [`spec/coding_standard.md`](spec/coding_standard.md) | 工程规范 |

> 以上设计文档均为中文。许可证见 [LICENSE](LICENSE)。

<a name="english"></a>

---

# OmniAgent · A Resident AI Companion That Chats, Remembers and Gets Things Done

[中文](#top) | **English**

> An AI companion that runs on your own machine: it chats on the desktop app, remembers you and the people around you across sessions, speaks on your behalf in QQ / Feishu / WeCom, runs scheduled jobs, answers voice messages in **your own timbre** (and can sing a cappella when someone requests a song), and can operate this computer or an Android device when needed.
>
> This is not a UI mock: memory (atomic memories → scene blocks / diary → profile → relation cards), an always-on voice engine service, multi-channel IM, cron, local models, MCP, plugins and human approval are all wired end to end. What it cannot do is listed under [Boundaries](#boundaries-honest-notes).

## At a Glance

| What you want | What it actually does |
|---|---|
| A continuous assistant | Desktop / browser chat with streaming output, visible reasoning and tool steps, interrupt & queue, a per-task "full access" toggle; each session carries a `chat / task` orientation capsule (decoupled from session state, remembered per session) |
| It remembers me | Every turn is distilled automatically: atomic memories → scene blocks (including **her own diary and monthly notes**) → user profile; recall is fetched by the model itself (`memory_recall` / `conversation_search` / `read_scene`), profile and character card stay hand-editable |
| It remembers people too | Relation cards at `memory/relations/<uid>.md` — one card per person, three lines (recent exchange / their attitude to me / my attitude to them), injected with the channel narration when someone @s her or messages her privately |
| Reply for me | Always-on channels for QQ (NapCat), Feishu, WeCom — replies **as you**; three-layer group gates plus natural presence (mentioned / volunteering / ambient); voice-in/voice-out; image understanding |
| Work on schedule | Cron jobs with result push-back to a channel, plus a web UI for CRUD and run history |
| Speak in my voice | The voice engine is **a standalone resident service** (OpenAI-audio-compatible endpoint): SenseVoice transcription + fine-tuned GPT-SoVITS synthesis; hold-to-talk, hands-free VAD, and speaking over playback interrupts it |
| Sing a song | A song request returns a sung voice note; library songs come back in seconds, unknown ones can be fetched → downloaded → separated → chorus-extracted → key-matched → stored → sung (gate off by default) |
| Actually act | Keyboard/mouse/screenshot on this machine, or ADB on Android; file read/write/search, web fetch and search work out of the box; `shell_exec` self-gates on the shells it can actually find |
| Use my own models | Cloud APIs and local llama.cpp (GGUF) side by side; `~/.omniagent/models.json` is the single source of truth for endpoints, with per-slot models for sub-agents, including a local vision model |
| Extend it | MCP servers (`~/.omniagent/mcp.json`), plugins (one file = one capability), new IM channel = one adapter file + one channel yaml |

## Four Typical Setups

### 1. Daily driver (fastest path)

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat      # three windows: backend :8000 + voice engine :18742 + frontend :5173
```

Open `http://localhost:5173` → **Settings → Models**, add your provider and key (or pick a local GGUF), then chat. The settings page writes `~/.omniagent/models.json`; the repo ships no plaintext secrets — every config has a fully commented template in [`examples/`](examples/README.md).

Prefer fewer windows? Use the desktop app (see [Quick Start](#quick-start)).

### 2. Speak for you on QQ

**One file per channel**: `~/.omniagent/channels/<name>.yaml` (the main config's `channels:` section is no longer read); the routing table is `routes.yaml` in the same directory.

1. Install [NapCat](https://github.com/NapNeko/NapCatQQ) with HTTP (`:3000`) and WebSocket (`:3001`) enabled;
2. Copy `examples/channels/napcat.yaml` to `~/.omniagent/channels/napcat.yaml`: `enabled` / `http_url` / `ws_url` / `token`, group gates `reply_groups` / `allow_groups` / `require_at`, private allow-list `allow_users`;
3. Start the gateway: `python -m gateway.channel_gateway` (the desktop app spawns it for you);
4. Private chats reply directly; group chats need an @ by default. Incoming voice is transcribed as `[voice] <content>`, and **the model decides** whether to answer with voice (`msg_send_voice`) or text — the channel never rewrites the medium. Images are genuinely understood.

To let her just hang out in a group, enable `group_presence` in the same yaml: `enabled_groups` (empty = off everywhere), `talk_value`, `thread_hot_min`, `quiet_hours`, `aliases` (her name counts as a mention). **Whether to speak is computed channel-side**: if no send tool is called nothing is sent, the inner monologue never leaks, and those silent runs are dropped from the next round's context.

External channels are **chat-only** by default (`allow_tools: false`, the model never sees a tool schema): people can talk to it, but cannot touch your files or machine. Feishu / WeCom are structurally identical — one yaml each.

### 3. Scheduled work with delivery

Create a job in the web UI (or edit `~/.omniagent/cron/jobs.json` — hot-reloaded, `//` comments tolerated, field-by-field notes in `examples/cron/jobs.json`): one-line objective + cron expression + optional completion check + `notify_channel` for delivery. Run history and failure reasons are visible in the UI.

### 4. Give it a voice (and a song)

Voice is split into **service-side and client-side config**; switching engines means changing the endpoint only — no code or UI edits:

```bash
# engine side: model paths, timbre, idle reaping, port (blank = auto-detect)
copy examples\voice_service.yaml %USERPROFILE%\.omniagent\voice_service.yaml
# client side: describes only where the request goes
copy examples\plugins\voice.yaml %USERPROFILE%\.omniagent\plugins\voice.yaml
```

- The service listens on loopback only, lazy-loads models and releases them on idle (the process keeps listening); to expose it on your LAN set `service_host` **and a `service_token`**.
- One timbre = `voices/<name>.wav` (reference audio) + a same-named `.txt` (prosody reference); the voice itself lives in the fine-tuned weights.
- The online song-library gate `sing_online` is **off by default**: when on, an unknown song is acknowledged first and delivered asynchronously once the pipeline finishes (a failure still says something); when off she simply says "I can't sing that one". Site sources live in `~/.omniagent/song_sources/` and are not published in this repo.
- UI entry: **Settings → Models**, the voice engine card (endpoint + service config + probe + preview). The plugin itself is `enabled: false` out of the box.

## Feature List

| Feature | Notes | Default |
|---|---|---|
| Desktop / web chat | Streaming, visible reasoning and tool steps, interrupt, message queue, open task folder; per-session `chat / task` orientation capsule | On |
| UI shell | Settings and Skills & Tools are full-screen overlays on one shared shell with a left nav (Appearance / General / Companion / Security / Models / Orchestration / About); the right dock is resizable and remembered per machine; Lucide icons throughout | On |
| Long-term memory | L1 atoms (`memory/brain.db`, one index row per atom) → L2 scene blocks + diary / monthly notes → L3 profile; L0 verbatim index; absolute-relevance scoring with same-topic redundancy trimming, **fetched by the model rather than auto-injected** | On |
| Relation cards | One global card per uid recording where they met; ≤200 chars injected on QQ mentions and private chats; no card, no injection | Needs a QQ channel |
| Character card | Single `character.md`, editable in Settings, effective from the next run; personality lives only in the card — channel narration states the situation, it does not act the persona out | On |
| Projects & tasks | Project = workspace + project knowledge, task = one session; rename / save-to-project / delete / open folder | On |
| IM channels | QQ / Feishu / WeCom, group @ trigger, private allow-list, voice replies, image input, cross-session lookback, natural group presence (T0 mention / T1 volunteering / T2 ambient; explicit send only, silent runs never re-enter history) | Needs config |
| Cron jobs | Trigger + missed-run catch-up + result delivery + run history | Needs gateway |
| Voice | Standalone engine service (OpenAI-audio compatible): SenseVoice STT + fine-tuned GPT-SoVITS v2 TTS; hold-to-talk, hands-free VAD, acoustic barge-in; QQ voice transcribed and optionally answered with voice | Plugin off |
| Singing | A cappella song requests: RVC SVC voice bank + local library; online library (search → download → separate → chorus → key → store → sing) delivered asynchronously | Online gate off |
| Vision | Screenshots / QQ images sent to a multimodal model; optional OCR (RapidOCR), optional SoM clicking | On (model dependent) |
| Control this PC | Keyboard, mouse, screenshot, screen observation | `plugins/host` off |
| Control Android | uiautomator2 / ADB: touch, keys, view hierarchy, screenshot, files | `plugins/emulator` off |
| Files & web | Read / write / edit / list / recursive grep; HTML→text fetch; keyless web search; `shell_exec` is not even registered when no usable shell resolves | On |
| Skills | Hand-written skills loaded on demand, project & global tiers, promote to global | On |
| Model freedom | Cloud APIs + local llama.cpp; multi-slot `models.json`; per-sub-agent models | On |
| Instruction files | Three-tier `AGENTS.md` (global / project / task) injected into the system prompt; the global one is read and written via `GET/PUT /api/runtime/instructions`, effective from the next run | On |
| Extensibility | MCP servers, plugins (`plugins/`), new IM channel (`gateway/channels/`) | On |
| Safety | Approval prompts for sensitive tools (with audit log), tool-less external channels, per-task full-access toggle | On |

## Quick Start

### Windows, one click

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd web && npm install && cd ..
.\start_all.bat                        # backend :8000 + voice engine :18742 + frontend :5173
python -m gateway.channel_gateway      # optional: IM channels + cron (own window)
.\start_all.bat stop                   # stops :8000 / :18742 / :5173
```

Configure the provider and default model in Settings before the first conversation — with no `main` slot in `models.json` the call sites raise "brain not configured" instead of half-working. Voice, machine control and Android are **off-by-default** plugins; enable only what you need.

### Desktop app (recommended for daily use)

```bash
cd desktop
npm install          # first run; China CDN mirror preconfigured
npm run dev          # vite + backend + gateway + voice service + Electron
npm run dist         # NSIS installer + portable exe (desktop/dist/)
```

Or run `pack_desktop.bat` from the repo root (builds `web/dist` first). The shell spawns backend, gateway and the voice engine service, restarts them on crash, and kills the whole process tree on quit; closing the window minimizes to the tray (restart backend / view logs / quit). Packages ship the **shell only**; backend code and `.venv` still point at this repo (`OMNIAGENT_HOME` or an `omniagent.home` file overrides the path).

### Gotchas

- The dev server listens on IPv6 `::1` — use `http://localhost:5173` (`127.0.0.1` may fail).
- `start_all.bat` opens **three visible windows** (Backend / Voice / Web) so output sits right there; the desktop shell runs them headless with file logs. **Do not add `--reload` to uvicorn** (the reloader double-binds the port on Windows and hangs).
- Endpoints come only from `~/.omniagent/models.json`; legacy endpoint keys in the main config are no longer read, so editing `config.yaml` will not switch models.
- The voice service is decoupled from the main process: if it is down, voice calls fail loudly while the text chain keeps working. GPT-SoVITS runs in its own venv under `external/GPT-SoVITS`, not the main venv.
- Machine and Android control need explicit plugin files (`~/.omniagent/plugins/host.yaml`, …); Android needs your own `adb_serial`.
- Dispatch is on by default and works with a single model. To tier it: declare slots in `~/.omniagent/models.json` (`worker` / `researcher`), then list them in `runtime.dispatch.agents`.

## Where Config and Data Live

`~/.omniagent/` is the single persistence root, split by **who reads it** — connection details and other deployment facts no longer pile into the main config:

| Path | Contents | Reader |
|---|---|---|
| `~/.omniagent/models.json` | **Single source of truth for model endpoints**: provider catalog + `defaults` slots (local GGUF, context, port, vision sidecar) | brain / gateway dispatch / memory |
| `~/.omniagent/config.yaml` | `brain` (reasoning mode, long-task params), `runtime` (dispatch, history windows), `security` (approval policy) | core |
| `~/.omniagent/channels/<name>.yaml` | Channel connection & gates (NapCat addr / token, @ rules, `group_presence`); `routes.yaml` is the routing table | gateway |
| `~/.omniagent/plugins/<name>.yaml` | Plugin client config (`enabled`, voice `base_url`, …) | plugins |
| `~/.omniagent/voice_service.yaml` | Voice engine service: port / token / idle reaping / model paths / timbre / singing gate | voice service |
| `~/.omniagent/mcp.json` | MCP server list (stdio / streamable HTTP) | tool runtime |
| `~/.omniagent/cron/jobs.json` | Cron jobs (hot-reloaded) | gateway scheduler |
| `~/.omniagent/character.md` | Assistant character card (injected with the system prompt, editable in Settings) | injection layer |
| `~/.omniagent/AGENTS.md` | Global discipline file (shared across tasks; absent = zero injection) | injection layer |
| `~/.omniagent/memory/` | `brain.db` (L1 atoms + L0 verbatim index) + `user_profile.md` (L3) + `relations/<uid>.md` | memory layer |
| `~/.omniagent/models/` | Local model assets: `sense-voice/`, `gptsovits/` (`weights/` + `voices/`), `sing/` (RVC voice bank) | voice service |
| `~/.omniagent/song_sources/` | Online song-library site sources (**not published in this repo**) | singing pipeline |
| `~/.omniagent/projects/<project>/` | Session `*.jsonl`, scene blocks (incl. `diary-*` and monthly notes), skills, project `AGENTS.md` | knowledge layer |
| `~/.omniagent/tasks/<task>/` | Trajectory, world model, collected results, task-level skills | runtime state |

The first seven rows each have a **fully commented template with the same name** under [`examples/`](examples/README.md) in the repo root (`examples/models.json`, `examples/config.yaml`, `examples/channels/*.yaml`, `examples/plugins/voice.yaml`, `examples/voice_service.yaml`, `examples/mcp.json`, `examples/cron/jobs.json`): copy them into `~/.omniagent/` at the same relative paths and you start from a readable config, with every key documented — who reads it, its range, and whether a change needs a restart. Templates carry no secrets and no machine-specific absolute paths. The three `*.json` files tolerate `//` comments, but the web UI rewrites them wholesale, so commentary lives in `examples/`, not in the live files.

In the UI: right-click a project to rename (display name only) or delete (cascades to its tasks); right-click a task to rename, save to project, open folder, or delete.

## Boundaries (honest notes)

- Personal learning project, Windows-first, single machine, **no stability or UX guarantee**; use at your own risk.
- The memory mechanism is live-verified, but **long-term effectiveness is still under observation**; the recall acceptance thresholds were calibrated on a very small corpus and some metrics still miss them — deliberately not re-tuned until the corpus grows. This is not perfect recall.
- Group turns do not produce L1 atoms (memory-poisoning defence), so "remembering a person" relies on the one registered exception: relation cards. They inject only on single-speaker QQ paths, stay ≤200 chars, are not searchable, and have **no size cap and no TTL**. Coverage and "an old impression read as recent fact" remain under observation; the mechanism is intentionally untouched for now.
- **The memory layer is not original work**: the L0–L3 tiers, the multi-file scene blocks and the consolidation cycle are ported from the open-source **TencentDB Agent Memory** (upstream is TypeScript; rewritten here in Python + SQLite FTS5). The summary table at the end of the Architecture section lists what changed, what was dropped and what is self-authored; the itemised record lives in [`doc/plans/memory-recall-2026-11-02.md`](doc/plans/memory-recall-2026-11-02.md) §3.
- Singing follows "offline separation cache + online voice conversion": library songs return in seconds, a brand-new song takes minutes end to end (download dominates), and quality depends on the voice-bank material and your VRAM. The online gate is off by default and site sources are not published.
- The voice service listens on loopback by default; exposing it to your LAN without a token means anyone can burn your GPU.
- Automatic skill distillation is off by default: an early pilot showed no step savings, A/B still pending.
- OCR / SoM complement model-native image reading; they are not general screen understanding.
- Local model quality depends on the GGUF and your VRAM; no promises.
- Tool-less external channels and @-only group replies are deliberate safety choices, not missing features.
- The UI is built under one hard constraint: **everything must stay operable in a plain browser**, with no Electron-only APIs; device-level display preferences (font size, icon tone, panel width) live in `localStorage` only. The light theme is deliberately postponed — colours are calibrated for dark and some values are still hard-coded.

## Architecture (for developers)

One line: **agent loop** (OpenAI Agents SDK `Runner`) → **tool layer** (plugins / capabilities / MCP, peers) → **orchestration** (LangGraph `Send` fan-out + subtask board) → **knowledge layer** (world model / skills / memory / three-tier `AGENTS.md` injection) → **FastAPI backend** → **React frontend / Electron shell / always-on gateway (IM + cron) / always-on voice engine service**.

| Layer | Tech |
|---|---|
| Agent loop | OpenAI Agents SDK (`Runner`, streaming, StopAtTools gating) |
| Orchestration | LangGraph (`Send` fan-out, `update_state` soft injection) |
| Backend | Python + FastAPI / uvicorn |
| Frontend | TypeScript + React 18 + Vite + MUI + Tailwind (hand-rolled store), Lucide icons |
| Desktop shell | Electron (spawns backend + gateway + voice service, tray-resident) |
| Local inference | llama.cpp `llama-server` (multi-slot, `--mmproj` vision) |
| Voice | Resident service `plugins/voice/engine_service.py` (OpenAI-audio compatible, lazy load, idle release); STT = SenseVoice (sherpa-onnx int8); TTS = fine-tuned GPT-SoVITS v2 (subprocess in `external/GPT-SoVITS`'s own venv) |
| Singing | Resident RVC SVC worker + PyMSS BS-Roformer separation (one-shot subprocess) + ffprobe duration screening |
| Vision | RapidOCR (ONNX); SoM markup |
| Memory storage | SQLite (`brain.db`: atoms + `atoms_fts` + L0 verbatim index) + JSONL scene blocks + Markdown profile / relation cards |

**Core red line**: zero scene-specific logic in the kernel — adding a capability means writing a tool / plugin, never hard-coding an app, game or business branch into `omni_core/`. The kernel never imports the voice plugin and the voice plugin registers no agent tools; "whether to speak" is pure channel-side computation rather than a model guess. `scripts/review_lint.py` (R1 screen-field leakage / R2 concrete-tool branching / R3 missing abstract methods) runs in CI (`.github/workflows/redline-lint.yml`).

**Memory layer provenance and porting deltas**

The memory axis (L0–L3 tiers / multi-file scene blocks / consolidation cycle) is ported from the open-source **TencentDB Agent Memory**: upstream is a TypeScript implementation, rewritten here in Python + SQLite FTS5, with the reference clone kept at `external/TencentDB-Agent-Memory` (not in git). Per-item trade-offs are recorded only in [`doc/plans/memory-recall-2026-11-02.md`](doc/plans/memory-recall-2026-11-02.md) §3 and the provenance table at the end of `doc/plans/implemented/memory-architecture.md`; this table is a summary, not a second copy.

| Mechanism | Upstream | This repo | Nature |
|---|---|---|---|
| L1 storage | two databases | single `brain.db` + FTS5 with the `simple` tokenizer (converged 2026-10-28) | choice made while rewriting |
| L1 relevance score | absolute `rel/(1+rel)` | used to be a self-invented candidate-set normalisation, now realigned. Measured absolute scores cluster at 0.5–0.6, so `threshold 0.3` rejects almost no weak hit (same upstream) — volume is actually controlled by k plus de-redundancy | implementation drift → realigned |
| L1 into the prompt | automatic injection retired in favour of two retrieval tools, max 3 calls per turn, because it breaks prompt caching | used to auto-inject 8 atoms per turn; retired globally on 2026-11-02, the model now fetches what it needs | directional deviation → realigned |
| Memory block placement | stable / dynamic split plus progressive disclosure | used to have a self-invented wrapper re-sending the memory block on every step; re-priming static content correlated with degraded tool calling, removed on 2026-10-07 and locked down by `review_lint` | self-invented → deleted and guarded |
| Write-time dedup candidates | FTS / vector neighbours top5, one batched LLM call deciding store/update/merge/skip | used a `content[:12]` exact-phrase probe: 0 of 33 active atoms produced any candidate, i.e. dedup never fired; now full-text FTS neighbours top5 | silent downgrade → fixed |
| L2 scene-block feed | L1 atom deltas only (`queryMemoryRecords({updatedAfter: cursor})` → `extract(memories)`); block size has **no code-level check** upstream, it is held by prompt budgets — 1500 chars per file, 400 per core narrative — plus read+write full rewrites | used to be fed raw "conversation digest + world state" every 900s: one day's story was recorded twice (diary and scene block) and `scene_edit` grew the block to 8081 chars, the only file in the store past 4000. Realigned on 2026-11-05 to L1 atom deltas plus a `last_l2_atom_ts` cursor, taken per `origin` (the project id stamped at write time) to mirror upstream's per-isolation-scope extractor run; `origin=''` hand-notes stay visible to every library, while storage and retrieval remain a single pool | directional deviation → realigned |
| L2 size cap and reads | no cap; the read tool returns the whole file | a self-added 4000 hard cap was enforced on `scene_write` only and skipped on `scene_edit`, while reads truncated to 4000 (an oversized block was only half visible, so it could never be pruned). Now both write paths enforce it — the edit check only rejects an edit that grows past the cap — and reads return the full file | self-invented, made consistent |
| Decay / forgetting | none (no decay / dormant / archive), only `retentionDays` hard delete, off by default | self-invented salience decay and dormant/archive state machine (off by default, never enabled; `access_count` kept as bookkeeping) | self-invented → decided to delete |
| Relation cards / multi-speaker groups | single-conversation semantics, no peer concept | added for IM groups: one card per uid globally, injected only on named or direct mentions, never searchable | self-authored, kept |
| Finish-intent gate | no such mechanism | malformed tool calls were once emitted as plain text and the task silently reported success, so a plain-text finish now gets one semantic re-check (oneshot success path only, max twice per run, fail-open if the judge errors) | self-authored, kept |

The conclusion was **not to swap approaches and not to embed upstream wholesale**: upstream has single-conversation semantics with no notion of peers or multi-speaker groups, and adopting it would also drag a Node service stack into a Python process.

**Repository map**

| Path | Purpose |
|---|---|
| `omni_core/brain/` | Model clients and routing (endpoints from `models.json`), prompts, SDK loop and tool schemas |
| `omni_core/local/` | World model, observation, ToolLoop, skills, trajectory, task/project stores, memory execution and consolidation |
| `omni_core/tools/` | Tool runtime: policy / approvals, capability registry, MCP, shell, skills, workspace |
| `omni_core/orchestration/` | Multi-agent orchestration (LangGraph fan-out + subtask board) |
| `plugins/` | Capability plugins: `filesystem` `web` `messaging` `host` `emulator` `voice` `ocr` `vision` (one `plugin.json` + one user yaml each) |
| `gateway/` | Always-on gateway: `channels/` (napcat / feishu / wecom), dispatch queue, group presence, cron scheduler |
| `backend/` | HTTP service and API routes; serves `web/dist` same-origin when present |
| `web/` | Single-page frontend (chat / cron + overlay-style settings & skills/tools) |
| `desktop/` | Electron shell: spawns backend + gateway + voice service, tray, packaging |
| `external/` | Upstream engine clones (GPT-SoVITS and its own venv), not tracked by git |
| `model_hub/` `llm_runtime/` | Model metadata and llama-server lifecycle / inference backends |
| `scripts/` | Red-line lint, real-device verification and benchmarks (`verify_memory_recall.py`, …) |
| `tests/` | Unit and regression tests |

**Documentation**

| Document | Role |
|---|---|
| [`doc/agent-control-product-design.html`](doc/agent-control-product-design.html) | Product design (positioning / principles / tech choices) |
| [`doc/agent-control-arch-design.html`](doc/agent-control-arch-design.html) | Architecture diagrams N1–N10 |
| [`doc/agent-control-master-spec.md`](doc/agent-control-master-spec.md) | Authoritative architecture decisions |
| [`doc/plans/implemented/memory-architecture.md`](doc/plans/implemented/memory-architecture.md) | Current memory architecture (L0–L3 / diary / relation cards / invariants / explicit non-goals) |
| [`doc/plans/memory-recall-2026-11-02.md`](doc/plans/memory-recall-2026-11-02.md) | Recall diagnosis and measured acceptance values |
| [`doc/plans/implemented/message-gateway-2026-10-28.md`](doc/plans/implemented/message-gateway-2026-10-28.md) | Message-channel design (protocol / tool gate / narration) |
| [`doc/plans/implemented/group-presence-2026-10-30.md`](doc/plans/implemented/group-presence-2026-10-30.md) | Natural group presence (T0/T1/T2 layering, gate ordering, parameter vs state split) |
| [`doc/plans/implemented/voice-2026-10-31.md`](doc/plans/implemented/voice-2026-10-31.md) | Voice interaction & engines (walkie-talkie / hands-free / frozen API contract) |
| [`doc/plans/sing-2026-11-02.md`](doc/plans/sing-2026-11-02.md) | Singing research and implementation (TTS / SVC / SVS split, pipeline, gates, measurements) |
| [`doc/plans/remote-client-contract-2026-10-30.md`](doc/plans/remote-client-contract-2026-10-30.md) | Remote client contract (incl. orientation / channel decoupling) |
| [`spec/coding_standard.md`](spec/coding_standard.md) | Engineering standard |

> The design documents above are written in Chinese. License: [LICENSE](LICENSE).
