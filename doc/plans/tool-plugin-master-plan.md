# OmniAgent 工具插件化总体计划（Master Plan）

> **日期**：2026-09-24
> **状态**：**P1 / P2 / P3 已执行完成并通过验收**（见文末「执行记录」）；安全线 S0-S3 未开工（见 `sandbox-permission-design.md`）
> **文档说明**：原《工具插件化与安全加固总体计划》按主题拆为两份——**本文＝工具插件化（P1-P3）**；**安全与沙箱（S0-S3）见 `doc/plans/sandbox-permission-design.md`**。两份互为唯一来源，勿再新建分文档；修订直接改对应文件并更新状态行。
> **基线**：`main` = `github/main` = `ff3c800`；工作区唯一未跟踪文件 = `doc/plans/sandbox-permission-design.md`（即零未提交代码改动）
> **执行机**：本机 Windows，用户 `vilya`，Python 3.12.8（仓库内 `.venv`），shell 为 PowerShell
>
> **本机适配说明**：本版已按本机环境改写。原稿中的 TSD 透明加密注意事项、`stash p1-wip-tsd-blocked`、`temp_venv` / `temp_venv_broken_tsd`、`C:\Users\v_leishen\...\Python310` 均属**另一台机器（用户 `v_leishen`）**，本机不存在，已替换为本机事实。原稿基线写作 `444b710`，该对象在本仓库不存在。

## 执行顺序

```
P1 插件机制 ──→ P2 迁移(fs/shell/web) ──→ P3 迁移(device/vision)
```

串行，每步验收通过才进下一步。
并行规则（跨文档）：P2 完成后，安全线的 S0+S1 与本线的 P3 可并行（互不触碰同一文件）。

---

# 第 0 部分：设计总纲

## 0.1 背景与痛点

M5 之后工具层已是「规范半成品」：`omni_core/tools/base.py` 的 `@function_tool` 装饰器即插件规范（写函数 + Google docstring 出 schema + group 分组），注册进 `TOOL_REGISTRY` 后由 SDK Runner 派发，内核零参与。但新增一个用户侧工具仍然必须改内核代码，耦合在三处：

| # | 耦合点 | 位置 |
|---|---|---|
| 1 | 发现是手动的：新工具模块必须进 `tools/__init__.py` 的硬编码 import 清单 + `__all__` | `omni_core/tools/__init__.py` |
| 2 | 生命周期钩子各自为政：`configure_python` / `configure_shell` / `configure_filesystem` / `configure_web` / `bind_execution_module` / `bind_vision_runtime`，签名与调用时机无统一协议，内核逐个点名 | `omni_core/local/loop/core.py` |
| 3 | 内核对具体工具符号的直接 import：`local/loop/*`、`brain/sdk_agent.py` 等模块按名引用，工具迁出即 `ImportError` | 实测依赖面见第 2 部分 |

实证：`t_ac6b5e00e3f5` / run `1fd875fd739c`（记忆实现分析任务）中，agent 因 `read_file` 无 offset 翻页能力连续 9 步撞墙后被迫用 `run_python` 内联代码自实现分段读取——工具能力原始 + 不可扩展的代价被轨迹直接量化。插件化后此类增强都变成「放个目录」的插件事务，不动内核。

工具面扩大（文件读写、shell、Python 执行）之后的**约束面**（权限预设 / 路径围栏 / 审批 / 沙箱）不在本文，见 `sandbox-permission-design.md`。

## 0.2 目标与红线

**目标**：
- 用户侧新增工具 = 放一个目录（manifest + 入口文件），零内核改动。
- 除内核必须工具外，现有工具全部迁出为插件。
- 单插件加载失败只降级、不拖垮 agent。

