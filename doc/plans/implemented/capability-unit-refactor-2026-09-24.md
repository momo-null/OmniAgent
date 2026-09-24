# 能力单元（unit）化简 + observe 工具化 + 执行/产物归一

> **日期**：2026-09-24
> **状态**：✅ **已结项（2026-09-25）** —— 代码侧无遗留（见 §11 结项结论）。
> **文档说明**：本计划是 `tool-plugin-master-plan.md` 之后的收口设计。插件化机制（P1-P3）已完成，但遗留了四个问题，本文统一收口：① `group` 概念冗余；② observe 被 loop 强制作成单独步骤；③ `run_python` 冗余且是个绕过门禁的常开后门；④ agent 产物默认落进项目目录。修订直接改本文件并更新状态行。
> **基线**：工作区当前未提交改动基线见 git；执行机本机 Windows，Python 3.12（仓库 `.venv`），PowerShell。
> **北极星约束**：内核零场景假设；能力 = 插件（模型直接调）；loop 只编排不替模型观察；全程不写死任何 app / 场景。**前端零插件特判**：插件在前端只能有一个开关 + 自报元数据，任何自定义 UI 都意味着它该归 core（详见 §2.1）。

---

## 1. 背景与痛点

插件化完成后，能力已经"按插件平级注册、SDK Runner 派发"。但遗留四处不合理：

1. **`group` 概念冗余且混入伪分组**。
   - `group` 字段 90% 是插件名的 1:1 影子（`loader.py:240` 用 `manifest.group` 作缺省），但有两个脏数据：
     - `device_emulator`：只是"哪个后端支持"的实现细节，被当成独立能力轴，导致 OCR 拆在 `device` 与 `device_emulator` 两个组。
     - `fs_pro`：只是"分页读 + 带上下文检索"的文件系统增强，被当成独立风险档。
   - 门禁机制不统一：`config.runtime.tools.groups` 管 shell/filesystem/device…，而 vision 用独立的 `runtime.vision.enabled` 注销 VLM 工具——同一件事两套写法。
2. **observe 被 loop 强制作成单独步骤**。
   - `observe` 本身是 `@function_tool`（`device/plugin.py:144`），但 `core.py:469-474` 的 `_build_user` 每个后续轮次硬塞一段 `[步骤 N] 当前环境状态: <缓存>` 合成块，模型被迫每轮处理观察，observe 工具反而被架空。与"能力=插件、模型直接调用"的北极星冲突。
3. **`run_python` 冗余且自相矛盾**。
   - `run_python` = `python -I -c <code>` 一次性子进程（`python_tool.py:72`），能力被 `shell_exec` 完全覆盖（`shell_exec` 可 `python script.py`），且不能落盘、更不通用。
   - 它**默认可 `os.system`、读任意文件，不比 shell 安全**，却默认常开；而等价能力的 shell 默认关。等于留了一条常开的代码执行后门绕过门禁（违 L0）。`sandbox-permission-design.md` §L1 本就把 `run_python` 与命令执行并列为"要审批"的危险动作。
4. **agent 产物默认落进项目目录**。
   - 截图默认 `<repo>/temp/*.png`（`devices/host.py:149`、`devices/emulator.py:403`）；agent 写的脚本经 `filesystem` 相对路径落在 `<repo>/`；`shell_exec` 的 `cwd=None` → 进程 CWD = 仓库根。项目目录被临时文件污染。

## 2. 设计决策（已全部与用户确认）

