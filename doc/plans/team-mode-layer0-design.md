# layer0-dispatch-foundation-2026-09-30

> 状态：**设计稿（三稿/终稿），未实施**。本 plan 是 `team-mode-design.md` 的**前置依赖**，本身可独立落地（且与主流 Supervisor 一致）；team-mode 主体暂缓。
> 日期：2026-09-30（三稿，基准 commit `c5ae9df`）
> 修订摘要：① §3.3 透传落点由死代码 `_normalize_subtasks` 更正为 `sdk_loop.parse_dispatch_items`（阻断性修正）；② **§3.5 终稿拍板：解绑固定双层架构**——门控只剩 `enabled`，brain 自派发为合法语义，planner/worker 分层降为配置形态；③ 补 `default_executor_slot` / `executor_cfg` 注入 / 按槽 reasoning_mode 等规格缺口；④ 全部行号按最新代码复核；⑤ **§5.1 新增实施同步清理清单**——全仓"架构锚点 / 固定双层 / 在线大脑\+本地执行器"过时描述的探查结果与处理方案（以本 plan 落地后状态为基准）。
> 
> 

---

## 1. 定位：本 plan 与 `team-mode-design.md` 的关系

```
大脑（中枢 agent，self.brain）
                           │
                           │   产出派发计划 items=[{desc, done_when, need_verify?, agent?}]
                           ▼
        ┌─────────────────────────────────────────────┐
        │  Layer 0（本 plan）：通用派发底座             │
        │  - executor 注册表 slot→LLMClient            │
        │  - 按 item["agent"] 路由到目标执行单元        │
        │  - graph 骨架（Send 扇出 + results 聚合）不动 │
        └─────────────────────────────────────────────┘
                           │
                           │   agent 字段为空 → 匿名子 agent（MT-1 现状/保底）
                           │   agent 字段=逻辑名 → 定向派发（MT-2）
                           ▼
        ┌─────────────────────────────────────────────┐
        │  team-mode-design.md（主体，暂缓）            │
        │  - roles state：agent→{instructions,history} │
        │  - spawn_team 元工具（读 skill L2 建团）      │
        │  - 角色常驻 + 滚动摘要记忆 + 黑板 view(scope) │
        │  - dispatch agent 字段升级为"角色名"          │
        └─────────────────────────────────────────────┘
```

**对接本质**：Layer 0 把"子 agent 是谁"从一个写死的 `self.executor` 变量，升级成一个**按逻辑名寻址的执行单元注册表**；`team-mode-design.md` 在此基础上把"逻辑名"进一步升级为"带身份与持久记忆的角色"。两者共用同一个接缝 = **`dispatch`**** 计划项的 ****`agent`****（逻辑名）字段 \+ executor 注册表**。

**降级链映射（对应 ****`team-mode-design.md`**** §6）**：

- MT-1 匿名 worker 扇出：现状，Layer 0 保持。

- **MT-2 定向派发：= Layer 0 使能**（`agent` 字段生效，但无角色记忆）。这是本 plan 的落点。

- MT-3 动态建团（spawn_team \+ roles state）：team-mode 主体，本 plan 不实现，只预留接缝。

---

## 2. 现状：写死的双态派发（已定位，待解耦）

|#|写死点|位置|问题|
|---|---|---|---|
|①|派发开关绑死"是否存在单一 worker"|`graph_runner.py:334allow_dispatch = enabled and not self.executor_is_planner`|没配 worker 就彻底关掉派发，中枢无法表达"派给某角色"|
|②|只有单一执行单元|`core.py:174-210` 仅 `self.executor = LLMClient(resolve_slot("worker"))`|所有子任务共用同一模型，无法定向|
|③|子任务永远用 `self.executor`|`graph_runner.py:574-586_run_subtask` → `self._run_via_sdk(inner_spec, self.executor, ...)`|子 agent 匿名、无角色、无差异|
|④|**计划项字段在中途被剥掉**|`sdk_loop.py:79-107parse_dispatch_items` 把每项重建为只含 `desc`/`done_when` 的新 dict|任何新增字段（含 `agent`）在此静默丢失——**这是 ****`agent`**** 透传的真实落点**|

**数据流（现状，已按最新代码核实）**：