**红线（贯穿全部阶段）**：
- **C1 工具零写死**：内核不出现任何工具名分支（`review_lint` R2 继续卡）。
- **C-P1 loader 零依赖具体插件**：loader/base 不 import 任何具体插件模块。
- **C-P2 单插件失败不阻断**：任何插件异常不传播出 `load_plugins`。
- **C-P3 保留名与重名保护**：注册表拒绝保留名 / 已占名。
- **C-P4 视图零破坏**：PluginRegistry / groups / disabled / 枚举 API 行为不变（响应只增 `plugins` 节）。
- **安全层不进具体插件**：S2 审批拦截在 `base.py` 的 `function_tool` 装饰器统一注入，插件作者无感知、无法绕过（安全侧细节见 `sandbox-permission-design.md`）。
- **派发始终由 SDK Runner 原生接管**，loader/安全层不碰派发。

**非目标（明确不做）**：
- ❌ pip 分发 / `entry_points` 自动发现（无多用户分发需求）
- ❌ 热重载 / effect 化自动卸载（仅 `shutdown()` 钩子，重启生效）
- ❌ DI 容器 / inject 服务解析
- ❌ DSH 式 bundle/patch YAML 组合（沿用 `config.runtime.tools.groups` + `disabled`）

## 0.3 架构总览

```
启动（ToolLoop 装配时）
  │
  ├─ ① builtin 内核工具：omni_core/tools/ 硬编码 import（仅保留内核必须件）
  │
  ├─ ② loader 扫描 plugin_dirs/*/plugin.json
  │    └─ importlib 动态加载 plugin.py
  │         ├─ 模块级 @function_tool 装饰器 → 自动注册 TOOL_REGISTRY（机制不变）
  │         ├─ configure(cfg)   ← config.runtime.plugins.<name>
  │         └─ startup(ctx)     ← PluginContext（vision/device 绑定句柄）
  │
  ├─ ②' function_tool 装饰器内：S2 审批拦截（查 ApprovalGate → 高危挂起等人工决议）
  │
  ├─ ②'' 工具入口：S1 PathPolicy 路径围栏校验（写类 filesystem 工具）
  │
  └─ ③ 现有 PluginRegistry 视图照常构建（groups / disabled / S0 预设换算）
       └─ SDK Runner 派发（完全不变）

进程退出（backend/server.py lifespan）
  └─ shutdown_plugins()：依次调各插件 shutdown()
```

加载顺序刻意为 builtin 先、插件后：插件可依赖 builtin 提供的绑定句柄（`PluginContext`），反向不成立。只读枚举路径（`GET /api/runtime/tools`）同样幂等触发一次 `load_plugins(cfg)`。

> ②' 审批拦截与 ②'' 路径围栏只在此处标注接入点，**实现细节属安全线**（见 `sandbox-permission-design.md` 的 S1 / S2）。

## 0.4 插件规范

### 0.4.1 目录约定

```
plugins/                     # 默认根目录（config.runtime.tools.plugin_dirs 可配多个）
  my_pack/                   # 插件包（目录名 = 插件名，须为合法 Python 标识符）
    plugin.json              # manifest（必需）
    plugin.py                # 入口（必需，文件名固定）
    README.md                # 契约文档（建议）
```

点开头目录（`.git` / `.omniagent` 等）静默忽略；无 `plugin.py` 的普通目录静默跳过。

### 0.4.2 manifest（`plugin.json`）

| 字段 | 类型 | 必需 | 说明 |
|---|---|---|---|
| `name` | string | ✅ | 插件名，须与目录名一致（不一致拒绝加载） |
| `version` | string | ❌ | 仅展示 |
| `description` | string | ❌ | 仅展示（工具 description 以 docstring 为准） |
| `group` | string | ❌ | 默认能力组；缺省取 `name`，工具级 `@function_tool(group=...)` 可覆盖 |
| `disabled` | bool | ❌ | manifest 级禁用，与 `config.runtime.tools.disabled` 叠加 |
| `permissions` | string[] | ❌ | S1 安全声明：`fs_write` / `fs_read` / `shell` / `net`（枚举展示与审计用） |

### 0.4.3 入口文件（`plugin.py`）

与 builtin 完全同构：`@function_tool(description=..., group=...)` + Google docstring（Args 段出 schema）。

可选生命周期钩子（统一签名，存在即调）：

