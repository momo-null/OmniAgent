# MCP 按需激活（lazy-activation）设计方案

> 现状：MCP 只要 `enabled=true` 就在启动期连接，之后**每个 run 的每次模型请求**都携带
> 该 server 的全部工具 schema；某内容平台（xhs）的 MCP 则完全绕开这套机制，走插件里手写的
> JSON-RPC 客户端 + 3 个常驻 `@function_tool`，登录态判断还要真发一次 RPC（20s 超时）。
>
> 本文把"MCP 何时连接、何时进上下文"从**配置期**改为**模型决定**，并把 xhs 的双轨
> 收敛到同一条链路。全文结论基于当前代码与 SDK 源码，不含推测；需要实测才能定的点
> 单独标注「待实测」。

## 1. 现状：链路事实

### 1.1 连接链路（三处入口）

```
服务启动
  └─ loop_pool.prime_mcp()                  pool.py:110  后台线程 connect 全部 enabled server
       └─ build_mcp_servers(mcp.json)       mcp_servers.py:115  只构造对象，不连接
       └─ _ensure_mcp_connected([...])      sdk_loop.py:1453    connect + list_tools 探活
ToolLoop.__init__（每 run 新建）
  └─ loop_pool.mcp_servers(cfg)             pool.py:78   按配置指纹复用**已连接**实例
  └─ self.mcp_servers = [...]               core.py:174
子任务 run
  └─ _assemble_agent_stack()
       └─ _ensure_mcp_connected(...)        sdk_loop.py:1639
       └─ Agent(mcp_servers=_mcp_connected) sdk_loop.py:1666/1675
```

### 1.2 注入链路（真正的成本所在）

```
Runner.run(agent, items, max_turns=块大小)   sdk_loop.py:1883
  └─ Agent.get_mcp_tools()                   agents/agent.py:250（每次 run 都调）
       └─ MCPUtil.get_all_function_tools()   agents/mcp/util.py:266
            └─ 每个 server: list_tools()     agents/mcp/server.py:1431 → _apply_tool_filter
            └─ 每条 MCP tool → FunctionTool → 放进本次请求的 tools 字段
```

三个可直接读出的结论：

1. **注入 = 连接之后永久生效**。`agent.mcp_servers` 里有的 server，每个 run 都被枚举成
   function tools，**与这一轮是否需要它无关**。这是"开了就注入"的字面机制。
2. **每个块都会再枚举一次**。`get_mcp_tools()` 在每次 `Runner.run` 前调用，`list_tools()`
   在 `cache_tools_list=False`（项目当前构造未开启）时是真实 round-trip。也就是说，
   不用 MCP 的轮次也在付「一次 RTT + 全量 schema」。
3. **`tool_filter` 只减 schema，不减连接与枚举**。它是 `list_tools()` 之后的过滤器
   （`server.py:1002`），server 仍然要被 connect、被枚举，未连接时直接抛
   `Server not initialized`（`server.py:1437`）。所以现有 `tool_filter` 不能承担懒加载。

### 1.3 xhs 为什么是双轨

| 维度 | 项目 MCP 通道 | 某内容平台现状 |
|---|---|---|
| 传输 | `MCPServerStreamableHttp`（SDK） | `XhsAdapter._mcp_rpc`（`requests` 手写 JSON-RPC，`xiaohongshu.py:185`） |
| 配置 | `~/.omniagent/mcp.json` | `~/.omniagent/channels/xiaohongshu.yaml` |
| 工具暴露 | 连接后全量注入 | 3 个 `@function_tool` 常驻 registry（`plugin.py:100/123/150`） |
| 登录态 | 无此概念 | `_login_status()` = initialize + `tools/call check_login_status`，20s 超时、5 分钟内存缓存（`xiaohongshu.py:263`） |

实测配置核对（2026-11-13）：

- `~/.omniagent/mcp.json` 里没有该条目 → **它确实没进项目 MCP**。
- `~/.omniagent/channels/<channel>.yaml`：`enabled: true`、`mcp_url` 指向同一端点（具体部署细节不入库）。
- 可执行文件与 `cookies.json` 装在本机工具目录下（具体路径不入库）。

至于"没登录没法判断"：**判断登录 = 必须连上服务 + 调一个工具**，这是协议决定的，绕不开。
能做到的是把这件事**变得便宜、可缓存、可读**（端口 → 协议 → 登录 三级探活，见 §6.2），
而不是让它阻塞 20s 或被当成异常。

### 1.4 四个病灶（按代价排序）

| # | 病灶 | 代价 | 位置 |
|---|---|---|---|
| P1 | 工具 schema 每轮全量注入 | 长会话累积 token；xhs 约 450 token/轮起 | `sdk_loop.py:1666` |
| P2 | 每块一次 `list_tools` RTT | 本地 HTTP 5–15ms / stdio 30–60ms，**每块** | `agent.py:250` |
| P3 | `prime_mcp` 启动即 connect | 服务没起时启动期空等，甚至拖慢首轮 | `pool.py:110` |
| P4 | 设置页枚举也会真连接 | 打开设置页就付连接成本 | `tools_api.py:60` |

## 2. 目标与非目标

**目标**

- G1 与 MCP 无关的 run：**零连接、零 schema**。请求 tools 字段里不出现任何该 server 的工具。
- G2 需要时一次调用即可用：模型调 `mcp_activate(name)` 后，**下一块**的工具清单里出现该
  server 的工具，且只出现与本任务相关的子集。