```
_mk_dispatch（sdk_loop.py:727-755，工具定义：items 顶层参数 + need_verify 顶层参数）
  → parse_dispatch_items(items)        # sdk_loop.py:79-107 ← 字段在此被剥（只留 desc/done_when）
  → state.dispatch_plan / gate.dispatch_plan
  → sdk_bridge.py:434  res["dispatch_plan"]
  → graph_runner.py:256 "plan"         # _run_main 返回
  → graph.py:147-156    Send payload   # item 完整 dict 透传，无再加工
  → graph.py:117-127    _sub 节点      # dict(state["item"]) 原样交出
  → graph_runner.py:349-372 _sub_fn(item, budget)
  → graph_runner.py:543-615 _run_subtask → 永远用 self.executor
```

> **勘误（评审发现，阻断性）**：初版设计稿 §3.3 认为透传点在 `graph_runner.py:97-132 _normalize_subtasks`。该函数是**死代码**——全仓无调用方（model-routing 迁移后遗留）。若只改它，`agent` 字段在 `sdk_loop` 就没了，整条路由链路静默失效。
> 
> 另：初版说 `agent` "与 `need_verify` 同处理法"不成立——`need_verify` 不是从 item 幸存的，而是 `_mk_dispatch`（`sdk_loop.py:742-746`）从工具**顶层参数**读出后重新附加到每个 item 上的，两条机制不同，不能类比。`agent` 是**逐项字段**，必须在 `parse_dispatch_items` 白名单里幸存。
> 
> 

---

## 3. Layer 0 设计（不含角色身份/常驻/记忆）

### 3.1 执行单元注册表（executor registry）

`core.py` 构造函数中，把单一 `self.executor` 升格为 `self.executors: Dict[str, LLMClient]`，键为逻辑槽名：

- **槽名来源（拍板）**：`runtime.dispatch.agents: [...]` 显式声明（如 `["worker", "researcher", "coder"]`），**不用**`models.json` 的 `defaults.*` 任意键隐式收集——后者有 typo 风险（手写 `"reseacher"` 会静默多出一个槽，模型拼错槽名反而命中，错误更难发现）。显式声明 \+ dbg 日志列出全部槽名，拼错槽名回退默认槽时日志可见。

- 每个槽经 `router.resolve_slot(slot)` 解析（**已对任意槽名生效**，见 `router.py:109-127`，无需改 router）；解析为空则不入表（该用途未启用）。

- 复用现有 `model_health_ok(exec_cfg.get("base_url"))`（`core.py:195`）做不可达回退：不可达槽从表中剔除并记日志。

- **`executor_cfg`**** 直注入兼容（规格补全）**：`ToolLoop(executor_cfg=...)`（`core.py:82`）是测试与旧调用方的构造路径。注册表构建时若显式注入 `executor_cfg`，将其映射为 `worker` 槽入表，保证 `tests/test_t44_verify_dispatch.py` 等现有测试构造方式不断裂。

- **`self.executor`**** 向后兼容别名**：保留为 `self.executors.get("worker") or self.brain`（避免一次性改崩其他调用）。`self.executor_is_planner` 退化为 `self.executors.get("worker") is None or self.executors["worker"] is self.brain` 的纯观测标记（§3.5 门控解绑后不再参与任何决策，仅 dbg/轨迹沿用旧字段名，避免一次性改崩消费方）。

- **`default_executor_slot`****（规格补全，初版未定义）**：恒为 `"worker"`。语义：`worker` 槽在注册表里 → 匿名项走它；不在 → 匿名项回退 `self.brain` 自派发（**合法语义，见 §3.5 终稿**）。不引入可配置项——多一个配置位只多一个出错面。

- **按槽附属属性（规格补全）**：现状 `executor_reasoning_mode`（`core.py:129`）与 `_make_llm_emitter("executor")` 的 role 标签都是单份的。多槽后改为按槽取值：

    - `executors_cfg[slot]["reasoning_mode"]` 随槽走，`_run_subtask` 按 target_slot 取（缺省 `native`）；

    - `_make_llm_emitter` 的 role 从固定 `"executor"` 改为 `"executor:{slot}"`（或保留 `"executor"` 当 slot 为默认槽，非默认槽加后缀——以前端/轨迹消费方式定，实施时二选一，验收标准是不同槽可区分）。

### 3.2 `dispatch` 计划项加可选目标字段

在 `dispatch` 工具 schema 与大脑输出约定中，计划项新增可选字段：

```json
{
  "desc": "...",
  "done_when": "...",
  "need_verify": true,
  "agent": "researcher"
}
```

- 不传 `agent`：匿名子任务，走默认槽；默认槽缺 → brain 自派发（§3.5 终稿，合法语义）。