| 决策点 | 结论 | 备注 |
|---|---|---|
| **group 概念** | 删 `group`，改名为 `unit`（能力单元）。unit 承载可以是插件包，也可以是特殊 builtin | 消除冗余 + 统一门禁 |
| **device_emulator** | 合并进 `device`（7 处 `device_emulator` → `device`） | host 后端对不支持工具已优雅降级（`factory.py`），代价可忽略 |
| **fs_pro** | **合并进 `filesystem`**（删 `plugins/fs_pro/`，工具归一为 6 件） | ✅ **已实施（2026-09-25）**；⚠️ 本条原结论"保留为独立 unit"**作废**（用户改判）。归一细节：`read_range` → `read_file(offset=, limit=)`；`search_with_context` → `search_content(context=)`；`generate_report`（`write_file` 纯别名）删除 |
| **文件读写粒度** | **不拆分**：读/写视为**同一文件操作面**（同一插件、同一开关、同一门禁） | 2026-09-25 用户定："这种本来就是文件操作，别分开"；故不引入"读常开 / 写受门禁"的分离档 |
| **OCR 归属** | 留在 `device` unit，**绝不并入 vision** | 关 VLM 开关不能连带关掉文字识别（VLM 三件套由 `runtime.vision.enabled` 自门禁，OCR 在 device 轴） |
| **python** | **删除 `run_python`**；python 由 shell 承载（写脚本 → 执行） | 用户定：agent 惯例＝写 python 脚本再执行，不直接执行内联代码 |
| **shell** | **挪回内核 builtin**（`omni_core/tools/shell_tool.py`），默认常开、不可关 | 用户选 A：shell 是通用必备能力，安全加固归 S 线（当前不为它设风险闸） |
| **full_access** | **保留**，＝ **S2 审批流的总开关**：`true` → **不弹审批卡**直接放行；`false`（默认）→ 危险动作进审批。摘掉旧用途（放行 shell） | 用户明示：该开关决定 `sandbox-permission-design.md` §6/S2——「开了就不弹决策」。S2 未实现前仅保留字段与前端开关（暂 inert） |
| **skill** | core 常开、不在开关列表 | skill_tool 是核心编排能力 |
| **local_model** | 特殊 unit，门禁仍走 `llm.local_as_tool`，保留前端「推理通道」直配面板 | 不挪 plugins/，不破坏现有前端配置 |
| **mcp** | 特殊源（`source="mcp"` + 按 server 分 unit），门禁走 mcp 配置，前端每 server 一开关 | 不强行插件化 |
| **前端开关 / 插件** | 插件**只有一个 `enabled` 开关**；默认写在插件自己（`plugin.json`），用户改动落 `~/.omniagent/plugins/<name>.yaml`。前端只读这一个值 | 无多级配置 / 无适配层 / 无 per-tool 开关；删插件包或 yaml 都不影响 core |
| **observe / 通用化（B1）** | **硬删 loop 级观察特判**：`_build_user` step>0 环境块 + `[步骤 N]`（含 470-471 重复死行）；`sdk_bridge:314/316` 的**工具名分支**改为工具自声明的 `percept` 元数据分发（内核零工具名字面量）；删 `brain/tools.py` 死设备 schema | 不再"保留一处兼容"；verify 依赖的世界状态由 `percept` 声明驱动，**非名字匹配** |
| **临时产物目录** | agent 自写的脚本 / 截图等默认落 `tasks/<task_id>/tmp/`，**任务终态系统自动清理** | 用户定：只针对临时产物；绝不碰同目录的持久资产 |
| **产物落点机制** | `ContextVar` 绑定当前 task 的 tmp 目录；工具默认解析到它 | 不用模块全局（M6 子任务可能并发，全局会串） |

## 2.1 前端红线：前端零插件特判

与内核「零场景硬编码」同源——**前端不得认识任何具体插件**：

- **插件 ⇒ 前端必须通用**：一个开关（启用 / 停用）＋插件自报的元数据（`title` / `description`），不得有该插件专属的 UI / 分支 / 映射表 / 文案。
- **要自定义 UI ⇒ 它就不是插件**：应归 `core`（或其它非插件类目），由 core 提供稳定面板。
- **插件的详细配置**：走**插件自有文件** `~/.omniagent/plugins/<name>.yaml`，由插件自读；**不在前端做表单**（前端只读那一个 `enabled`）。
- 判据：新增 / 改名 / 删除一个插件，前端**零改动**。

## 2.2 反兼容红线：无用户 ⇒ 零兼容，删不迁移

用户口径（2026-09-24）：**「宁可完全删掉代码，也不背着旧的；现在没有任何用户，不叠技术债。」** 据此从"允许一次性迁移"收紧为**绝对零兼容**：

- 决定通用化 / 改名 / 删除的，**直接删旧代码**：不留 alias、不留 shim、不双读旧配置键、不留旧分支、不写"if 旧配置则走旧逻辑"、**不做数据迁移代码**。
- 旧配置键（`groups` / `python_exec` / `runtime.mcp` 回退 …）直接失效不读；旧磁盘格式不兼容即不兼容（无用户）。
- 判据：改完 grep 旧标识符（`group` / `groups` / `env_preview` / `python_exec` / `tool_registry` / `tool_loop` / 工具名字面量 …），应**零残留**。

## 3. 三类东西：环境 / 插件 / core

> 判定（2026-09-24）：**能扩展、能整体拿掉的才叫插件；自包含不了的归 core。** 环境是与插件并列的**第二种扩展点**（驱动型，单选）。

**环境** `environments/<name>/`（自带 driver + 全套工具；按 `runtime.backend` **单选**；前端 radio）：

| 环境 | 内容 |
|---|---|
| `host` | 本机：pyautogui / mss + observer（EasyOCR CPU）；工具 `press`/`click`/`type`/`observe`/`screenshot`/`template_match`/`wait_for` … |
| `emulator` | Android：uiautomator2 + EasyOCR GPU；在 host 基础上加 `ocr_screenshot`/`get_ui_tree`/`tap_by_id`/`tap_text`/`launch_app`/`press_keycode`/`collect_list` …；**设备侧文件 / shell 由 adb 工具承担（已定：加）**：`android_shell`/`android_pull`/`android_push`/`android_list_dir`，基于 u2 设备句柄实现（`d.shell` / `d.push` / `d.pull`，零额外 adb 路径配置；见 §4） |

> 工具**名字/参数/语义按各自环境自然定义**（host `press(key)` vs emulator `press_keycode(code)`），**不套统一签名**；不存在"通用工具层"。

**插件** `plugins/<name>/`（自包含；前端每个一个 on/off 开关）：

| 插件 | 内容 | 默认 `enabled` |
|---|---|---|
| `vision` | SoM/VLM（`runtime.py` 已在目录内，自包含） | false（治"每次都用 vision"） |
| `filesystem` | 文件操作**全套**：读（可分页 `offset/limit`）/ 写 / 编辑 / 列举 / 递归检索（可带上下文 `context`）/ 建目录（stdlib，自包含） | true |
| `web` | 联网（stdlib，自包含） | true |