```python
def configure(cfg: dict) -> None: ...           # config.runtime.plugins.<name> 节
def startup(ctx: "PluginContext") -> None: ...  # 内核装配（vision/device 就绪）后
def shutdown() -> None: ...                     # 进程退出前
```

### 0.4.4 PluginContext

```python
@dataclass
class PluginContext:
    vision: Any = None        # VisionRuntime；None = 未启用
    execution: Any = None     # ExecutionModule；None = 未装配
    config: Dict[str, Any] = field(default_factory=dict)  # 全局 config 快照（只读约定）
```

### 0.4.5 命名规则与保留名

- **保留工具名**（注册表拒绝占用）：`run_python`、`task_done` / `verify` / `escalate` / `record` / `plan`（元工具由 `tool_loop` 门控）。
- **重名策略**：与任何已注册名冲突（含覆盖 builtin）→ 拒绝整包并回滚，错误信息含"保留名"/"覆盖已注册工具"。

## 0.5 加载器设计（`omni_core/tools/loader.py`）

```python
load_plugins(cfg, ctx) -> LoadReport
  for root in plugin_dirs:
      for d in sorted(root.iterdir()):            # 目录名排序 = 加载顺序（可预期）
          隐藏目录 → 忽略；目录名非标识符 → skip；无 plugin.py → skip
          manifest 缺失/非法/名字不一致 → fail；disabled → skip
          快照注册表 → importlib 动态 import plugin.py（模块名 _omni_plugin_<name>）
          命名校验（保留名/覆盖已注册）→ 违例整包回滚 + fail
          configure(cfg) → startup(ctx)
          失败 → 回滚 + fail，继续下一包
          记录 _loaded_packages / _loaded_modules
  return report（loaded / skipped / failed 三表；缓存为 last_report()）
```

- **幂等**：`_loaded_packages` 集合，重复装载记 skip。
- `shutdown_plugins()`：退出时按包名序调 `shutdown()`，异常吞掉。
- `plugin_module(name)`（P3 增量）：公开取已装载插件模块，测试注入用。
- 配置键（缺省值以代码为唯一出处，不进 `config.example.yaml`）：`runtime.tools.plugin_dirs`（缺省 `["plugins"]`）、`runtime.plugins.<name>`（各插件私有节）。

## 0.6 内核必须工具边界（定稿）

| 类别 | 工具 | 留在内核的理由 |
|---|---|---|
| 元工具（tool_loop 门控，不在注册表） | `task_done` / `verify` / `escalate` / `record` / `plan` | 循环控制流，与 L2 门控强耦合 |
| 逃生舱 | `run_python` | 插件全挂时 agent 仍具备计算/自举能力（对齐 DSH 保留 run_code） |
| 知识层门面 | `skill_tool` | 内核 `sdk_bridge` 每 run 下行注入 `set_skill_task_context(task_id)`；迁移会让内核反向 import 插件模块，违反 C-P1 |
| 动态注册 | `local_model_tool` | 配置驱动注册/注销，随每个 ToolLoop 实例重配，非静态能力插件 |
| 基础设施 | `base.py` / `loader.py` / `mcp_servers.py` / `vision_runtime.py` | 规范与装配设施本身 |

迁移对象（第 2 部分）：`filesystem` / `shell` / `web`（P2）→ `plugins/`；`device` / `vision`（P3）→ `plugins/`。

---

# 第 1 部分：P1 实施（机制 + 示例插件，从零）

基线：工作区 = `HEAD`（`ff3c800`），零未提交代码改动。

## 1.1 环境事实（本机）

| 项 | 值 |
|---|---|
| 解释器（下称 `PY`） | `.venv\Scripts\python.exe` = **Python 3.12.8** |
| 已有依赖 | `pytest` / `PyYAML` / `httpx` / `fastapi` 可用；`openai-agents 0.22.2`（满足 `>=0.22.2`） |
| shell | **PowerShell**：`cd /d <path> && <cmd>` 会报 `Set-Location 找不到位置形式参数`，且 `&&` 不短路；正确写法 `Set-Location <path>; <cmd>` |
| 文件读取 | 本机**无 TSD 透明加密**，`read_file` / `type` / IDE 读取均可信（原稿的 TSD 坑属另一台机器，本机不适用） |
| 运行态后端 | 不要加 `--reload`（Windows 双监听坑） |