- G3 状态可读：未启动 / 未登录 / 半死 是**结构化返回值**，带恢复动作，不是异常、不阻塞。
- G4 xhs 收敛为 mcp.json 的一条 `mode: lazy` server；写路径（发布/评论）**继续只走网关
  四道闸**，不进模型工具面。
- G5 逐字节可回退：`mode: eager` 或缺省配置时行为与今天完全一致。

**非目标**

- 不做 MCP 工具级审批（S2 收口是另一条线；本方案只改"何时暴露"）。
- 不改写路径纪律（`mcp_servers.py:36` 的"过渡期只接只读 server"仍然成立）。
- 不依赖 `defer_loading`：SDK 有这个字段（`tool.py:517`），但语义是
  "Responses API 的 tool search 隐藏工具定义"，本项目走 chat/completions 兼容端点，
  多数模型不支持 —— 只在 OpenAI 原生 Responses 端点可作为替代路径，不作为主方案。
- 不重构插件系统（xhs 三件套的最终形态见 §7.3，P0 不做）。

## 3. 架构总览

```
                       ┌──────────── L0 目录（零 IO，常驻）────────────┐
mcp.json ──静态元数据──> │ <available_mcp_servers>                      │
mcp_state.json ──缓存──> │ - demo-serv: 示例只读服务…｜未登录（需扫码）  │
                       │ - example-analysis: 示例数据分析…｜未启动    │
                       └───────────────────┬──────────────────────────┘
                                           │ 模型判断需要
                       ┌───────────────────▼──────────────────────────┐
                       │ L1 mcp_activate(name, tools?)                │
                       │   端口(0.3s) → connect+list(1.5s) → 登录(2s)  │
                       │   成功: agent.mcp_servers.append(srv)         │
                       │        + allowed[name] = 子集                 │
                       │   失败: {state, reason, hint}（不抛、不重试） │
                       └───────────────────┬──────────────────────────┘
                                           │ 下一块 Runner.run 自动枚举
                       ┌───────────────────▼──────────────────────────┐
                       │ L2 派发                                      │
                       │   A) 工具直调（activate 已注入真实工具）      │
                       │   B) mcp_call(name, tool, args) 通用兜底      │
                       └──────────────────────────────────────────────┘
```

**状态机**（每个 server 一份，落 `~/.omniagent/mcp_state.json`）

```
        ┌─── port 不通 ──────────────► offline  ── 提示: 启动服务 ──┐
unknown ┤                                                          │
        └─── port 通 ─► initialize+list_tools ─┬─ 成功 ─► probe 登录 ─┬─ true  ─► ready
                                               │                      └─ false ─► unready
                                               └─ 失败 ─► degraded（服务半死）
```

- `ready` / `unready` / `offline` / `degraded` 四态都是**可用信息**，不是错误。
- 每次状态带 `ts` 与 `latency_ms`；TTL（`readiness.ttl_sec`，缺省 600s）内重复 activate
  直接读缓存并返回。
- 与 `_MCP_ABANDONED` 解耦：lazy server **永不进程级放弃**，只做 TTL 退避；一次没登录
  不能导致"这一整个会话都不再用它"。

### 3.1 三层语义（澄清一个常见误解）

"给 MCP 加一个 lazy 状态"容易混成一件事，实际是三件，必须分开：

| 层 | 是什么 | 谁写 | 何时读 |
|---|---|---|---|
| **配置** `mode: eager\|lazy` | 静态声明：这个 server 不预连接、不预注入 | 人（mcp.json） | 构造 Agent 时 |
| **观测** `state: offline\|degraded\|unready\|ready` | 一次探活的结果 + `ts` + `latency_ms` | 只有 `probe()`（activate / 设置页 refresh / 网关 `ready()`） | 目录渲染、activate 快判 |
| **承诺** | **没有承诺层** | — | — |

**关键取舍：目录不承诺"能连上"。** 要让上下文保证"这个服务可用"，就必须在注入目录前
把它连一遍——那正是现状的毛病（启动即连接），只是把成本从 run 挪到了目录构建，没有消除。
所以目录只给三样东西：**存在什么服务、干什么用、上次探到的状态（带新鲜度）**；
"这一次到底能不能用"由 `mcp_activate` 当场裁定，返回值为准。

由此推出两条纪律：

1. **目录是提示，activate 是事实。** 某个服务的 cookie 过期后，目录可能还显示 `ready`
   （10 分钟前探的）；activate 会当场翻成 `unready` + 扫码提示。模型必须以 activate
   返回值为准，不得拿目录状态当结论对外汇报。
2. **`state` 不塞进 SDK 的 `MCPServer` 对象。** 那对象是 per-run 池化实例，跨 run、跨进程
   不可见；而设置页、网关 `ready()`、目录注入都要读同一份。所以状态按 server 名落
   `mcp_state.json`，SDK 对象保持原样（未连接 = `session is None`），lazy 只是我们这层的
   调度策略，不改 SDK 的任何字段。

一句话：**`lazy` 是"不要提前连"的声明，`state` 是"上次连过怎么样"的观测，
`activate` 是"这一秒到底行不行"的裁决。**

## 4. 关键技术依据（实施前已核对）