**core（内核能力）**——不进插件开关列表，但**内部三分**（共享"不在插件列表"，理由各不相同）：

| 分格 | 内容 | 为何归 core |
|---|---|---|
| ① **执行原语** | `shell_exec`（唯一命令执行入口；Python 走"写脚本 → 执行"） | **命令面地基**：与设备层的键鼠面并列（键鼠面归环境）；去掉后只剩"思考 + 环境感知"，无命令面行动手段 |
| ② **编排能力** | `skill`（技能目录 / 按需加载）、元工具（`task_done` / `verify` / `escalate` / `record` / `plan`） | **核心编排**，非可选增强：去掉则循环收不了尾、不会复用策略 |
| ③ **接入机制** | `local_model`（本地推理通道，门禁 `llm.local_as_tool`）、`mcp`（外部工具来源，**按 server 开关**）、`brain` / `executor`（模型路由） | **通道与来源**，不是能力本身（配置型；可有自己的 UI） |

**适配层**（同为 core，但**不是模型可见能力**）：`devices/`（契约只剩 `kind` + `text_of` + `verify_done`）+ 环境选择（`runtime.backend`）；设备工具是**环境的工具面**，随环境切换。

> ⚠️ `filesystem` / `web` / `vision` **均为插件**（见上表），**不是 core 执行原语**（原 `fs_pro` 已于 2026-09-25 **并入 `filesystem`**，工具同为 6 件、只留一套文件操作）。
> 判据：自包含（stdlib）且**能整体拿掉**，而 `shell_exec` 已提供等价的命令面通道（`type`/`cat` 等）。
> 内核自身的文件 IO（AGENTS.md / 技能目录 / world_model / trajectory）走内核代码，不经过 `filesystem` 插件 ⇒ 关插件不影响内核机制。

> device 工具**不单列**——它们是**环境的工具面**（见上表），随环境切换。

## 4. 门禁与绑定：环境单选 + 插件开关（core 零具体知识）

- **环境**：按 `runtime.backend` **单选**；激活的环境注册其全套工具，其余不注册。换后端 = 重装。
- **环境配置**：`~/.omniagent/environments/<kind>.yaml`（该环境自有参数）；**选中哪个环境** = core 的 `runtime.backend`（前端 radio 写它）。`runtime.emulator.*` 从 config.yaml 迁出。
  - emulator 参数：`adb_path`（**留空 = 按 `ADBUTILS_ADB_PATH` → `ANDROID_HOME/platform-tools` → `PATH` 找 adb**；第三方模拟器自带 adb 常不在 PATH，此时填其 `adb.exe`）、`adb_serial`（**留空 = 自动取 `adb devices` 唯一在线设备**；否则显式：AVD `emulator-5554` / MuMu `127.0.0.1:7555` / 夜神 `127.0.0.1:62001` / 雷电 `127.0.0.1:5555`）、`resolution`。
  - 默认两项留空 → 全自动接通；连接失败时工具返回**自解释错误**（如 `未找到 adb:请在 ~/.omniagent/environments/emulator.yaml 配 adb_path`），不裸抛异常。
- **插件**：每个一个 `enabled` 开关；**默认**写在插件自己（`plugin.json`），**用户改动**落 `~/.omniagent/plugins/<name>.yaml`（前端只读这一个值）。loader 扫 `plugins/` 发现，关 = 不注册其工具。
- **环境↔插件绑定（最小化）**：插件默认**环境无关**（只吃工具产物：路径 / 文本）；需要绑的在 `plugin.json` 声明 `requires_env`（装载器按当前环境过滤，不匹配不注册）；`PluginContext` 只给**只读 `env_kind`**，**删 `execution` 厚句柄**。
- **删除**（反兼容）：全局 `config.runtime.tools.groups` / `disabled_units` / `DEFAULT_OFF_UNITS` / `effective_disabled_units`；per-tool 禁用（`runtime.tools.disabled` / `excluded` / `enabled_source` 等适配层）。
- `devices/` = **环境适配层**，契约只剩 `kind` + `text_of` + `verify_done`（core 只调这三个）；`ExecutionBackend` 的 `execute_*` / `observe` / `tap_by_id` 等**厚接口删除**。详见 §5.5。
- **core 概念**不是插件：可有自己的 UI / 配置，**只有插件不许有**。归属见 §3 的 core 三分——
  `brain` / `executor`（模型路由）与 `local_model` / `mcp` 同属 **③ 接入机制**；`shell_exec` 属 **① 执行原语**；`skill` 与元工具属 **② 编排能力**。

## 5. 执行能力归一（shell 承载 python）

- **删 `run_python`**，`shell_exec` 成为唯一命令执行入口（builtin）。
- **shell 描述引导**（工具描述是模型唯一入口，措辞如下）：

```
description =
  "在宿主机执行命令（cmd / powershell / bash），返回输出。"
  "需要写 Python 时：先用 write_file 把代码写成 .py 脚本（落在当前任务目录），"
  "再用本工具运行 `python <脚本名>`；不要把大段 Python 内联进命令行（不要用 python -c）。"
  "命令的工作目录、以及相对路径基准，默认 = 当前任务目录。"
  "注意：此工具在你的机器上真实执行命令。"
```