## 1.2 Step 0：确认测试解释器

本机 `.venv` 已就绪，**无需重建**（原稿的 `ren temp_venv temp_venv_broken_tsd` 那套是另一台机器流程）。只做一次体检：

```
.venv\Scripts\python.exe -c "import sys; print(sys.version)"
.venv\Scripts\python.exe -c "import pytest,yaml,httpx,fastapi; print('core deps ok')"
.venv\Scripts\python.exe -c "import agents; print('openai-agents', agents.__version__)"
```

**已通过**（2026-09-24 实测）：`3.12.8 (tags/v3.12.8:2dc476b, Dec  3 2024, 19:32:04)` / `core deps ok` / `openai-agents 0.22.2`。

若后续某测试因缺依赖失败（如 `PIL` / `mss` / `PyAutoGUI` / `opencv-python`），按需 `PY -m pip install -q <包>` 补装即可，不装 `torch` / `easyocr` / `ultralytics`。

## 1.3 Step 0.5：基线回归预跑

工作区此刻是干净基线，先记录预存在失败：

```
PY -m pytest tests\test_m0_tool_plugin.py tests\test_m3_tools.py tests\test_m3_python.py tests\test_u2_tool_disable.py tests\test_api_integration.py -q
```

书面记录失败用例清单。后续一切验收的**允许失败集合 = 本步记录的集合**。
已知疑点（待实测确认）：`test_m3_tools.py` 两个 `template_match`、`test_api_integration.py` 的 `som_marks` 断言。

## 1.4 Step 1：新增 `omni_core/tools/loader.py`

契约（语义见 §0.5）：
- 导出：`RESERVED_TOOL_NAMES`、`PluginContext`、`LoadReport`（含 `skip` / `fail` / `as_dict`）、`load_plugins(cfg, ctx)`、`shutdown_plugins()`、`last_report()`。
- 失败隔离：单包 import/钩子异常 → 回滚该包已注册工具（注册表快照恢复）→ 记 `failed` → 继续下一包。
- 日志 `from utils import get_logger; logger = get_logger("tools.loader")`。
- **不 import 任何具体插件**。

验收：

```
PY -c "from omni_core.tools.loader import load_plugins, PluginContext, LoadReport, shutdown_plugins, last_report, RESERVED_TOOL_NAMES; assert 'run_python' in RESERVED_TOOL_NAMES; print('loader ok')"
```

## 1.5 Step 2：示例插件 `plugins/fs_pro/`

- `plugin.json`：`{"name": "fs_pro", "version": "0.1.0", "description": "...", "group": "fs_pro"}`。
- `plugin.py`：
  - `configure(cfg)` 读 `max_output`（缺省 8000）；
  - `read_range(path, offset=0, limit=200)`（读 `[offset, offset+limit)` 行，返回 `{ok, path, total_lines, offset, end_line, has_more, content}`，content 带行号前缀、按 `max_output` 截断）；
  - `search_with_context(pattern, path=".", glob="*", context=2, max_matches=20)`（递归正则搜索，命中返回 `{file, line, text, context: [{line, text}...]}`）。
  - 两工具 `@function_tool(group="fs_pro")`，不占保留名。

验收：

```
PY -c "import json; m=json.load(open(r'plugins/fs_pro/plugin.json', encoding='utf-8')); assert m['name']=='fs_pro'; print('manifest ok')"
```

## 1.6 Step 3：四处接线（最小增量）

| 文件 | 接线 |
|---|---|
| `omni_core/tools/__init__.py` | `import` loader 五符号 + `__all__` |
| `omni_core/local/loop/core.py` | `self.mcp_servers = ...`（实测 `core.py:188`）之后、设备工具收窄段之前：调 `load_plugins(_cfg, ctx=PluginContext(vision=self.vision, execution=self.exec, config=_cfg))` 存 `self.plugin_report`；`failed` 非空时 `self._log`。函数内局部 import |
| `backend/api/routers/tools_api.py` | `configure_local_model_from_config(cfg)`（实测 `tools_api.py:34`）后调 `load_plugins(cfg)`（幂等）；响应加 `"plugins"` 节 = `last_report().as_dict()`（无报告时空三表） |
| `backend/server.py` | `lifespan` `yield` 后 `try` 内调 `shutdown_plugins()`，`except` 记 warning |