| 结论 | 依据 |
|---|---|
| 每次 `Runner.run` 都重新枚举 MCP 工具 | `agents/agent.py:250` `get_mcp_tools()` → `MCPUtil.get_all_function_tools`（`mcp/util.py:266`） |
| `agent.mcp_servers` 是可变 list，run 时读取 | `agents/agent.py:197` 字段 + `get_mcp_tools` 读 `self.mcp_servers` |
| `tool_filter` 支持 callable，每次 `list_tools` 都应用 | `mcp/server.py:1002 _apply_tool_filter`，`ToolFilterCallable` 已在项目使用（`mcp_servers.py:93`） |
| `list_tools` 可缓存并可失效 | `mcp/server.py:953 cache_tools_list` + `:1102 invalidate_tools_cache()` |
| `call_tool` 可直接派发（不经过 tool schema） | `mcp/server.py:628` 抽象 `call_tool` |
| 未连接时 `list_tools` 抛 `Server not initialized` | `mcp/server.py:1437` |

→ 结论：**把 lazy server 从 `agent.mcp_servers` 里拿掉、按需 append，就能实现"下一块才注入"，
不需要重建 Agent，也不需要改 SDK。**

## 5. 配置协议（mcp.json 增量）

```jsonc
{
  "enabled": true,
  "catalog": { "inject": true, "max_items": 12, "desc_limit": 60 },
  "servers": [
    {
      "name": "example-svc",
      "enabled": true,
      "mode": "lazy",                       // lazy | eager（缺省 eager，见下方纪律）
      "desc": "示例只读服务：列表 / 搜索 / 详情（含评论）",
      "url": "http://127.0.0.1:18080/mcp",
      "port": 18080,                        // 可选；缺省从 url 解析，用于 0.3s 端口探活
      "timeout_ms": 8000,                   // 保留原语义（SDK client_session_timeout）
      "lifetime": "run",                    // run（run 结束可 disconnect）| process（常驻保活）
      "readiness": {
        "probe": "check_login_status",      // 登录态探针（只读工具）
        "extract": "logged_in",             // 从返回 JSON 取的字段
        "expect": true,
        "hint": "未登录：先在服务端浏览器里扫码（scripts\\<svc>_login.bat）",
        "ttl_sec": 600
      },
      "tool_filter": {
        "include": ["check_login_status", "list_items", "search", "get_detail"]
      },
      "transform": {                        // 可选：结果归一钩子（见 §7.3）
        "list_items": "items",
        "search": "items",
        "get_detail": "detail"
      }
    },
    {
      "name": "example-analysis",
      "enabled": false,
      "mode": "eager",
      "command": "python",
      "args": ["<repo>/system/mcp_server.py"]
    }
  ]
}
```

字段纪律：

- `mode` **缺省 eager**，不是 lazy。这是落地时对初版方案（"宁可显式 eager，不要默认全量"）
  的修正：现网 mcp.json 的条目大多没写 `mode`，缺省 lazy 会让它们突然"不连接、不注入"，
  模型必须显式 activate 才能用——**升级静默关掉了已配好的能力**，比多带几 K token 危险得多。
  懒加载必须是显式声明，不靠缺省推断。
- `tool_filter.include` 是**能力白名单**，不是性能开关；懒加载的 server 也必须显式声明，
  否则 activate 会把 server 的全部工具（含写类）注入模型面 —— 与 §2 非目标冲突。
- 敏感/写类工具**不进 mcp.json**，继续留在网关通道做四道闸（发布/评论这类写路径属此类）。

## 6. 组件设计

### 6.1 目录构建与注入（L0）

新增 `omni_core/tools/mcp_state.py::build_catalog_message(cfg, state) -> str`，形态对齐
已有的技能目录（`knowledge_inject.py:309 format_skill_catalog_message`），注入点紧随
`skill_catalog` 之后（`sdk_loop.py:1831-1834` 同一位置），**不进 system prompt**。

```
<available_mcp_servers>
- `example-svc`: 示例只读服务：列表 / 搜索 / 详情｜未登录（需扫码）
- `example-analysis`: 示例数据集查询｜未启动
以上为外部 MCP 服务目录（仅摘要，工具尚未加载）。需要时调用 mcp_activate(name) 连接并按需
加载工具；先看目录即可，不要假设服务可用。
</available_mcp_servers>
```

- 目录**只读配置 + 上次探活缓存**，零连接、零 RTT。
- 单个 server 上限 `desc_limit`（默认 60 字），整体上限 `max_items`（默认 12）。
- `catalog.inject=false` → 完全不注入（纯 `mcp_catalog` 工具拉取模式，适合 server 很多时）。

**注入时机与方式（照抄技能目录那套，不另造机制）**

| 项 | 技能目录（现状） | MCP 目录（本方案） |
|---|---|---|
| 构建点 | `sdk_bridge.py:281-294`，`build_run()` 内、组装请求之前 | 同位置追加一次 `build_catalog_message()` |
| 载体 | 一条 **user 消息**（不是 system） | 同 |
| 位置 | 历史之后、本轮 `user_input` 之前（`sdk_loop.py:1831`） | 紧跟 `skill_catalog` 之后 |
| 频次 | 每个子任务 run **一次**；块间由 `to_input_list()` 继承 | 同 |
| 开关 | `mode == "chat"` 不注入（`sdk_bridge.py:281`） | 同（闲聊不塞能力目录，避免稀释人格） |
| 抗压缩 | 起始 items 会被 `_CompactionModel` 切掉，靠 `context_note` 重贴（`sdk_loop.py:565-571`） | 新增 `mcp_catalog` 参数，压缩/硬滑窗后重贴（与 `_reapply_context_note` 同款） |
| 可观测 | `_log_knowledge_injection(kind=...)` | 新增 `kind="mcp_catalog_injected"`，含 server 数 / 字符数 / 各 server state |