- `cwd` 默认由 `None`（→进程 CWD=仓库根）改为**当前任务 tmp 目录**。
- 诚实边界：描述只是**引导**非强制；项目明确不做命令级内容拦截，故目标是"高概率引导到 写脚本→执行"，不是保证。

## 5.5 环境层重构：`devices/` 适配层 + `environments/`

**现状耦合**：`devices/factory.py:37` 硬编码 `if kind=="emulator"`；`devices/__init__.py:21-22` 导出具体环境类；`devices/observer.py`（host 专属）混在共享包；6 个 loop mixin 重复 import `ExecutionModule`。

**目标结构**：
```
devices/                    # 适配层：core 真正需要的契约
  base.py                   # Environment 协议：kind + text_of + verify_done（仅此三项）
  registry.py               # register_environment(kind, factory) / create_backend(cfg)（查表，零硬编码名）
  module.py                 # ExecutionModule：持有当前环境，委派那 3 个
environments/
  host/     {backend.py, tools.py, observer.py}
  emulator/ {backend.py, tools.py}
```

- **厚接口删除**：`ExecutionBackend` 的 `execute_keyboard_action` / `execute_mouse_action` / `observe` / `read_screen_text` / `screenshot` / `get_ui_tree` / `tap_by_id` / `launch_app` / `press_keycode` / `normalize_coordinate` 全删——core 不调它们，各归本环境内部。
- 保留的 `ExecutionModuleProtocol`（本就只 3 个成员）升为**唯一契约**。
- `devices/__init__.py` 不再导出具体环境类；`devices/` 不 import `environments/`（反向：环境自注册进注册表）。
- 6 个 mixin 的重复 `from devices import ExecutionModule` 收敛到 `core.py` 一处。
- 环境工具用 `@function_tool` 注册（复用机制），但由**环境装载器**按 `runtime.backend` 装载（非 `plugins/`）。

**消费者同步**：`omni_core/__init__.py`、6 个 loop mixin、`tests/test_execution_backend.py`、`tests/test_m4_cleanup.py`、`scripts/verify_*_live.py`。

**配套**：
- 环境自带配置文件 `~/.omniagent/environments/<kind>.yaml`（见 §4）；`runtime.emulator.*` 迁出 config.yaml。
- emulator 的 adb 工具（`android_shell`/`pull`/`push`/`list_dir`）基于 u2 设备句柄（`d.shell`/`d.push`/`d.pull`），默认零额外 adb 路径配置。✅ 已实施（2026-09-25，见 §11.1-1）。
- system prompt 注入一行 **`当前环境: <kind>（<平台>）`**（已定：加），帮模型对齐语义（如 keycode）。✅ 已实施（2026-09-25）：平台名由环境自报（内核零硬编码），渲染为 `host（Windows）`。

## 6. 临时产物目录（task tmp）

**现状全是反例**（实测）：截图写 `<repo>/temp/*.png`；`shell cwd=None`→仓库根；`filesystem` 相对路径→仓库根。

**冲突点**：`tasks/<task_id>/` **不是空目录**，装的是持久资产——`task.json`、`trajectory.jsonl`、`world_model.md`、`collected.json`、`subtasks.json`、`skills/`、`AGENTS.md`（`runtime_paths.py:169-200`）、`todo.json`（`sdk_bridge.py:179`）。故**不能让临时产物平铺其内**，否则终态清理会误删轨迹 / world model。

**设计（方案 A：子目录）**：
1. **目录**：新增 `runtime_paths.task_tmp(task_id)` = `tasks/<task_id>/tmp/`；`ensure_task_dirs` 顺带创建。
2. **注入**：`ContextVar`（放 `omni_core/tools/` 下，插件可 import，与 `function_tool` 同源）；run 起始绑定 `task_id`/tmp 目录，子任务天然继承；运行结束清绑。
3. **各工具默认落点**：
   - `filesystem`：相对 `path` → task tmp；绝对路径暂不拦（S1 再上围栏）。
   - `shell_exec`：`cwd` 默认 = task tmp。
   - `screenshot`（device 插件）：默认 `save_path` = task tmp（不再用后端写死的 `repo/temp`）；`template_match` 的兜底截图同源。
4. **清理**：**任务终态（done / failed / aborted）由系统自动删** `tasks/<task_id>/tmp/`（`shutil.rmtree(..., ignore_errors=True)`），挂点在 `_finish`（Curator 同层）。确定性，不依赖模型自觉；**只删 `tmp/`，绝不碰同目录持久资产**。
5. **仓库卫生**：清掉现有 `<repo>/temp/` 并加 `.gitignore`。

## 7. 逐文件改动清单