验收：

```
PY -c "import omni_core.tools; from omni_core.tools import load_plugins; print('init ok')"
PY -m py_compile omni_core/local/loop/core.py backend/api/routers/tools_api.py backend/server.py
```

## 1.7 Step 4：测试 `tests/test_plugin_loader.py`

17 用例（fixture：注册表快照恢复 + loader 幂等状态清理 + `_omni_plugin_*` 模块缓存清理；`tmp_path` 造插件目录）：

①装载成功组注册+按名派发 ②`configure` 收私有节 ③钩子顺序 `configure`→`startup` 且收到 `PluginContext` ④单包 import 炸不殃及同批 ⑤`startup` 炸回滚已注册工具 ⑥占保留名拒载 ⑦覆盖 builtin 拒载且原对象未替换 ⑧无 manifest/非法 JSON fail ⑨name 不一致 fail ⑩`disabled` skip ⑪普通目录/非标识符目录 skip ⑫双重 load 幂等 ⑬`plugin_dirs` 不存在空报告 ⑭保留名集合定义 ⑮`last_report` 缓存 ⑯`shutdown` 调用且异常吞噬 ⑰真实装载仓库 `plugins/`：`fs_pro` + `read_range` 分页语义（`requirements.txt` offset=2/limit=3 → `end_line=5`、`has_more`、行号前缀）+ `search_with_context` 带 context 数组。

验收：`py_compile` 通过。

## 1.8 Step 5：验证

```
PY -m pytest tests\test_plugin_loader.py -q          # 17 passed
PY -m pytest <Step 0.5 同一回归集> -q                # 失败 ⊆ 基线集合
```

`git status` 改动恰为：修改 4 接线文件；新增 `omni_core/tools/loader.py`、`plugins/`、`tests/test_plugin_loader.py`、`doc/plans/tool-plugin-master-plan.md`（本计划文档）。

---

# 第 2 部分：P2+P3 迁移（builtin 迁出为官方插件）

前置：第 1 部分验收全通过。改写范围 = §2.1 依赖面清单，不得扩大。

## 2.1 依赖面（已在 `ff3c800` 基线复验）

**死 import（P2 顺手清理）**：`omni_core/local/loop/` 下 `emitter` / `finish` / `graph_runner` / `instructions` / `parts` / `sdk_bridge` 六个文件的 `from omni_core.tools import (...)` 块（实测位于各文件 28~37 行）——**正文零使用**（Mixin 拆分逐字搬移遗留），整块删除；各文件 `from omni_core.tools.vision_runtime import VisionRuntime` **保留**（永留内核）。

**P2 改写点**：

| 文件 | 改动 |
|---|---|
| `omni_core/tools/__init__.py` | 删 filesystem/shell/web 三组 import 与 `__all__` 条目 |
| `omni_core/local/loop/core.py` | import 块删 `configure_shell` / `configure_filesystem` / `configure_web` 三项；`__init__` 内删除（实测 `core.py:178-180`）三行调用（语义由插件 `startup` 承接） |
| `filesystem_tool.py` / `shell_tool.py` / `web_tool.py` → `plugins/{filesystem,shell,web}/plugin.py` | 函数体零改动；新增 manifest；新增 `startup(ctx)`：读 `ctx.config["runtime"]` 旧键（`filesystem` / `shell_exec` / `web`）执行原 `configure` 逻辑 |

配置兼容（零破坏）：不引入 `runtime.plugins.<name>` 键；用户 `~/.omniagent/config.yaml` 一字不改。`ToolPlugin.source` 不动（插件工具仍标 `builtin`，与 `mcp` 的区分语义不变）。旧路径不留兼容 re-export。