- 传 `agent`：定向派发给该逻辑名对应的执行单元（MT-2）。

- 红线一致（`team-mode-design.md` §4 约束 1）：`agent` 只是逻辑名，由 config 解析到实体；绝不在工具里选 endpoint。

- **大脑需知道可用槽名**：dispatch 工具 docstring 的 `items` 说明里列出当前注册表槽名（动态拼接，如"可选 agent：researcher/coder；缺省走默认执行单元"）。模型无法派给不知道存在的槽——这一条初版遗漏，不加则 `agent` 字段基本不会被模型用到。

### 3.3 `agent` 字段透传（落点更正）

**真实落点是 ****`sdk_loop.py`****，不是 ****`graph_runner._normalize_subtasks`****（死代码，不动它）**：

1. `parse_dispatch_items`（`sdk_loop.py:79-107`）：白名单从 `{desc, done_when}` 扩为 `{desc, done_when, agent}`——`agent` 为非空字符串才保留（`str(item.get("agent") or "").strip()`），否则不写入，保证下游 `item.get("agent")` 语义干净（缺失 = 匿名）。

2. `_mk_dispatch`（`sdk_loop.py:727-755`）：docstring 中 `items` 的格式说明补 `"agent": "执行单元名（可选）"`。

透传链其余环节（gate.dispatch_plan → res\["dispatch_plan"] → plan → Send payload → _sub_fn）已核实为完整 dict 透传，**零改动**。

### 3.4 `_run_subtask` 按目标路由

`graph_runner.py:543-615` 的 `_run_subtask` 增加形参 `target_slot=None`，内部解析执行单元与能力：

```python
def _run_subtask(self, inner_spec, ..., target_slot=None):
    slot = (target_slot or "").strip() or self.default_executor_slot
    client = self.executors.get(slot)
    if client is None:
        if target_slot:  # 定向槽不存在 → 记日志回退默认槽，永不抛错
            self._log(f"[dispatch] agent 槽 '{slot}' 不在注册表，回退默认槽")
        slot = self.default_executor_slot
        client = self.executors.get(slot) or self.brain  # 默认槽也缺 → brain 自派发（合法语义，§3.5）
    caps = self.exec_capabilities.get(slot) or self.executor_capabilities
    # 日志 exec_model / is_planner 改为按 slot 取值；emitter role 按 §3.1 带槽名
    res = self._run_via_sdk(inner_spec, client,
                             build_system_prompt(caps, self.tool_schemas, ...), ...)
```

`_sub_fn`（`graph_runner.py:349-372`）改传 `target_slot=item.get("agent")`。同时 `_sub_fn` 回填 `res` 时透传 `res["agent"] = item.get("agent", "")`，`_finalize_fn` 的 `subtask_results` 摘要增加 `"agent"` 字段（排障时能看出谁跑了什么）。

### 3.5 `allow_dispatch` 门控改写（终稿：彻底解绑固定双层）

`graph_runner.py:334` 原逻辑在"无独立 worker"时关掉派发。**终稿拍板（2026-09-30 用户定调）：不要固定的双层架构**——planner/worker 分离不再是引擎里的写死语义，而是注册表的一种可选配置形态。门控彻底简化：

```python
allow_dispatch = bool(_dispatch_cfg.get("enabled", True))
```

**语义**：

- **中枢（大脑）自己决定**本轮是否派发、派给谁——这正是 Layer 0 的标题承诺："由中枢自行决定派给谁"。编排层不再替大脑做"有没有人可派"的预判。

- **匿名项路由**：默认槽（`worker`）在注册表 → 走它；不在 → **回退 brain 自派发（合法语义，非降级告警）**——子 agent 用主模型开全新上下文执行，等价于"中枢给自己开一个干净的执行分身"。这与主流 Supervisor 一致：supervisor 完全可以把任务派给同模型的 member。

- **brain 自派发的价值场景**：并行 I/O 型子任务（各自独立、互不依赖、上下文干净反而更好）——派发的目的可以是**并发与上下文隔离**，不必然是"能力分层"。旧门控把"派发"与"有独立 worker"绑死，恰恰堵死了这个用法。

- **成本自担由模型判断**：`dispatch` docstring 既有约束（"仅当子任务互不依赖、各自耗时较长时才使用；本轮能直接做完的请自己做"）已经把"何时不该派"交给大脑判断，无需引擎再用门控猜。单模型用户如果担心模型乱派，可显式 `enabled: false`——配置位已存在，行为可预期。