### 7.1 内核注册层
- **`omni_core/tools/base.py`**：`ToolPlugin.group` → `unit`（**不留 `group` 别名属性**）；`function_tool(group=...)` → `unit=...`，新增 `percept=`（`state`/`collected`，写入 `ToolPlugin.meta`，供内核世界模型分发，**替代工具名分支**）；**删 `_plugins` 的过滤参数**（门禁改由 loader 按插件 `enabled` 决定是否注册）→ `_plugins()` 只做只读枚举；`sdk_tools/schemas/PluginRegistry/build_plugin_registry` 同步瘦身；**不加** `DEFAULT_OFF_UNITS` / `effective_disabled_units`（撤销）；更新 docstring（16-19、94、142-146 等）。
- **`omni_core/tools/loader.py`**：删 `manifest.group` 缺省机制（240-246）；**新增**读插件自有配置 `~/.omniagent/plugins/<name>.yaml`（`enabled`，缺省取 `plugin.json` 的 `enabled`）→ 关则不注册其工具；`RESERVED_TOOL_NAMES`（36-44）**删 `run_python`、加 `shell_exec`**。
- **`omni_core/tools/__init__.py`**：删 `run_python`/`configure_python` 导入与导出（28、63-64），加 `shell_exec`；docstring（9-14）同步（去 python，shell 提为 builtin）。

### 7.2 执行能力（删除 / 迁入）
- **删 `omni_core/tools/python_tool.py`**。
- **新增 `omni_core/tools/shell_tool.py`**：由 `plugins/shell/plugin.py` 迁入（函数体照搬），`configure` 保留读 `runtime.shell_exec`；描述按 §5 改写；`cwd` 默认改 task tmp。
- **删 `plugins/shell/`**（plugin.json + plugin.py）。

### 7.3 运行时（agent loop）
- **`omni_core/local/loop/core.py`**：删 `configure_python` 导入（32）与调用（147）；删 shell 默认排除（190）与 `full_access` 放行 shell（198-203）；188-197 `device_emulator` 增删逻辑删，改 `build_plugin_registry()`（**无参**：门禁由各装载器按 `runtime.backend` / 插件 `enabled` 决定注册与否，见 §4；中间态 `disabled_units` / `effective_disabled_units` / `self._units` **均已撤销**，`core.py:170` 为无参调用）；`full_access` 形参保留但不再影响分组（no-op，留作 S 线挂点）；`_build_user`（449-475）**硬删** env_preview 注入 + `[步骤 N]` 前缀 + **470-471 重复死行**（若分块重入需非空消息，改发中性「继续」，**不恢复环境块**）；docstring（151、453-455）更新。
- **`omni_core/local/loop/sdk_bridge.py`**：184 技能目录判定 `self._groups` → `self._units` 且判 `"skill"` unit；**314/316 工具名分支删** → 改按工具自声明元数据分发：`percept=="state"` → `world.update`；`percept=="collected"` → `world.add_collected_bulk`（内核零工具名字面量）。
- **`omni_core/brain/tools.py`**：删死代码 `TOOL_SCHEMAS` / `EMULATOR_TOOL_SCHEMAS` / `M3_TOOL_SCHEMAS`（全仓零外部引用，硬编码设备工具 schema），只留 meta 工具常量（`VERIFY/ESCALATE/TASK_DONE/RECORD_TOOL_SCHEMA` + `WORKER_EXTRA_SCHEMAS`）。
- **`omni_core/local/loop/graph_runner.py`**：run 起始绑定 task tmp ContextVar；**`_finish`（finish 路径）加终态清理 task tmp**。

### 7.4 API + 前端（通用渲染，零插件特判）
- **`backend/api/routers/tools_api.py`**：重写——返回 `environments: [{kind, title, active}]`（**radio 单选**，写 `runtime.backend`）+ `plugins: [{name, title, description, enabled}]`（开关，写 `~/.omniagent/plugins/<name>.yaml`）+ 只读工具清单（当前环境的工具 + core + 已启用插件）；删 groups / shell 特判（39-48）/ full_access 分组逻辑 / per-tool `disabled`；docstring 更新（19-22）。
- **`web/src/types/index.ts`**：`ToolsResponse` → `environments` / `plugins` / `tools`（只读）；`ToolInfo` 去掉 `group`/`group_enabled`。
- **`web/src/pages/SkillsAndTools/index.tsx`**：三块——**环境 radio / 插件 switch / 工具只读清单**；删 `GROUP_LABEL` 映射、`g == "shell"` 等名字分支、per-tool 开关。
- **`web/src/pages/Chat/index.tsx`**：「完全访问」开关保留（＝S2 审批总开关）；**弹窗文案去 shell 特判**（不写「cmd / powershell / bash」），改通用审批语义。
- **`web/src/pages/Settings/index.tsx`**：**只有 core 概念保留自定义面板**（`brain`/`executor`＝内核模型路由；`local_model`＝内核 builtin）；**插件不得有自定义面板** → `vision`（插件包）：**已定＝降为纯插件**（前端仅一个开关；详细配置移 `~/.omniagent/plugins/vision.yaml`（见 §4），Settings 不再有 vision 面板）。
- **`plugins/*/plugin.json`**：删 `group`，加 `title`（显示名）。