> ⚠️ **本机复验修正（与计划原稿不符，务必一并处理）**：原稿称「filesystem/shell/web 无任何测试/backend/brain 直接 import 符号，tests 全走 `call_tool` 按名」——**该声明在当前基线不成立**。实测 `tests/test_f4_1_instructions.py:18` 有 `from omni_core.tools import filesystem_tool as fs`，并在 `test_agents_md_write_blocked` / `test_agents_md_write_guard_covers_nested_name` 中直接调 `fs.write_file` / `fs.edit_file`。因 P2 明确「旧路径不留兼容 re-export」，该文件必须同步改（改经 `plugin_module("filesystem")` 取模块，并确保插件已 `load_plugins`），否则 P2 必挂。（`shell` / `web` 确实无测试直接 import，此点原稿正确。）

**P3 改写点**：

| 文件 | 改动 |
|---|---|
| `device_tool.py` → `plugins/device/plugin.py` | 保留模块级 `bind_execution_module(em)`（测试注入用）；新增 `startup(ctx)`：`bind_execution_module(ctx.execution)`（`None` 跳过并 log） |
| `vision_tool.py` → `plugins/vision/plugin.py` | 保留 `bind_vision_runtime(vr)`；新增 `startup(ctx)`：`ctx.vision` 非空 → bind；`None` → 自注销 `vision_describe` / `som_ground` / `som_marks` 并 log（对齐现 `core.py:156-165` 行为） |
| `omni_core/local/loop/core.py` | 删 `bind_execution_module` import/调用（实测 `core.py:126`）；删 vision 分支的 `bind_vision_runtime` 延迟 import/调用（实测 `core.py:154-155`）与关闭时三工具 `unregister` 循环（实测 `core.py:159-160`）（语义移入插件 `startup`） |
| `omni_core/brain/sdk_agent.py` | 删 `vision_tool` import（实测 `sdk_agent.py:18`；`bind_vision_runtime` 本就未用）；`vision_describe` / `som_ground` / `som_marks` / `tap_by_mark` 改经 `TOOL_REGISTRY[name].tool` 获取，装配语义不变 |
| `omni_core/tools/__init__.py` | 删 device/vision 两组 import 及 `__all__` 条目 |
| 测试 | `tests/test_execution_backend.py`、`test_m3_tools.py`、`test_m0_tool_plugin.py`、`test_m1_sdk_agent.py`、`test_m2p5.py` 的 device/vision 直接 import 改经 `from omni_core.tools.loader import plugin_module` + `plugin_module("device"/"vision").bind_*(...)` |
| `omni_core/tools/loader.py` | 唯一增量：公开 `plugin_module(name)`（读 `_loaded_modules`） |

> ⚠️ **本机复验修正（P3 测试清单遗漏 2 处，务必一并处理）**：
> 1. **`tests/test_vision_som.py` 未在原稿清单内**，但实测 `test_vision_som.py:24` 有 `from omni_core.tools import vision_tool`、`:152` 调 `vision_tool.bind_vision_runtime(vr)` → 同样要改。
> 2. **`tests/test_m3_tools.py:227-233` 是源码文本断言**：它把 `omni_core/local/loop/*.py` 拼成 `src` 后断言 `"bind_execution_module" in src`（本意＝「内核只注入运行时、不按工具名分支」）。P3 删掉 `core.py:126` 后该断言**必然失败**，需改写为等价意图的新断言（例如改断 `plugin_module("device")` 路径或删该行并保留 `"build_plugin_registry" in src`），**不要**为了让断言通过而在内核里保留该字符串。
> 3. 原稿标题写「测试（4 文件）」但实际列了 5 个文件，另有上述 1 处遗漏 → 去重后 P3 实为 **6 个测试文件**：`test_execution_backend.py`、`test_m3_tools.py`、`test_m0_tool_plugin.py`、`test_m1_sdk_agent.py`、`test_m2p5.py`、`test_vision_som.py`。

时序保证：`load_plugins` 在 ToolLoop 装配中位于 vision/execution 就绪之后（P1 已定），插件 `startup(ctx)` 拿到的句柄已就绪。