- **透明性**：匿名项回退 brain 时打一条普通 info 日志（`[dispatch] 匿名子任务由主模型执行`），供观测而非警告——这是正常路径，不是配置错误。

**`worker`**** 槽的地位变化**：从"架构锚点"降为"注册表普通槽名 \+ 匿名项的默认路由约定"。名字保留纯粹是向后兼容（models.json 的 `defaults.worker` 不用迁移）；若配置里没有它，什么都不会坏，只是匿名项落到 brain。

**双层成为"配置"而非"架构"**：想要 planner/worker 分层的用户，在 models.json 给 `worker` 槽选个模型即可——分层由数据（目录选择）表达，不再由代码（门控 \+ `executor_is_planner` 特判）表达。这也与 `team-mode-design.md` 的三层分离理念一致：**架构决策尽量下沉为配置/数据**。§4.1.2 的"双层大脑"从"引擎本体价值"改读为"一种值得保留的配置形态"。

> 历史注记：M-fix 时代的门控（`not self.executor_is_planner`）防的是"本地 4B 未启动 → 子任务静默失败烧步数"，当时的回退确实该关。model-routing 之后该担忧已由 `model_health_ok` 健康检查按槽处理（不可达槽不入表），门控的历史使命完成，可安全解绑。
> 
> 二稿曾拍板"匿名/定向双门控"保双层——三稿按用户定调废弃，理由见上。
> 
> 

### 3.6 `models.json` 多 slot 声明

`defaults` 已支持任意键（`router.resolve_slot` 只读 `_defaults().get(slot)`）。Layer 0 在文档/配置示例里说明：除 `main` 外可声明 `worker` / `researcher` / `coder` 等。

- **槽名注册以 ****`runtime.dispatch.agents`**** 为准**（§3.1 拍板）：models.json 的 `defaults.<slot>` 是"该槽指向哪个模型"，config 的 `agents` 是"哪些槽参与派发"——两层职责分离，typo 风险收敛在显式声明处。

- `router.py:160` 的 `SLOTS` 元组（API 视图 `list_models` / `set_default` 用）**暂不扩展**——Layer 0 不依赖 API 改动；后续若要在前端选角色模型，再扩 `SLOTS` 或新增 `agents` 列表。已核实 `save_providers` 清理 defaults 时按 selection 有效性过滤（`router.py:222-228`），不按 SLOTS 白名单，额外槽写进 models.json **不会被冲掉**；仅 `set_default`（`router.py:235-236`）拒绝非 SLOTS 槽——`researcher` 等槽的 defaults 需手改 models.json 或后续扩 SLOTS，当前阶段可接受。

### 3.7 健康/回退/日志复用

全部复用既有 `model_health_ok` 回退与 `self._dbg("executor")` 诊断埋点，仅把"单执行单元"语义换成"按槽取"（dbg 事件名可带槽名后缀，如 `executor_unavailable:{slot}`）。

---

## 4. 与 `team-mode-design.md` 的对接面（本 plan 重点）

**共享接缝（Layer 0 产出，team-mode 消费）**：

1. `dispatch` 计划项的 `agent` 字段 —— Layer 0 认为是"逻辑名→执行单元"；team-mode 认为是"角色名"。**完全同字段，向后兼容。**

2. executor 注册表 `slot→LLMClient` —— team-mode 的"角色模型"直接复用此表解析（skill L2 `model:` 写逻辑名，如 `executor`/`researcher`，由 config 落到实体）。

3. `_run_subtask(target_slot=...)` 路由入口 —— team-mode 在此之上叠加"角色 instructions 注入 \+ history 挂载"。

**team-mode 在其上叠加的内容（本 plan 不做，仅预留）**：

> 取向提示：以下"角色常驻/记忆"沿用 `team-mode-design.md` §4 的**手搓 ****`roles`**** 字典 \+ history** 写法；但 2026-09-30 讨论后倾向更框架化的形态——**角色 = LangGraph subgraph \+ 自带 state/checkpointer**（框架原生持久化），ToolLoop 仅作节点函数。两种形态共用同一接缝（`agent` 字段 \+ executor 注册表）。取舍见 §4.1，**最终以 §4.1 为准**。
> 
> 

- **（备选·手搓）**`graph_runner` 的 `state` 加 `roles: {name → {instructions, history}}`（`team-mode-design.md` §4 末）。