### 7.5 工具本体
- ⚠️ ~~**`plugins/device/plugin.py`**~~：**该插件已删除**（设备工具面迁入 `environments/<kind>/tools.py`，见 §5.5），本条目**已失指**。其所要求的属性现于**环境工具面**声明 ✓：`observe`/`read_screen_text`/`ocr_screenshot` → `percept="state"`、`collect_list` → `percept="collected"`；`screenshot`/`template_match` 默认落 task tmp。
- **`omni_core/tools/skill_tool.py`**：设 `unit="skill"`（✅ 已实施）。⚠️ 原稿的 `essential=True` **判定过时、不实现**（core 工具不带开关即不可关，见 §11.1-2）。
- **`omni_core/tools/local_model_tool.py`**：`GROUP` → `UNIT="local_model"`。
- **`plugins/*/plugin.json`**（device/vision/filesystem/fs_pro/web）：删 `"group"` 键。

### 7.6 配置 / 文档
- `config.py:210` 的 `python_exec` → 死键，删除（缺省 `CONTEXT_DEFAULTS`）。
- docstring 同步：`__init__.py`（9-14）、`base.py`、`tools_api.py:19-22`、`sandbox-permission-design.md` §5/§L1（`run_python` 相关改指 shell）、`tool-plugin-master-plan.md` §13（内核必备清单去 `run_python`）。

### 7.7 测试
- **删 `tests/test_m3_python.py`**（整体）。
- `tests/test_o2_encoding.py`：去 `run_python` 两例（shell_exec 编码例保留）。
- `tests/test_m3_tools.py:277/287`：去 `run_python` 断言。
- `tests/test_api_integration.py:164/168-169`：`run_python` builtin 断言换等价内置工具（如 `skill` / shell）。
- `tests/test_agent_run.py:151/154`：样本名 `run_python` 换掉。
- `tests/test_u2_tool_disable.py:42`：`run_python` 换一个仍存在的内置工具。
- `tests/test_plugin_loader.py:231-237/319`：保留名样本换 `task_done`；保留名集合断言随之更新（`shell_exec` 新增）。
- `tests/test_provider_registry.py:69` `sdk_tools(["grpA"])` → 新排除式 API。
- `tests/test_api_integration.py:173/180-198`、`test_t24_skill_catalog.py:157-159`、`test_agent_run.py:143` group → unit；`helpers.py`（`_enabled_groups` ~126、464）一并改。
- 新增 / 更新：shell builtin 常开断言、task tmp 落点 + 终态清理断言。

### 7.9 遗留兼容 / 死代码清除（反兼容红线落地）

`omni_core` 内已实测的"背着的旧代码"：

**A. 纯 shim / 死代码（直接删，低风险）**
- `omni_core/local/tool_loop.py`：整文件转发 shim（"兼容 shim：历史 import 全部照常可用"）→ 删；改 `tests/test_m3_tools.py:232` 等直接 import `omni_core.local.loop`。
- `omni_core/tools/base.py:262` `tool_registry = TOOL_REGISTRY`（"与旧 ToolRegistry 语义对齐"）→ 删；改 `core.py:186-187` 的 `tool_registry` 回退。
- `omni_core/local/loop/graph_runner.py:66` `run_task_two_layer`（"降级为兼容别名"）→ 删。
- `omni_core/local/loop/graph_runner.py:177` `load_matching_skills`（"保留兼容存量调用，此处不再使用"）→ 删。
- `omni_core/brain/tools.py` 死 schema（见 §7.3）→ 删。
- `devices/factory.py:117` `execute_decision()`（"保留以兼容 state_manager 工作流"，该工作流已不存在）→ 删。

**B. 过渡期兼容（删，同步改各自调用点）**
- `omni_core/tools/local_model_tool.py:165` "优先旧 `runtime.executor` 端点（过渡期）" → 删旧回退，只走 `llm.local_as_tool` / resolver。
- `core.py:149` 插件"读取 runtime 下的**旧配置键**（用户配置零改动）" → 统一到**插件自有配置** `~/.omniagent/plugins/<name>.yaml`（见 §4）；**删除 `config.runtime.plugins.<name>` 入口**与 `runtime.filesystem/shell_exec/web` 等插件键。
- `core.py:186-187` "tool_registry 优先注入实例，否则回退全局（向后兼容）" → 按新调用形态收敛。
- `config.load_mcp_config()` 回退 `config.runtime.mcp`（记忆标注"待清理"）→ 只留 `~/.omniagent/mcp.json`。
- `omni_core/tools/mcp_servers.py:37-38` "同时兼容原始名与带前缀暴露名" → 收敛为一种命名。
- `plugins/vision/plugin.py` 读旧 `config.runtime.vision`（含 `backend/api/router_settings.py` 默认 `runtime.vision.enabled=False`）→ 统一到 `~/.omniagent/plugins/vision.yaml`（`enabled` + 端点/模型；见 §4）。
- `devices/factory.py::_load_config`（直读项目根 `config.yaml`）与 `devices/observer.py::_model_path`（同上）→ 统一走 `config` 模块（core 已加载），不各自读盘。

**C. 判断类（已定）**
- 删：`world_model.py:425` 旧文件无 `facts_meta` 退化；`sdk_bridge.py:325-328` `_summarize` 旧的单参调用兼容；`skill_library.py:232/315` 兼容原 `objective_pattern` / 纯 `.md` 技能无 frontmatter。
- 保留（功能分支，非旧债）：`emitter.py:134` 非流式回退；`core.py:402` `run_id=None` 自生成。