**为什么必须是 user 消息、不能进 system**：system 的硬约束是"逐字节稳定 → 前缀缓存命中"
（`sdk_bridge.py:297-300`）；目录含 `state`（随探活变化），进 system 会让每次状态变化击穿
整段缓存。且"每步重发"被明令禁止（`sdk_loop.py:1827-1828`：反复 priming 会抬高生成退化
概率），所以目录只能在起始 items 注入一次。

**为什么不能每块重生成**：`res.to_input_list()` 会把上一块的 items 原样带回；每块重贴会让
同一段文本以 N 条 user 消息累积，既吃 token 又构成重复 priming。需要"目录更新"时，走
`mcp_catalog` 工具的返回值，而不是重贴消息。

### 6.2 三级探活（L1 的前半段）

```python
# omni_core/tools/mcp_state.py（落地签名）
def probe(name, cfg, server, *, force=False,
          budget_s=DEFAULT_BUDGET_S,
          with_readiness=True) -> ServerState

# L0 端口（0.3s，无协议）
socket.create_connection((host, port), timeout=0.3)
# L1 连接 + list_tools（1.5s，预算内）
server.connect() → mcp_state.list_tools(server)      # 不能裸调，见下
# L2 登录/就绪探针——**激活路径默认不跑**（with_readiness=False）
server.call_tool(readiness["probe"], {}) → 按 extract/expect 或文本口径判定
```

**`list_tools` 必须走统一入口**：只要 server 绑定了 callable 过滤器（我们的
include / 子集过滤都是 callable），SDK 在缺 `run_context`/`agent` 时直接抛
`UserError("run_context and agent are required for dynamic tool filtering")`
（`agents/mcp/server.py:1019`）。Runner 内调用自带上下文，所以平时不暴露；
探活、设置页枚举这类"上下文外调用"必须由 `mcp_state.list_tools` 补一个
`RunContextWrapper(context=None)` + 占位 agent。这是落地时才暴露的坑：
裸调在真实服务上直接 `degraded/UserError`。

预算纪律：

| 级 | 超时 | 失败语义 |
|---|---|---|
| 端口 | 0.3s | `offline`（服务没起）→ hint 指向启动脚本 |
| connect+list | 1.5s | `degraded`（端口通但协议不应答/半死） |
| readiness | **默认不执行**；显式 `probe_readiness_on_activate: true` 才跑，且 `probe_budget_s` 需 ≥12s | `unready`（活着但没登录）→ hint 指向扫码 |

**总预算 `DEFAULT_BUDGET_S = 2.0`**：`mcp_activate` 是工具调用，不能复用
`_MCP_CONNECT_BUDGET_SEC=8.0`（`sdk_loop.py:1408`，那是"首次大脑调用前"的预算），
否则用户会看到"工具调用卡 8 秒"。就绪探针不计入这个预算——见下一段的实测。

#### 实测（2026-11-13，某本地 url 型服务 @ 127.0.0.1:18080）

| 步骤 | 实测 |
|---|---|
| 端口 + connect | 125ms |
| `list_tools`（include 过滤后 4 个只读工具） | 合计 **375ms** → `ready` |
| `check_login_status` | **11093ms**，返回纯文本 `✅ 已登录 / 用户名: <示例用户>` |

两个结论，都改变了初版方案：

1. **登录探针进不了激活同步路径**。11s 放进 `mcp_activate` 等于让工具调用卡死，
   2.0s 预算下必然 `degraded/TimeoutError`（首版实测就是这个结果）。所以
   `probe_readiness_on_activate` **缺省 false**：激活只裁"端口 + 协议 + 工具清单"。
2. **它返回的是人话，不是 JSON**。`extract: "logged_in"` 取不到字段；
   初版 `_readiness_ok` 只认 `logged_in`/`true`，会把"✅ 已登录"判成未登录。
   已补中文/英文文本口径（`已登录` / `未登录` / `logged_in` / `not logged`…），
   并且**字段缺失不再直接判死**，继续走文本口径。

那"没登录"怎么判断？**在真正调用时回写**：`mcp_call` 拿到 `isError` 且文本像
"未登录/请先登录/login required"时，调 `mcp_state.record_call_failure` 把该 server
记成 `unready`（带扫码 hint），下次目录/激活快判；本次返回语义不变，模型仍拿到原始
错误。代价是"第一次用到才知道"，收益是激活路径稳定在几百毫秒。

磁盘状态文件：

```json
{"example-svc": {"state": "unready", "ready": false, "reason": "not_logged_in",
                 "ts": 1763000000.0, "latency_ms": 118, "tools": ["list_feeds", "..."]}}
```

写盘用"写临时文件 + replace"，损坏时按空处理（与 `load_state` 同款宽容策略）。

### 6.3 元工具（unit="mcp"，source="mcp"）

放在新文件 `omni_core/tools/mcp_meta.py`，由 `build_meta_tools` 在
`tools_enabled=True` 时追加（`sdk_loop.py:1640`）。