## 2.2 执行顺序与验收

**P2 步骤**：①死 import 清理 → ②三插件目录创建 → ③`__init__.py`/`core.py` 改写 → ④`tests/test_f4_1_instructions.py` 改写 → ⑤验收。

```
PY -m pytest tests\test_plugin_loader.py tests\test_m3_tools.py tests\test_m3_python.py tests\test_u2_tool_disable.py tests\test_api_integration.py tests\test_m0_tool_plugin.py tests\test_f4_1_instructions.py -q
PY -c "import omni_core.tools as t; assert not hasattr(t,'read_file') and not hasattr(t,'shell_exec') and not hasattr(t,'web_fetch'); print('symbols gone')"
PY -c "from omni_core.tools.loader import load_plugins, last_report; load_plugins({}); r=last_report(); assert {'filesystem','shell','web','fs_pro'} <= set(r.loaded), r.loaded; print('migrated:', r.loaded)"
```

标准：失败 ⊆ 基线集合；后两条输出正常；`git status` 改动恰为移动 3 模块 + 改 3 类文件（+1 测试文件）+ 删 6 处死 import。

**P3 步骤**：①loader 增 `plugin_module` → ②device/vision 插件创建 → ③`core.py`/`sdk_agent.py`/`__init__.py` 改写 → ④测试 import 改写（6 个文件，含 `test_vision_som.py` 与 `test_m3_tools.py` 源码断言）→ ⑤验收。

```
PY -m pytest tests\ -q --ignore=tests\test_model_hub.py
PY -c "import omni_core.tools as t; assert not hasattr(t,'bind_execution_module') and not hasattr(t,'press'); print('symbols gone')"
PY -c "from omni_core.tools.loader import load_plugins, last_report; load_plugins({}); r=last_report(); assert {'device','vision'} <= set(r.loaded); print('ok')"
```

标准：全量测试失败 ⊆ 基线；其余同 P2。

---

# 附：本机复验记录（2026-09-24，只读核对，未改任何代码）

| 项 | 结果 |
|---|---|
| `git rev-parse HEAD` / `github/main` | 均为 `ff3c800`；原稿基线 `444b710` → `git cat-file -t` = **Not a valid object name**（本仓库不存在） |
| `git stash list` | 空（原稿所述 `stash p1-wip-tsd-blocked` 不在本仓库） |
| `temp_venv` / `temp_venv_broken_tsd` / `plugins/` / `omni_core/tools/loader.py` / `C:\Users\v_leishen` | 全部 **不存在**；`.venv` 存在 |
| `PY` 体检 | Python 3.12.8；`pytest`/`yaml`/`httpx`/`fastapi` OK；`openai-agents 0.22.2` |
| `omni_core/tools/__init__.py` 硬编码 import 清单 | 确认（base / vision / device / python / shell / filesystem / web / mcp_servers / skill / local_model） |
| `omni_core/local/loop/{emitter,finish,graph_runner,instructions,parts,sdk_bridge}.py` 的 `from omni_core.tools import (...)` | 确认存在且**正文零使用**（死 import） |
| `core.py` 生命周期调用点 | `126` `bind_execution_module`；`154-155` vision bind（延迟 import）；`159-160` vision 关闭时 `unregister` 三工具；`177-180` `configure_python/shell/filesystem/web`；`188` `self.mcp_servers` |
| `sdk_agent.py:18` | 确认 `from omni_core.tools.vision_tool import (...)` |
| filesystem/shell/web 的直接 import 面 | **`tests/test_f4_1_instructions.py:18` 直接 import `filesystem_tool`**（原稿声明不成立，见 §2.1 修正）；shell/web 无直接 import |
| device/vision 的直接 import 面 | `tests/test_execution_backend.py`、`test_m3_tools.py`、`test_m0_tool_plugin.py`、`test_m1_sdk_agent.py`、`test_m2p5.py`、**`test_vision_som.py`**（原稿遗漏）、`omni_core/brain/sdk_agent.py` |
| `tests/test_m3_tools.py:227-233` | 源码文本断言 `"bind_execution_module" in src`（P3 必挂，见 §2.1 修正） |