- `spawn_team` 元工具：读 skill L2（`role/scope/model/tools/knowledge`）生成角色定义，写入 `roles` state；`agent` 字段从"执行单元逻辑名"升级为"角色名"，运行时按 `model:` 逻辑名查 executor 注册表。

- 角色常驻 \+ 滚动摘要记忆：`_run_subtask` 在取 `client` 后，额外把 `roles[name].instructions` 包装进 system prompt（通用系统提示 \+ duty 注入，`team-mode-design.md` §4 约束 2），并读写 `roles[name].history`。

- 黑板 `WorldModel.view(scope)` 分区（`team-mode-design.md` §4）独立于本 plan。

**红线在对接中保持**：

- core 仍零场景零知识：角色定义来自 skill（L2），不在代码写死（约束 2）。

- `agent`/`model` 始终是逻辑名，由 config 解析（约束 1、§3.2）。

- `spawn_team` 与 `dispatch` 同机制（元工具 \+ `StopAtTools` 交回编排层），复用 M7 骨架（约束 3）——本 plan 的 `_sub_fn`/`_run_subtask` 路由即为该骨架的承载点。

---

## 4.1 框架化取向：手搓 vs 框架（2026-09-30 补充，待拍板）

本 plan 与 `team-mode-design.md` 的多 agent 演进，遵循一条已明确的工程取向：**编排层与角色持久化优先用 LangGraph 原语；引擎本体（我们的 ToolLoop）保持自研，不换成框架 agent runtime。**

### 4.1.1 哪些手搓是"套在框架上的胶水"（可框架化 / 最小手搓）

- `graph.py:99-100max_parallel` 截断：LangGraph 的 `Send` 无内建并发数上限，我们手动截 plan 模拟——属框架缺口，**保留最小手搓**即可（不必为"纯框架"而去掉）。

- `graph.py:124asyncio.to_thread`：因子执行体是同步 ToolLoop 才需包一层；若子执行体改 async 原生可去掉，属适配层。

- `graph.py:136-145max_rounds` / 预算闸门：防无限派发的安全栏，手搓——**必须等价保留**（框架无此语义）。

> 这类手搓是"我们引擎套在 LangGraph 上"才产生的 incidental complexity；team-mode 改用 subgraph \+ checkpointer 后，部分胶水可被框架原生能力吸收。
> 
> 

### 4.1.2 哪些必须保留（引擎本体 = 产品价值，非迁移包袱）

- WorldModel 共享黑板 \+ 读写门控 / scope

- meta-tools \+ `StopAtTools` 交回编排层（M7 派发回收骨架）

- `escalate` / `verify` 完成判定（防假成功）

- 长任务压缩、Curator 自升级 / K 系列知识闭环

- 双层大脑（planner / executor 分离模型）

> graph \+ 原生 SDK **均不提供上述能力。一个纯 graph\+sdk 新项目能快速搭出 supervisor 骨架，但拿不到这些迭代稳定的可靠性 / 自进化特性。因此它们不是迁移包袱，是产品本身，不换框架 agent runtime**。
> 
> 

### 4.1.3 team-mode 推荐形态（以本小节为准）

- **角色 = LangGraph subgraph \+ 自带 state / checkpointer**：角色常驻与滚动摘要记忆由框架 checkpointer 原生承担，替代 §4 "备选·手搓" 里的 `roles` 字典 \+ 手管 history。

- **扇出仍用 ****`Send`**（已是框架）；`_sub` 节点内部调用我们的 ToolLoop（保留引擎语义与可指定模型 / 工具）。

- `spawn_team` 写出的角色定义（skill L2 驱动）映射到 subgraph 的初始化参数（instructions / 目标 slot / scope）；`agent` 字段 = 角色名 → 路由到对应 subgraph 实例。

- 结果：框架做骨架与持久化，我们的 loop 当节点函数——既"用框架"又不丢引擎价值，也保留可指定 workagent 模型的需求。

---

## 5. 影响范围与验证

**改动文件**：

- `omni_core/local/loop/core.py`：executor 注册表（§3.1，含 `executor_cfg` 直注入映射与按槽 reasoning_mode）。

- `omni_core/brain/sdk_loop.py`：`parse_dispatch_items` 白名单透传 `agent` \+ `_mk_dispatch` docstring（§3.3，**新增——初版遗漏的真实落点**）。