```python
mcp_catalog(refresh: bool = False) -> dict
# 返回 [{name, desc, state, ready, reason, hint, tools, activated, last_probe_ago_sec}]
# refresh=False 只读缓存；refresh=True 对**当前 run 已激活/曾失败的** server 重探

mcp_activate(server: str, tools: list[str] | None = None) -> dict
# 1) 端口 → connect+list → readiness（预算 2.0s）
# 2) 成功：srv.tool_filter 的 allowed 集合置为 tools 子集（None=include 白名单）
#          agent.mcp_servers.append(srv)          ← 下一块 Runner.run 生效
#          srv.invalidate_tools_cache()
# 3) 返回 {ok, state, activated, tools:[{name, desc, required:[{name,type}]}]}
#    失败返回 {ok:false, state, reason, hint, retry_after_sec}，**不抛异常**

mcp_call(server: str, tool: str, arguments: object | None = None) -> dict
# 未激活则内部走一次 activate（同样 2.0s 预算）；随后 call_tool
# 返回 {ok, content, structured, normalized?}；错误归一成 {ok:false, state, reason, hint}

mcp_deactivate(server: str) -> dict
# 从 agent.mcp_servers 移除 + 按 lifetime=run 断开（可选，P2）
```

返回 schema 的精简策略：只给 `name / desc(≤140 字) / required 参数名+类型`，
**不回传完整 JSON Schema**（那是激活后 SDK 自己注入的，重复回传纯浪费 token）。

### 6.4 注入实现（关键，改动量最小）

```python
# sdk_loop.py 新增
class _McpSession:
    """一个子任务内的 MCP 激活状态（per-run，不进池，不跨 run 共享）。"""
    def __init__(self, cfgs: list[dict], servers: list[MCPServer], agent_holder: dict):
        self.cfgs, self.servers = cfgs, servers
        self.agent = agent_holder              # Agent 构造后回填
        self.armed: set[str] = set()
        self.allowed: dict[str, set[str] | None] = {}
        self._lock = threading.RLock()

    def filter_for(self, name: str) -> Callable:
        def _f(ctx, tool):
            allow = self.allowed.get(name)
            return True if allow is None else str(getattr(tool, "name", "")) in allow
        return _f
```

接线：

```python
# _assemble_agent_stack：lazy server 不进 Agent
_eager = [s for s in (mcp_servers or []) if _is_eager(s)]
_mcp_session = _McpSession(_lazy_cfgs, _lazy_servers, holder={})
_agent_tools = list(tools) + build_meta_tools(..., mcp_session=_mcp_session)
agent = Agent(..., mcp_servers=_mcp_connected_eager_only)
_mcp_session.agent = agent                # 只读引用，activate 时 append
```

- `lazy` 的 server 对象在 `ToolLoop.__init__` 就构造好（`build_mcp_servers` 本来就不连接，
  零成本），但**不 connect、不 append**。
- `activate` 成功 → `agent.mcp_servers.append(srv)`。下一次 `Runner.run`（下一个块）
  由 `get_mcp_tools()` 自然纳入。**不重建 Agent、不改 `while` 循环结构。**
- `tool_filter` 用 §6.4 的动态闭包，`activate` 里设置 `allowed[name]`；因为
  `_apply_tool_filter` 每次 `list_tools` 都跑，子集立即生效。
- 同一块内想连续调用，用 `mcp_call`（激活后直接 `call_tool`，不受"下一块"限制）。

**桥线程约束（实施第一步必须验证）**

`run_async` 在常驻桥线程上会主动抛错（`async_bridge.py:70-82`）。而 SDK 工具可能在桥 loop
上执行，所以 `mcp_activate` 里**不能**直接 `run_async(srv.connect())`。三条候选，按验证结果选：

1. 元工具声明为 `async def`，内部 `await srv.connect()` / `await srv.list_tools()` —— SDK 原生
   支持 async handler，这是首选。
2. 若 `omni_core.tools.base.function_tool` 的 `wrap_tool` 包装链对 async 函数不友好：
   工具内只做"登记 pending"，由 `run_subtask_sdk` 在**块边界**（`sdk_loop.py:1854` 循环顶部，
   主线程）执行 connect + append，`mcp_activate` 立即返回 `{state:"connecting"}`。
3. 兜底：把连接放进一个专用工作线程（`asyncio.run` 独立 loop），代价是每次 activate 新建
   连接池 —— 只在 1、2 都不成立时用。

**验证脚本（5 行，实施前先跑）**：打一条 `async def` 的 `@function_tool`，打印
`threading.current_thread().name` 与 `inspect.iscoroutinefunction` 结果；同时打印 SDK 是否
把同步工具丢进了工作线程。一次运行即可定案。

### 6.5 连接回收

| lifetime | 行为 |
|---|---|
| `run` | run 结束（`run_subtask_sdk` 返回）调 `activate` 过的 server `cleanup()`；下个 run 重新连接（本地 HTTP 连接 ~10-30ms） |
| `process` | 留在 `loop_pool`，跨 run 复用（现状 `pool.py:78` 语义） |

`lifetime` 缺省 `run`：某些"偶尔用一次"的服务，进程里长挂一个常驻浏览器连接没有收益，
反而在服务重启后留下死 session。

## 7. 某内容平台（xhs）落地

### 7.1 mcp.json 段

见 §5 的第一条示例，要点三条：

1. `tool_filter.include` **只列只读工具**；`publish_content` / `post_comment_to_feed`
   不进 MCP（写路径仍由 `XhsAdapter.send_note/send_text` 的四道闸收口）。
2. `readiness.probe = check_login_status`：登录态成为"服务可用性"的最后一环，
   `hint` 直接给出扫码动作，模型可以原样转述给用户。