> 已定（2026-09-24）：**A + B 并入本次**（纯删，无新语义）；**C 如上（三项删 / 两项留）**。

## 8. 风险点（开工先核实）
1. **loader 覆盖 unit**：插件工具 unit 以插件名为准，插件内 `unit=` 须与插件名一致，否则分裂。
2. **full_access 变 no-op（S2 前）**：字段/API/task.json/前端全保留，但不再影响任何分组；须确认无代码仍依赖它启停 shell（`core.py:202`、`tools_api.py:47` 两处删除）。其**正式语义**＝S2 审批流总开关（开则不弹卡片直接放行，见 `sandbox-permission-design.md` §6）；S2 落地前该字段 inert，但不下线。
3. **chunk 重入**：确认 `_build_user(step>0)` 在 `graph_runner` 分块处是否被当作非空用户消息；若依赖，改发中性「继续」，**不得为过测试恢复环境块**。
4. **OCR 边界**：绝不可把 OCR 工具的 unit 改成 `vision`。
5. **task tmp 清理时机**：清理挂终态（done/failed/aborted），非每个 run 结束——长任务跨 run 续跑时，上一 run 写的脚本若被后续 run 复用会失效（临时产物按定义允许重建，接受）。**只删 `tmp/`**，务必不碰同目录持久资产。
6. **ContextVar 并发**：必须用 ContextVar（非模块全局），否则 M6 并发子任务的 tmp 基准会串。
7. **shell 常开的安全后果**：S 线落地前，宿主机命令执行无门禁、默认常开（L0「shell 默认关」被主动放弃）——已知并接受，S 线以「危险行为拦截 + full_access」补齐。

## 9. 验收标准
- `scripts/review_lint.py --strict` 通过（红线：无 app/场景硬编码）。✅ **通过**（2026-09-25 实测：34 内核文件 + `devices/`，零违规）。
- 相关单测随本计划更新后全绿（含删 `test_m3_python.py`）。⚠️ **已判定不追（2026-09-25 用户决定）**：卡死点已修（`test_m3b.py` 补 `max_steps`）；历史失败集**现已全过**（该批 44/44）；残余 `F` 为**顺序依赖假失败**（单跑全过），根因＝测试基础设施的进程级全局状态 ⇒ **不视为本计划回归** → §11.1-4。
- 前端「能力单元」页（**现状**＝环境 radio / 插件 switch / 工具只读清单）：unit 标签完整 ✓；**shell 不再作为开关出现** ✓；`local_model` 开关随「推理通道」面板（`llm.local_as_tool`）✓。⚠️ 原稿的「essential 锁定」**判定过时**（core 工具不带开关即不可关，无需该字段）→ §11.1-2。
- 行为验证：shell 默认可用且是唯一执行入口 ✓；agent 写的脚本 / 截图落 `tasks/<task_id>/tmp/` ✓（`workspace.py` ContextVar + 各工具默认解析）；任务结束后 `tmp/` 被清空、而 `trajectory.jsonl`/`world_model.md` 等持久资产**完好** ✓（`graph_runner._cleanup_task_tmp` 只 `rmtree` tmp；`tests/test_task_tmp.py` 看守）；⚠️ `<repo>/temp/` **历史残留未清**（不再新生 ✓，但旧产物仍在目录里）→ §11.1-3。
- observe 工具化：模型自主调 observe，loop 不再注入环境块，verify 完成判定仍正常（依赖 `world.update`）。

## 10. 实施顺序
```
内核 base/loader ──→ 执行能力(删 python_tool / 迁入 shell_tool / 删 plugins/shell)
   ──→ 工具本体(device/skill/local_model/plugin.json) ──→ 运行时(core/sdk_bridge/graph_runner)
   ──→ task tmp(ContextVar + 各工具默认 + 终态清理) ──→ API/tools_api ──→ 前端 types/SkillsAndTools
   ──→ 遗留兼容清除(§7.9 A+B) ──→ 测试 + docstring/配置 ──→ 验收(lint + 测试 + 行为验证)
```
串行，每步跑 lint + 相关测试，避免跨层回归。

---

## 11. 结项结论（2026-09-25 逐条核对代码后）

核对口径：本计划自身的判据（残留标识符 grep 零命中 / `review_lint --strict` / 单测 / 行为验证）。

**结论：代码侧无遗留。** 曾判定"未实现"的 4 项 → 2 项**已补做**、1 项**判过时**、1 项（单测全绿）**判不追**；4 项"有意偏离"保留并留理由。`review_lint --strict` ✅ 通过。

### 11.1 缺口清单（已补做 / 判过时 / 不追）