- `omni_core/local/loop/graph_runner.py`：`_run_subtask` 路由 \+ `_sub_fn` 传参/回填（§3.4）、`allow_dispatch` 门控（§3.5）、`_finalize_fn` 摘要加 `agent`。

- `omni_core/brain/router.py`：**不动**（`resolve_slot` 已通用）；仅文档/示例补充多 slot。

- `config.example.yaml`：`runtime.dispatch.agents` 配置示例。

- `models.json`：配置示例补充多 slot 声明（§3.6）。

- §5.1 清单所列的注释 / 文档 / 前端文案文件（同步清理，均无行为变更）。

**redline 守护**：core 仍场景无关；本改动只改变"子任务用哪个 LLMClient"，不引入任何业务词表/启发式。既有 `tests/test_t44_verify_dispatch.py` 等必须保持绿。

**新增验证**：

- 单测：构造含 `worker` \+ `researcher` 双槽的 fake 配置，断言：

    1. **`agent`**** 字段在 ****`parse_dispatch_items`**** 输出中存活**（评审新增——防透传链路回归的第一道断言）；

    2. `agent="researcher"` 的项走对应 `LLMClient`；

    3. 缺省项走默认槽；

    4. `agent` 拼写错误回退默认槽且不抛错（有日志）；

    5. `executor_cfg` 直注入构造的 ToolLoop，注册表含 worker 槽（兼容旧测试构造路径）；

    6. **worker 配置形态等价断言**：注册表仅含 worker 槽、计划项全不带 `agent` 时，子任务路由结果与现状完全一致（走 worker client，dispatch 可用）；

    7. **单模型自派发断言**：注册表为空（纯单模型）时 `allow_dispatch` 为真；匿名项路由到 brain（自派发合法路径，info 日志，无告警）；

    8. **门控开关断言**：`runtime.dispatch.enabled: false` 时 dispatch 工具不挂载，任何配置形态下均不派发。

- 真实：双模型配置下，让大脑在派发计划里指定 `agent`，观察子任务使用目标模型（轨迹里 `executor:{slot}` role 可区分）。

### 5.1 实施同步清理：过时架构描述与实现清单

> 背景：本 plan 落地后（executor 注册表 \+ §3.5 门控解绑 \+ `executor_is_planner` 降观测标记），"固定双层 / 在线大脑\+本地执行器 / worker 架构锚点"**全部成为过时表述**。实施时同步清理，避免代码语义与描述脱节。以下清单为 2026-09-30 全仓探查结果（基准 commit `c5ae9df`），**均为本 plan 的实施范围，不再单独决策**。
> 
> 

**A. 运行时代码（锚点机制——即 §3.1/§3.5 本体，非额外改动）**

|位置|处理|
|---|---|
|`graph_runner.py:332-334`|门控按 §3.5 终稿只剩 `enabled`；M-fix 注释整块替换为 §3.5 语义注释（"中枢自行决定派不派；健康检查按槽接管"）|
|`core.py:176/210`|`executor_is_planner` 写点降为观测标记（§3.1），不再参与任何决策|
|`graph_runner.py:561`|`subtask_start` dbg 的 `is_planner` 观测字段保留（兼容轨迹消费方）|

**B. 代码注释（纯文字清理，零行为变更）**

|位置|过时内容|处理|
|---|---|---|
|`omni_core/brain/__init__.py:1-4`|"在线强模型规划者…本地执行器只负责把工具结果回填、循环派发"|按现状改写：客户端 \+ Agents SDK 循环 \+ 槽位路由（循环早已交给 `Runner`）|
|`core.py:192-204`|`executor(本地4B)` 日志措辞、M-fix 健康回退注释|措辞改 "executor(worker 槽)"；回退**逻辑不动**，注释按 §3.1 健康检查语义改写（"槽不可达→不入表"）|
|`core.py:121-123, 130`|"单大脑模式 / 两层模式 / 本地小模型 4B@8K ctx"|改中性表述："主链 / 子任务"；`max_history` 与 `worker_history_keep` 的说明保留（语义仍准确）|
|`telemetry.py:8`|"越低 = 本地执行器越自主"|改 "越低 = 子 agent 越自主"（指标计算不动）|
|`backend/api/router_settings.py:13-21`|docstring 描述 `runtime.executor` / `llm.local_as_tool` 配置结构|删过时键描述——与同文件 `:116-117` "端点真源已收敛到 models.json" 的现状注释自相矛盾|
|`config.py:7`|"真 API key、`llm.local_as_tool`…只存这里"|删 `llm.local_as_tool` 字样|