3. `lifetime: run`：不用就断，避免浏览器连接长期空挂。

### 7.2 登录态可判断性（回答原问题）

"没登录没法判断"的真实约束是：**判断必须连服务**。本方案的处置：

| 场景 | 现在 | 方案 |
|---|---|---|
| 服务没起 | `requests` 等满 20s 超时 | 0.3s 端口探测 → `offline` + "运行 scripts/<svc>_mcp.bat" |
| 服务起了没登录 | 20s 调 `check_login_status`，失败当错误 | 2.0s 探针 → `unready` + "扫码"（结果缓存 600s） |
| 首轮就想知道 | 不知道，除非真去连 | 目录里直接显示 `未登录（需扫码）`（读磁盘缓存，零 IO） |
| 网关 `ready()` | 20s 阻塞（`xiaohongshu.py:278`） | 复用同一份 `mcp_state` 缓存（≤5ms） |

`gateway/channels/xiaohongshu.py::_login_status` 改为：
`mcp_state.probe(cfg, level="readiness")` 的薄包装，缓存口径统一，**删掉对象内的
`_login_cache`**（ToolLoop 每轮新建，那个缓存本来就不跨 run）。

### 7.3 xhs 只读三件套的归位（分两档）

**P0（与懒加载同批，改动小）**：`plugins/xiaohongshu/plugin.py` 的三个 `@function_tool`
保留实现，但**不再注册进 registry**；改为在 mcp.json 有 `xiaohongshu` 时跳过注册
（`if mcp_state.has_lazy_server("xiaohongshu"): return`）。归一逻辑
（`_normalize_note/_extract_notes/_extract_comments`）抽到
`plugins/xiaohongshu/normalize.py`，由 `mcp_call` 的 `transform` 钩子调用 ——
**工具数从 3 个变 0 个，返回结构不变**。

**P1（可选，统一 Provider）**：把"插件 unit"与"MCP server"都抽象成 Provider，
目录统一列出、`mcp_activate` 接受 `plugin:xiaohongshu` 与 `mcp:xiaohongshu` 两种 id，
注入仍走 `tools` 列表（重建一次 Agent）。收益是彻底消除双轨，代价是要动 registry。
**P1 不做也能达成 G1–G5**，建议先落 P0 看实测收益。

## 8. 兼容与回退

| 开关 | 行为 |
|---|---|
| `mcp.enabled=false` | 与今天完全一致（无目录、无元工具、无连接） |
| 单 server `mode=eager`（缺省） | 与今天完全一致（启动 connect + 全量注入） |
| 单 server 显式 `mode=lazy` | 目录常驻 + 按需激活 |
| `mcp.catalog.inject=false` | 不注入目录，模型只能靠 `mcp_catalog` 主动拉 |

回退验收方式：拿现网 `mcp.json` 的拷贝跑一次 eager 对照，断言
`agent.mcp_servers` 数量、请求 `tools` 数量、`list_tools` 调用次数三者与改造前逐值相等。

## 9. 验收

**指标**

| # | 断言 | 观测点 |
|---|---|---|
| A1 | 无关 run：`len(agent.mcp_servers)==0`，请求 `tools` 无 `mcp_<srv>__*` | `_assemble_agent_stack` 打点 |
| A2 | 无关 run：`list_tools` 调用 0 次 | 在 `_list_tools_with_span` 外挂计数器（测试专用 monkeypatch） |
| A3 | 相关 run：目录含该 server；`mcp_activate` 后下一块 `tools` 含子集；新增 token < 600 | 轨迹 + usage |
| A4 | 服务未起：activate ≤ 400ms 返回 `offline`，同 run 第二次 activate 0 网络 | `latency_ms` |
| A5 | 未登录：真实调用返回错误文本后，`mcp_state.json` 记为 `unready` + `hint`（激活路径**不**跑 11s 探针） | `mcp_state.json` |
| A6 | eager 回退：与改造前逐值相等 | 对照脚本 |
| A7 | 真实 url 型服务（示例端口 18080）：`mcp_activate` ≤ 500ms 且拿到 4 个只读工具；目录状态为 `ready` | 实测 375ms ✓ |

**单测矩阵**（`tests/test_t43_mcp_lazy.py`，实到 18 项）

- 状态机五条边：port 拒 / port 通+init 失败 / init 通+login false / login true / 缓存 TTL 命中
- `mcp_state.json` 缺失 / 损坏 / 半写（临时文件残留）
- `mcp_activate` 幂等（同 run 调两次只 connect 一次）
- `mcp_call` 未激活自动激活；工具不存在 → `{ok:false, reason:"no_such_tool"}` 不抛
- `tool_filter` 子集：`allowed={list_feeds}` 时 `tools` 只含 1 个
- 目录渲染：`max_items` / `desc_limit` 截断；`state` 齐全
- eager 路径零回归：现有 `test_m3_mcp.py` / `test_t41_mcp_filter.py` / `test_t42_mcp_reconnect.py` 全绿
- 元工具不污染 `TOOL_REGISTRY`（它们是 per-run 装配物）

> SDK 侧"每次 `get_mcp_tools` 重新遍历 `agent.mcp_servers`"这条前提**不做** fake 测试：
> 要满足 SDK 私有协议（`_get_failure_error_function` 等），维护成本高于收益。该行为已直接
> 核对源码（`agents/agent.py:250` → `mcp/util.py:262`，无内部缓存），注入路径本身由
> `test_activate_injects_only_when_ready` 覆盖。