---

# 实施修正（执行期对原设计的契约调整）

以下 6 条是在实施 P1-P3 时发现的**设计必需修正**（不改会让契约自相矛盾或破坏 C-P4），已落入代码：

1. **`startup(ctx)` 每次装载都跑，不是只跑一次**（修正 §0.5 / §1.4 的"startup 只跑一次"假设）。
   已装载的包在后续每次 `load_plugins` 中仍会收到新的 `startup(ctx)`，只跳过 import 与注册。
   原因：`bind_*` 类钩子必须跟随**每个 ToolLoop 实例**刷新——每个实例持有自己的
   `ExecutionModule` / `VisionRuntime`，"只跑一次"会让第 2 轮仍绑在上一轮对象上（跨任务串味）。

2. **`PluginContext` 增 `wired: bool`**（补充 §0.4.4）。
   `True` = 真实装配（ToolLoop，句柄已就绪）；`False` = 只读枚举等旁路（`GET /api/runtime/tools`、测试）。
   插件**不得**在 `wired=False` 时做破坏性动作（如注销工具）——全局 `TOOL_REGISTRY` 是进程级状态，
   一旦被抹掉，幂等装载不会补回来。

3. **插件对自己声明的工具负责（"谁注册谁维护"）**（补充 §2.1 的 vision 插件）。
   vision 关闭时会注销三个依赖 VLM 的工具；若无人恢复，一次关闭会让 vision 永久消失。
   故 vision 插件在每次 `startup` 开头做 `_ensure_registered()`：工具不在册即在**既有模块命名空间**内
   重跑模块体（模块级装饰器重新登记）。⚠️ 实现约束：插件是按文件路径动态装载的，
   **不能用 `importlib.reload`**（按名 find_spec 会失败：`ModuleNotFoundError: spec not found`），
   必须 `spec_from_file_location(...).loader.exec_module(module)`。

4. **接线从 4 处变 5 处**（修正 §1.6）：`PATCH /api/runtime/tools/disabled` 的**工具名校验前**
   也要幂等 `load_plugins(cfg)`。否则工具迁出后，合法的插件工具名会被判「非法工具名」返回 400，
   直接破坏 C-P4（视图/接口契约不因迁出而变）。

5. **内核保留 `vision_disabled` 前端事件**（kernel 侧只留陈述性文案，不含工具名）。
   注销动作移入插件后，若一并删掉该事件会丢前端右栏提示；保留文案即可，不违反「内核零工具名分支」。

6. **`plugin_module(name)` 在 P1 即提供**（原计划列为 P3 增量）。测试注入需要它，提前实现无副作用。

## 测试面必改清单（超出原稿 §2.1 的声明）

| 文件 | 原因 |
|---|---|
| `tests/test_f4_1_instructions.py` | 直接 `from omni_core.tools import filesystem_tool as fs`（P2 起改经 `plugin_module("filesystem")`） |
| `tests/test_o2_encoding.py` | 按名 `call_tool("shell_exec", ...)`（P2 起需先装载插件） |
| `tests/test_vision_som.py` | 直接 `from omni_core.tools import vision_tool`（P3） |
| `tests/test_m3_tools.py:227-233` | 源码文本断言 `"bind_execution_module"` → 改为断 `load_plugins` |

## 验收口径与已知失败集

- 口径：**改动后全量测试失败集 ⊆ 改动前失败集**（`pytest tests\ --ignore=tests\test_model_hub.py`）。
- 改动前失败集（9）：`test_m4d_sdk_transport` / `test_resolve` / `test_states`（真实存量缺陷）
  + 6 个「vision 关闭后未恢复」造成的顺序依赖失败（`test_api_integration` som_marks、`test_m0_tool_plugin` ×4、`test_m2p5`）。
- 改动后失败集（3）：只剩上述三个真实存量缺陷；顺序依赖族因修正 3 一并消失。
- `test_model_hub.py` 按原计划排除（依赖 model_hub 运行时）。