**C. 工具 schema / prompt 文案**

- `brain/tools.py:37, 43-44, 60, 73, 89`：escalate/verify 描述与注释中"当你（本地执行器）…交回**在线大脑**"、"两层反思阶段"、"两层内层循环"——brain 兼任 worker 时这些措辞对子 agent 是错的。

- **前置检查（实施时先做）**：该常量链（`parts.py` re-export → `loop/__init__.__all__` → 五 Mixin import）疑似死代码——实际进 prompt 的是 `sdk_loop.py` 动态定义（`@sdk_function_tool`）的 docstring。实施时先确认消费链：若常量为死链，改 `sdk_loop` 侧动态 docstring \+ `tools.py` 注释即可；常量本体的死代码清理不在本 plan（另行处理）。

- 措辞原则：escalate 目标统一为"把现场交回**主 agent / 编排层**"，不预设执行单元身份（"本地执行器"→"子 agent"）。

**D. 文档（按本 plan 落地后状态重写）**

|位置|处理|
|---|---|
|`README.md:7, 19, 55, 84, 101, 122, 168, 214`（中英双语对称改）|定位句改为"主 agent \+ graph 自主派发（主 agent 决定派不派、派给哪个槽）\+ 多槽模型目录（`~/.omniagent/models.json`）"；设计原则 #5"大脑低频、本地高频"→"派发可选：是否分层、分层用哪些模型由目录配置决定"；quick start"本地执行器另起"→"可选：为 worker 等槽位配置模型"。扩展性段按诚实边界惯例写 teammode："规划中：skill 驱动的角色化团队（team-mode），复用本 plan 的 `agent` 字段与执行单元注册表"——不写成已实现|
|`doc/agent-control-master-spec.md:4-5, 183, 185, 350-352`|头部加过时声明（X3 架构描述停留在三通道/mode 时代，`run_task_two_layer`/mode 三态均已删除；现状以 README \+ `doc/plans/` 为准），**不逐节重写**（历史固化文档，逐节重写成本高且失去留档价值）|
|`doc/agent-control-arch-design.html` / `agent-control-product-design.html`|历史设计/调研文档，**留档不动**|
|`doc/plans/implemented/*` / `backlog-maintenance-plan-2026-09-29.md`|已完成记录 / 已含过时标注，**不动**（项目惯例：plan 是历史档案）|
|`team-mode-design.md`|**不动**（本 plan 的下游消费方）|

**E. 前端**

|位置|处理|
|---|---|
|`web/src/pages/Settings/index.tsx:510`|AboutTab "大脑（在线强模型）作规划/反思，本地快模型作执行器"→"主 agent 规划并执行；可在模型页为多个槽位配置模型，主 agent 自主决定派发"|
|`Settings/index.tsx:521/538`|"① 双层路径 / ② 单大脑路径"→"worker 槽已配置 / 未配置形态"（帮助文案标签中性化，内容里 long_task 生效范围说明仍准确，保留）|
|`Settings/index.tsx:548`|"没有子 agent 可派（派发无意义）"**必须随 §3.5 门控解绑同步删改**——单模型下 dispatch 已可用（brain 自派发），原句误导；改为"派发由主模型自派发执行（并发与上下文隔离仍有价值）"|
|`web/src/pages/Settings/ModelProviders.tsx:39-43`|SLOTS hint 为现状准确描述，**不动**（本 plan 不扩 SLOTS，§3.6）|

**F. 脚本/测试（留档）**

- `scripts/verify_two_layer_live.py` / `scripts/verify_m4b1_two_layer.py` / `scripts/run_mvp.py`：历史真机验证脚本，文件名与措辞属历史，**留档不动**。

- `tests/test_telemetry.py:43`：注释"两层"为历史命名，测试逻辑有效，**不动**。

**防误伤清单（命中关键词但与过时架构无关，禁止改）**：

- `router.py:9-10` "worker 由主模型兼任"（现状语义）；`backend/api/routers/helpers.py:330-342`（现状注释）；

- `skill_library.py:162` "双层级读取"、`doc/end-to-end-dataflow.md:102` "双层目录"（均指**技能目录层级**，非架构）；

- `doc/plans/layer0-dispatch-foundation-2026-09-30.md` 自身（本 plan 的历史演进记录）。

**清理验证（并入 §5 验证清单）**：