## 10. 实施顺序

| 阶段 | 内容 | 文件 | 估行 | 前置 |
|---|---|---|---|---|
| S0 | 验证桥线程与 async 工具（§6.4 那 5 行脚本） | — | — | 无 |
| S1 | `mcp_state.py`（状态机 + 三级探活 + 磁盘缓存） | 新 | ~180 | S0 |
| S2 | `mcp_servers.py` 解析 `mode/desc/readiness/lifetime` + `eager_subset()` | 改 | +60 | 无 |
| S3 | `mcp_meta.py` 三件套 + `build_meta_tools` 挂载 | 新/改 | ~200/+15 | S0,S1 |
| S4 | `_McpSession` 接线（agent.mcp_servers 按需 append + 动态 filter） | 改 | +50 | S3 |
| S5 | 目录注入（`build_catalog_message` + sdk_bridge 透传） | 新/改 | ~50/+5 | S1 |
| S6 | `prime_mcp` 只预热 eager；`_MCP_ABANDONED` 对 lazy 改 TTL | 改 | ~20 | S2 |
| S7 | xhs 接入（mcp.json 段 + `ready()` 复用缓存 + plugin 停止注册） | 改/配 | ~40 | S2,S4 |
| S8 | 设置页枚举改读缓存（不再真连接） | 改 | ~15 | S1 |
| S9 | 测试与 A1–A6 | 新 | ~200 | 全部 |

S1–S4 是主链，S7 是 xhs 的收口。S0 不过就换 §6.4 候选 2，**不影响其余设计**。

## 11. 未决问题

1. **`lifetime=run` 的断开时机**：`run_subtask_sdk` 有多个 return 分支（成功/预算耗尽/停止/异常），
   需要统一在 `finally` 里做；否则会漏断（连接泄漏）。倾向做成 `_McpSession.close()` 挂在
   `run_subtask_sdk` 的 `finally`。
2. **`cache_tools_list=True` 与工具集变更**：工具集静态的服务可开缓存；但 activate 子集时
   必须 `invalidate_tools_cache()`，否则会拿到上一份全量。测一遍即可。
3. **弱模型的两步成本**：`mcp_activate`→工具 是两步，弱模型可能"激活了不调"。
   缓解：目录里写明"激活后直接调用返回的工具名"；`mcp_activate` 返回里带
   `next: "直接调用 list_feeds(...)"` 的强提示。A3 指标里要看"激活后首次调用成功率"。
4. **服务_endpoint 是 stdio 时的端口探测**：stdio 型 server 没有端口，L0 端口探测要跳过
   （直接进 L1，预算放宽到 2.0s）。否则每次 activate 都白等 0.3s。

## 12. 实施记录（2026-11-13）

本次按 S1–S9 落地，未提交代码。落点：

| 文件 | 动作 | 内容 |
|---|---|---|
| `omni_core/tools/mcp_state.py` | 新 | 状态机 + 三级探活 + 磁盘缓存 + `list_tools` 统一入口 + 目录渲染 + `record_call_failure` |
| `omni_core/tools/mcp_meta.py` | 新 | `McpSession`（activate / call / catalog / close）+ 三个元工具 + transform 归一 |
| `omni_core/tools/mcp_servers.py` | 改 | `mode_of`（缺省 eager）/ `split_configs` / `build_mcp_servers(mode_filter, activation)` / 动态 filter 增加子集 |
| `omni_core/local/loop/pool.py` | 改 | 只池化 eager server（`prime_mcp` 因此只预热 eager） |
| `omni_core/local/loop/core.py` | 改 | 装配 lazy bundle；`_sdk_tools` 按 `replace_plugin_tools` 过滤插件工具 |
| `omni_core/brain/sdk_loop.py` | 改 | 元工具挂载、`agent.mcp_servers` 按需 append、目录注入与压缩重贴、`_McpSession` 生命周期挂载 |
| `omni_core/local/loop/sdk_bridge.py` | 改 | 非 chat 且 tools_enabled 时构建目录并透传给子 run |
| `backend/api/routers/tools_api.py` | 改 | eager 真连接、lazy 读缓存；servers 段新增 `mode/state/ready/state_reason` |
| `gateway/channels/xiaohongshu.py` | 改 | `_login_status` 先读缓存（TTL 内直接返回），不在通道 `ready()` 里发 20s RPC |
| `plugins/xiaohongshu/normalize.py` | 新 | 归一函数抽出，供 `mcp_call` 的 transform 动态 import |
| `plugins/xiaohongshu/plugin.py` | 改 | 改为从 normalize 别名导入（行为不变） |
| `tests/test_t43_mcp_lazy.py` | 新 | 18 项 |

实施中被迫改掉的三处设计（都有实测依据）：

1. **`mode` 缺省 eager，不是 lazy**。`test_loop_pool` 立刻暴露：现网 mcp.json 条目大多没写
   `mode`，缺省 lazy 会让它们静默"不连接、不注入"，等于把已配好的能力悄悄关掉。懒加载改为
   显式声明。
2. **`list_tools` 不能裸调**。真实探活第一次就拿到 `degraded/UserError`——动态 filter 需要
   `run_context` + `agent`（`agents/mcp/server.py:1019`）。补了 `mcp_state.list_tools` 统一入口，
   探活 / `McpSession._describe` / 设置页枚举三处统一走它。