| # | 条目 | 出处 | 现状（实测） |
|---|---|---|---|
| 1 | 设备侧 adb 工具 `android_shell` / `android_pull` / `android_push` / `android_list_dir` | §3 emulator 行、§5.5 配套（标注"**已定：加**"） | ✅ **已补做（2026-09-25）**：`environments/emulator/backend.py` 加 `device_shell`/`device_push`/`device_pull`/`device_list_dir`（+ `_shell_result` 归一 u2 新旧返回），`environments/emulator/tools.py` 加同名 4 工具；本地路径基准走 `workspace.resolve_path`（task tmp）。冒烟：emulator 工具面 **22 件**含该 4 件 |
| 2 | `skill_tool` 的 `essential=True` + 前端「essential 锁定」 | §7.5、§9 | **已判定过时 → 不实现**（2026-09-25 用户确认）。全仓（含 `web/src`）无 `essential` 字段；且 **core 工具本就不带开关**、不出现在插件列表，"必备工具不可关"已由**结构**保证，该字段无意义 |
| 3 | 清掉仓库 `<repo>/temp/` 历史产物 | §6.5 | ✅ **已完成（2026-09-25）**：删 **50 个文件**（76 → 26）；保留运行日志（`omniagent.log`/`backend.*`/`web.*`）、模型进程日志（`llama_server_*`）、工具链包（`llama-*.zip`）。⚠️ 仅剩 4 个 `pytest-review*` 目录因**环境 safe-delete 守卫**拦截未能删除 → 手动 `Remove-Item temp\pytest-review* -Recurse -Force` 即可，**不影响本计划结项** |
| 4 | "相关单测全绿" | §9 | ⚠️ **判定不追（2026-09-25 用户决定）**。实测：① 按序全量的**卡死点已修**（`test_m3b.py::test_m7_happy_path` 补 `max_steps=20` —— 缺省 `None` 在内核语义上是"不限"，子 agent 感知不到 `role=="tool"` 即无界 observe）；② `.pytest_cache` 记的 5 条历史失败**现已全过**（该 5 文件 44 例 100%）；③ 残余 `F` 属**顺序依赖假失败**（单跑全过），根因＝**测试基础设施的进程级全局状态**（`TOOL_REGISTRY` / `environments/*/tools._BACKEND` / `AsyncBridge` / config 缓存）⇒ **不视为本计划回归**。⚠️ 把 `_BACKEND` 改 ContextVar 需跨线程 context 传播（`run_coroutine_threadsafe` 不复制 context）→ **独立任务，勿顺手改** |
| 5 | system prompt 注入 `当前环境: <kind>（<平台>）` | §5.5 配套（标注"**已定：加**"） | ✅ **已补做（2026-09-25）**：平台名由**环境自报**（`HostBackend.platform="Windows"` / `EmulatorBackend.platform="Android"`，内核零硬编码）→ `ExecutionModule.platform` → `graph_runner` 两处 ctx（主链 + worker）→ `prompt._platform_suffix`。实测渲染 `- 当前环境：host（Windows）` |

### 11.2 有意偏离（本计划要求做，核对后判定不做 —— 理由）

| 条目 | 计划要求 | 判定与理由 |
|---|---|---|
| `omni_core/tools/mcp_servers.py::_build_tool_filter` 双名匹配 | §7.9 B：`raw` 与带前缀名"收敛为一种命名" | **保留**：两种写法都能匹配＝**用户 glob 便利**（`read_*` 与 `mcp_fs__read_*` 皆可），非旧代码残留 |
| `omni_core/local/world_model.py:425` | §7.9 C：删"旧文件无 `facts_meta` 退化" | **保留**：`data.get("facts_meta") or {}` 是**防御性默认**，删掉会 TypeError（非格式兼容分支） |
| `omni_core/local/skill_library.py:232/315` | §7.9 C：删"兼容原 `objective_pattern` / 纯 `.md` 无 frontmatter" | **保留**：该两处是**注释**（解释召回策略），**不是代码**，无可删 |
| `graph_runner::run_task_two_layer` | §7.9 A：删"降级为兼容别名" | **该函数不存在**（测试本就断言其缺席），条目失效。同批 `devices/factory.py::execute_decision` / `_load_config`、`devices/observer.py::_model_path` 随 `devices/` 重构**整体消失**（已被 §5.5 结构重构吸收） |

### 11.3 本次修正的计划内部过时 / 矛盾（文档侧，已改）

- **状态行**：`待实施` → `已实施（2026-09-24 设计 → 2026-09-25 收口）` → **`已结项（2026-09-25）`**。
- **§7.3 与 §4 自相矛盾**：§7.3 原写 `disabled_units = effective_disabled_units(cfg)` + `build_plugin_registry(disabled_units)`，而 §4 要求**删除**这两个 → 现实现为 `build_plugin_registry()` **无参**（`core.py:170`），中间态 `self._units` 亦已撤销；§7.3 该句已更正。
- **§7.5 `plugins/device/plugin.py` 条目失指**：该插件**已删除**（设备工具面归 `environments/<kind>/tools.py`）；其要求的 `percept` 声明现由环境工具面承担 ✓。§1、§3 中 `device/plugin.py:144` 的引用同因失效。
- **§9 验收措辞对齐现状**：前端＝环境 radio / 插件 switch / 工具只读；`shell` 确实不再是开关 ✓，但"essential 锁定"从未实现。
- **`plugins/vision/plugin.py` docstring 陈旧**（第 15、52 行）：曾写旧键 `ctx.config["runtime"]["vision"]` 与 `ctx.execution`（实现早已改为 `plugin_config("vision")` 读插件自有 yaml，且 `PluginContext` 已**删除 `execution` 厚句柄**）→ ✅ **已修（2026-09-25）**：3 处注释改为现状描述（纯文本、零行为）。