1. 清理后 grep 断言：`omni_core/` 与 `web/src/` 中不再出现"在线大脑/本地执行器/双层路径"作为**架构描述**（豁免：`scripts/` 历史脚本、`doc/` 历史留档、`tests/` 历史命名）；

2. B/C/E 类改动均为纯文案/注释，跑既有测试确认零行为变更（`test_t44_verify_dispatch` / `test_m6_shared_board` / `test_model_router` 全绿）；

3. 前端改动走 lint \+ build。

---

## 6. 触发条件（何时开始做）

- 本 plan 排期在 team-mode 之前（或并行皆可，因二者接缝已锁定）。

- 开始信号：用户确认本修订版设计无误、且 redline 测试可覆盖。

---

## 7. 明确不做（边界）

- 不实现 `spawn_team`、角色 `roles` state、角色滚动记忆、`WorldModel.view(scope)` 分区 —— 这些是 `team-mode-design.md` 的 MT-3 主体。

- 不引入异步并行（文档 MT-4 远期）；Layer 0 仍复用 `max_rounds` 栅栏串行/并发扇出。

- 不改动 graph 骨架（Send 扇出 \+ results 聚合）与 `build_agent_graph` 拓扑。

- 不动死代码 `_normalize_subtasks`（保持原样；若后续清理另行处理）。

---

## 附：修订记录（2026-09-30 评审）

|#|初版问题|修订|
|---|---|---|
|1|§3.3 透传点指向死代码 `_normalize_subtasks`（全仓无调用方），照改则 `agent` 字段在 `sdk_loop` 被剥、路由链路静默失效|落点更正为 `parse_dispatch_items` \+ `_mk_dispatch`；数据流链路按真实代码重写（§2）|
|2|"与 need_verify 同处理法"类比不成立（need_verify 是顶层参数重附加，非 item 幸存）|§3.3 明确 `agent` 是逐项字段，须进白名单|
|3|§3.5 全开门控改变单模型用户默认行为（匿名派发 = brain 新上下文重跑，烧步数，非"等价子循环自做"）|一稿拍板"保底语义 \+ 定向使能"；**三稿推翻，见 #12**|
|4|`default_executor_slot` 未定义|恒为 `"worker"`，缺失回退 brain（§3.1）|
|5|`executor_cfg` 直注入路径（core.py:82，测试在用）未考虑|显式注入映射为 worker 槽入表（§3.1）|
|6|`executor_reasoning_mode` / emitter role 单份，多槽后无法区分|按槽取值，role 带槽名（§3.1）|
|7|槽来源用 `defaults.*` 任意键有 typo 静默风险|改 `runtime.dispatch.agents` 显式声明（§3.1/§3.6）|
|8|大脑不知道有哪些槽可用，`agent` 字段形同虚设|dispatch docstring 动态列出注册表槽名（§3.2）|
|9|subtask_results 摘要无执行者信息|加 `agent` 字段（§3.4）|
|10|验证清单缺"字段存活"断言|新增第 1 条断言（§5）|
|11|一稿门控 `len(executors)>=1` 下，"有槽但无 worker"形态的匿名项会回退 brain = 大脑把子任务派给新上下文的自己，双层架构在该形态名存实亡|二稿曾改为匿名/定向双门控保双层；**三稿推翻，见 #12**|
|12|**用户终稿定调（2026-09-30）：不要固定的双层架构**——planner/worker 分离不应是引擎写死的语义；二稿的"双门控 \+ 存续声明"是在往回拉|§3.5 重写为终稿：门控只剩 `enabled`；匿名项默认槽缺 → brain 自派发为合法语义（派发的价值包括并发与上下文隔离，不必然是能力分层）；双层降为"配置形态"（models.json 给 worker 槽选模型即得），`executor_is_planner` 退化为纯观测标记；历史注记说明 M-fix 门控的原始担忧已由 `model_health_ok` 按槽健康检查接管|
|13|用户要求清理全仓过时架构描述（架构锚点 / 固定双层 / 在线大脑\+本地执行器），且以本 plan 实现后的状态为基准|全仓探查结果固化为 **§5.1 实施同步清理清单**（A 代码锚点=§3.1/§3.5 本体；B 注释；C 工具 schema docstring——含死链前置检查；D 文档——README 按落地后状态重写、master-spec 头部过时声明、历史文档留档；E 前端——:548 与门控解绑联动删改；F 脚本/测试留档）\+ 防误伤清单 \+ grep 验证断言|

> （注：部分内容由豆包工作 AI 生成）