3. **就绪探针缺省不跑**。实测 `check_login_status` 11093ms（要驱动浏览器），2.0s 预算下
   必然超时；且它返回纯文本而非 JSON，字段口径失效。改为"默认只裁端口+协议"，登录态由
   真实调用结果回写（`record_call_failure`）。

测试结果：

- MCP 相关 54 项全绿（`test_loop_pool` / `test_t43_mcp_lazy` / `test_m3_mcp` /
  `test_t41_mcp_filter` / `test_t42_mcp_reconnect`）。
- 全量 `pytest tests` 有 17 项红，根因单一：该执行环境 PATH 只暴露 `pwsh`
  （Doubao sandbox runtime），没有 `cmd.exe`/`powershell.exe`/`bash`，`shell_tool` 的
  环境自门判空 → `shell_exec` 不注册，连锁打挂 `approval_gate` / `approvals_e2e` /
  `m3_tools` / `api_integration` / `o2_encoding` / `plugin_loader` / `skill_axis` / `task_tmp`。
  与本次改动无关，未在本任务内修改（属另一条线：`_SHELLS` 缺 `pwsh` 声明）。

未做 / 待办：

- `lifetime=run` 的主动断开：`McpSession.close()` 已写好，但 `run_subtask_sdk` 多出口，
  尚未在 `finally` 里调用（§11 第 1 条）。
- `~/.omniagent/mcp.json` 未写入该 server 段：不动运行配置，切换由使用者决定。
- 设置页前端未加 lazy 标识渲染（后端字段已就绪）。

### 12.1 运行配置（已写入，值不入库）

本机 `~/.omniagent/mcp.json` 增加了一条 `mode: lazy` 的 url 型 server（带 `spawn` 段），
写入前已备份原文件。**具体路径、端口、脚本名属于部署信息，本文不复述**；只记三个与机制
相关、换个部署也成立的取值理由：

- `tool_filter.include` 不含昂贵的诊断型探针（实测某服务单次 ~11s）：把它当常驻工具给模型
  必被误调；需要诊断时走 `mcp_call`——`call_tool` 不受 `tool_filter` 约束，模型仍能显式要求。
- `readiness` 只留 `hint`：`probe_readiness_on_activate` 未开 → 激活路径不跑探针，
  hint 留给 `record_call_failure` 回写时显示。
- `replace_plugin_tools` 是**布尔开关**，不是工具名列表：为 true 时该 unit 的插件工具整体
  不进本轮清单，插件版只读工具因此让位给 lazy MCP。

写入后实测（`split_configs` + `probe`，只读）：`eager` 为空（唯一另一条是 disabled，被跳过）、
`lazy` 1 条；槽位与 `replaced_units` 各 1 条；探活 **235ms → ready**，注入工具 3 个；
`mcp_state.json` 从旧的 `degraded` 刷成 `ready`。

生效时机：`mcp.json` 在 `ToolLoop` 构造期读取，**重启后端**后生效；回退删段或把 `mode`
改回 `eager`。

### 12.2 服务进程的生命周期：spawn（按需拉起）

问题：某些 url 型 MCP 服务（示例：一个本地采集服务的 `<svc>.exe`）是**独立 HTTP 服务**（127.0.0.1:<port>），不是 SDK 能当 stdio
子进程接管的那类 server（`command` 型走的是 stdio 管道，跟它无关）。没人起它，开机后第一次
使用永远是 `offline`，得手动跑 `scripts/<svc>_mcp.bat`。

做法：在 `probe` 的 L0 加 `spawn` 分支——

```
L0 端口不通
  ├─ 配了 spawn → ensure_local_service
  │     锁内双检端口 → Popen(CREATE_NO_WINDOW + 独立进程组)
  │     → 锁外轮询端口（ready_timeout_s，缺省 20s）→ 通了继续 L1（给协议层 +5s 独立预算）
  └─ 没配 spawn → offline（hint 指向启动脚本）
```

配置：

```json
"spawn": {
  "command": "<ABS>/tools/<svc>/<svc>.exe",
  "cwd": "<ABS>/tools/<svc>",
  "args": [],
  "ready_timeout_s": 20,
  "hint": "xhs 服务未在跑且自动拉起失败：手动运行 scripts/<svc>_mcp.bat"
}
```

三条纪律（都不是可选项）：

- **幂等**：进锁后先复查端口，通了就返回 `already_up`——两个 run 同时激活不会拉出两份服务。
- **不阻塞他人**：等端口在锁外做；否则一个 run 的冷启动会把别的 run 卡在锁上十几秒。
- **只起不杀**：不做 stop/restart。误杀一个共享服务比拉不起来更糟；要"空闲退出"必须另做看护。

实测（2026-11-13，Windows 本机）：空闲端口 + 临时假服务 → `spawned`、端口在 8s 内就绪、
子进程 pid 正常；紧接着第二次调用 → `already_up`（未重复拉起）。真实服务：在跑时
`probe` 走端口通分支（235ms ready），`spawn` 完全不触发。

代价与边界：

- 冷启动第一次激活要等 3–8s（有的服务还要起浏览器）；之后服务常驻，再激活回到 ~235ms。
- 服务起来后不会自己退，直到关机。想"用完就停"需要独立看护（按 `mcp_state` 的访问时间
  轮询 + 停进程），未做。
- **不装开机自启**：那等于每次开机就跑一个浏览器驱动，与"不需要时零成本"直接冲突。
  真要开机自启，把启动脚本的快捷方式放进 `shell:startup` 即可
  ——系统级改动，由用户自行决定，代码不代劳。
