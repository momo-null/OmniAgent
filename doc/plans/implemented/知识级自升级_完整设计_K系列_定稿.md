# 知识级自升级 · K 系列（合并文档：设计 + 可行性 + K2–K5 实施计划）

> **范围**：知识级自升级（K 系列）的设计定稿 + 可行性分析 + K2–K5 实施契约（§5–§8）。已知缺口见 §2「已知缺口」。

**背景基线**：自进化闭环消费端曾断裂（skill 零注入、`global_memory()` 零读写、world-model per-task）——对新任务系统与裸 SDK 应用无行为差异；本设计接通闭环并以实验裁决其有效性。

---

## 1. 范式：人机协同蒸馏至稳态（全序列的设计前提）

**核心命题**：当前大模型无法完全自主学习。必然是人不停提示、喂资料、纠偏，系统持续积累，最终在域内收敛到稳态。

```plain
浪漫版（弃）：agent 自主跑任务 → 自己变强 → 无限自提升
现实版（本设计基座）：人持续提示/纠偏/喂料 → 知识层积累 → 域内收敛稳态
```

**五条推论**：
1. 学习发生在交互模式；自主模式是应用不是学习（探索交还交互）。
2. 语料优先级（M5 输入）：C₁ 批准的对话轨迹 ＞ C₂ 验证的自主轨迹。
3. 稳态是目标且可检测（见下表）。
4. 人工介入几何衰减：从每轮拍板 → supervision-by-exception。
5. 成功标准 = 域内介入频率趋零，人力滚动到下一域（V3 测的即「稳态前后的差距」）。

**稳态检测指标**：

|信号|定义|收敛表现|
|---|---|---|
|蒸馏去重命中率|新任务蒸馏 facts 中已被 MEMORY.md 覆盖的比例|持续高位|
|skill 晋升率|单位任务数的 candidate→active 转化|趋平|
|步数方差|同域任务步数离散度|收缩|
|人工介入频率|交互会话中纠偏/重述轮占比|几何衰减|

域收敛判据（初值，实测校准）：去重命中率 ≥80% 且晋升率趋平 且介入频率 ≤5% —— 满足即判定稳态，蒸馏降频，人力转下一域。

**指标噪声注记**：介入频率由 C₁ 分类器（LLM 判定）产生，展示须做时间窗平滑与置信标注；分类器校准误差（§5.3）应随曲线一同呈现，防止噪声淹没早期衰减信号。

---

## 2. 里程碑总览

|里程碑|内容|依赖|状态|
|---|---|---|---|
|**K0 知识层接通**|memory 子系统 + skill/memory 弱注入（默认关）|—|✅ 已完工（commit `acb985e`）|
|**K1 运行积累 + 记忆可视化**|memory REST API + Memory Tab + 右栏 `memory_updated`|K0|✅ 已落地（分支 `refactor/agent-core-framework`）|
|**K2 信号基础设施**|C₁/C₂ 仪器 + 校准 + 纯前向采集（无回溯脚本，用户零操作）|K1|✅ 已落地 + **真机验证**（`signals.py` + `GET /signals`、`/signals/summary`）|
|**K3 有效性裁决**|蒸馏抽检 + ablation 聚合（判据：步数降 ≥15% 且成功率不降）|K2|✅ 脚本已落地（`review_rollouts.py` + `effectiveness.py`）；⚠️ V3 判据待 ≥20 次对照跑出结论|
|**K4 纠偏采集**|用户纠偏轮入蒸馏（复用 C₁，双层红线②默认关）|K2|✅ 已落地 + **真机验证**（5 例纠偏入库、零误采）|
|**K5 稳态运营**|§1 四信号接入 + 域收敛判据 + 蒸馏降频 + 域健康度面板（前端稳态 Tab 衰减曲线）|K2 + K4|✅ 已落地 + **真机验证**（`steady_state.py` + `GET /signals/steady` + 稳态 Tab）|
|**K6 LLM 蒸馏增强**（可选）|phase-1 规则版 → LLM 改写；向 M5 供给语料|K3 通过|🔮 远期|

### 时序与并行

```plaintext
K0(已完工) ─→ K1(已落地) ─→ K2(信号,自动采集) ─→ K3(裁决) ─→ [解冻 M5] ─→ K6(可选)
                                 ├─→ K4(纠偏采集)
                                 └─→ K5(稳态运营)
```

- K2 信号采集 = 纯实时内嵌（详见 §5.6），只对新任务，**用户零操作**；不扫描 / 不回溯历史，30 天窗口不进代码。
- K4 复用 K2 的 C₁ 分类器，排 K2 后（避免做两遍）。
- **时序声明**：父计划 §4 的「V1 → V0+」依赖序按范式修正由本 K 序列替代执行（K2=V1 后移至 V3 前，靠回溯过滤兜底）；父计划文件不改动（见 §11.5）。

### 已知缺口（2026-09-19 实现状态）

K2–K5 主干已落地，K2 / K5 经真机验证。下列为**明确未做**项（不影响闭环运行，属后续增强）：

|缺口|归属|说明|
|---|---|---|
|回溯过滤（清除判脏记忆）|§5.6.6|未实现；依赖足够的 C₁ 判脏样本|
|准入升级 → `approved_success`|§5.6.6|未实现；当前仍 `plain_success`，切早会误挡正常 success|
|C₃ 人工抽检校准|§5.3 / §5.6.8|未做；`c1_calibration_error` 为 `null`，率标「未校准」|
|V3 ablation 结论|§6|脚本已就绪，需 ≥20 次同域对照人工长跑|
|多域分区|§8|v1 为单域常量 `single-domain(v1)`，远期扩展|

---

## 3. K0 摘要（细节见父计划）

- **存储**：`~/.omniagent/memory/{MEMORY.md, memory_summary.md, merged.json, rollouts/<task_id>.md}`——按 task 不按天；独立 memory/ 目录（task 目录会被 rmtree 整删；`delete_task` 不得清 rollouts）。
- **蒸馏**：phase-1 规则提取（零 LLM、确定性，挂 Curator 第五件）；phase-2 未合并 ≥5 时合并（append-only + 全文去重 + summary 截 20000 字符）。
- **注入**：system prompt 附加块，三来源（memory summary + skill recall 弱匹配 limit 3），config `runtime.knowledge.*` 默认关，注入时发 `knowledge_injected` debug 事件。
- **验收**：pytest 全绿；冒烟见注入事件；rollout 生成；5 任务后合并更新。

---

## 4. K1 · 运行积累与记忆可视化（已落地）

### 4.1 用户旅程（学习零操作）
用户说目标 → agent 带着历史记忆/技能干活（右栏可见带了什么）→ 用户看结果自然反应（不满抱怨 = C₁ 标注）→ 任务结束 Curator 静默蒸馏 → 同类任务少走弯路。
信任操作（可选）：Memory Tab 浏览/编辑/溯源；对话纠偏（K4 采集）；重置（清 memory/，二次确认）；关开关即停学。隐私卖点：全链本地零上云。

### 4.2 Memory REST API 契约（`/api/runtime` 前缀）

|方法/路径|行为|要点|
|---|---|---|
|`GET /memory`|`{master, summary_chars, rollouts_total, merged_total, enabled}`|只读聚合|
|`GET /memory/rollouts`|列表 `[{task_id, distilled_at, success, facts_n, lessons_n}]` 倒序|支持分页|
|`GET /memory/rollouts/{task_id}`|单条全文（含 trajectory 引用）|过 `validate_identifier`|
|`PUT /memory`|body `{master}`；写盘后**服务端重生成 summary**|复用 Curator 截断常量，勿复制魔数|
|`DELETE /memory?confirm=reset`|重置：清空 memory/ 全部产物|缺 confirm 返回 400|
|`DELETE /memory/rollouts/{task_id}`|删单条（不回滚已合并）|优先级最低|

### 4.3 前端规格
- **Memory Tab**（SkillsAndTools 四 Tab）：① MEMORY.md 编辑器（只读+编辑切换，PUT 保存）② rollouts 溯源列表（点开全文抽屉）③ 统计条 ④ 重置按钮（输入 `reset` 二次确认）。
- **右栏学习提示**：复用 SSE `debug` 通道 `kind=memory_updated`（Curator 完成后推 `{distilled, merged}`），零新通道。
- 客户端：`memoryApi`（同 skillApi 风格）+ store 扩展；`memory_updated` 事件刷新统计。域健康度面板属 K5。

### 4.4 K1 验收
管道健康运转 ≥1 周；Memory Tab 四功能可用；**编辑生效链**：PUT 修改 → 下轮 `knowledge_injected` 的 memory_chars 反映新内容；蒸馏后右栏轻提示出现；关开关时 Memory Tab 只读可用、注入不发生；pytest 覆盖 API 契约。

---

## 5. K2 · 信号基础设施（原 V1，重定位为 V3 前置）

### 5.1 三实体一致率模型

|实体|定义|来源|
|---|---|---|
|A|系统记录的 success + 收尾路径|`*.run.json`|
|B|模型自报（结论口播）|session jsonl `assistant.content`|
|C|真值|三级推导（下）|

测 A↔C（假成功率主判据）、B↔C（自报可信度）、A↔B（分歧点即候选修复点）。中心假设：**LLM 自己知道任务是否完成**——假成功来源是转录损失而非说谎，预判集中在无条件路径（/chat 常态 done_when=""）。

### 5.2 三级 C 真值体系

|级|机制|适用|
|---|---|---|
|C₁ 对话批准|agent 轮后下一个 user 消息即裁决：接受/继续/新话题→批准；抱怨/重述→证伪|交互会话（零成本收割）|
|C₂ 观测评审|fresh context LLM，输入**仅** objective + 终态观测原文，**绝不给**轨迹/推理/结论|自主运行 / 用户沉默|
|C₃ 人工抽检|校准 C₁/C₂ 的错误率|10–20% 样本|

### 5.3 指标与判据

|指标|定义|判据|
|---|---|---|
|假成功率（分模式）|A=true 且 C=false ÷ A=true；交互段 C₁、自主段 C₂|≤10% 通过|
|漏报率|A=false 且 C=true|记录|
|分歧率|A≠B|定位修复点|
|C₁/C₂ 校准误差|分级判定 vs 人工复核不一致率|≤10% 方采信|
|不可判占比|C=不可判 ÷ 总数|>20% 先修终态观测|

### 5.4 K2 交付物
1. C₁ 分类器 + C₂ 评审——复用 config brain 通道；
2. 信号基线：由前向内嵌采集持续累加假成功率基线（按模式/收尾路径分解）；**不回溯历史 tasks/**；
3. 回溯过滤：已积累 memory 中 C 判脏的条目清除（有日志）；
4. 准入切换：蒸馏准入升级「用户批准的 success」（success + 下一轮无证伪）；
5. **自动采集契约（纯前向、无手动脚本、无回溯）**：信号仅在**新任务运行时**内嵌采集——agent 轮后 / run 结束时自动分类 C₁/C₂，写入 `*.signal.json` 并累加到 `memory/signals_aggregate.json`。**不扫描历史 `tasks/`、不回溯旧 trajectory、不处理 30 天窗口**；零写入任务原始数据；人工仅复核校准行。

蒸馏质量的外部标尺（可选）：LongMemEval / LoCoMo（Mem0 同款）。

### 5.5 假成功分支修法（K2 数字难看时另立计划）
① 无条件路径 → 完成依据自报（门核验观测存在且非空）；② 字面匹配 → 命中窗口收窄（仅终态观测内）；③ 模型自欺 → 终态强制观测后验（成本最高）。若①证实，「完成依据」可沉淀为 skill/memory 准入通用信号。

### 5.6 详细实施计划（代码级）

**5.6.1 目标与依赖**：建立三实体一致率模型 + 三级 C 真值。依赖 K1；**纯前向采集，只对 K1 之后的新任务，不回溯历史**。
（原目标还含「回溯过滤 + 准入升级」——本次**未实现**，见 §5.6.6 与 §2「已知缺口」。）

**5.6.2 数据来源（精确映射到仓库）**

|实体|来源|代码落点|
|---|---|---|
|A 系统 success|run 结束 `loop.run_task` 返回的 `result["success"]`（与 `tasks/<tid>/<run_id>.run.json` 同源）|`tool_loop._finish` → `router_runtime._dispatch_chat`|
|B 模型自报|本轮 assistant 结论文本（session 末条 `assistant.content`）|`_dispatch_chat.conclusion`|
|C₁ 对话批准|同 session 中 agent 轮后的下一条 `user` 消息|按 session 记录顺序配对|
|C₂ 观测评审|**终态观测原文**，来自 `tasks/<tid>/<date>_<run_id>.jsonl`|`signals.read_terminal_observation(task_id, run_id)`|
|配对键|`task.json` 持 `{project_id, session_id}`|`task_store.TaskStore`|

> **实现校正（2026-09-19 真机验证发现）**：TrajectoryStore 实际落盘名为
> `<date>_<run_id>.jsonl`（见 `omni_core/local/trajectory.py:129`），
> **不是** `trajectory.jsonl`；`runtime_paths.task_trajectory()` 指向的后者并不存在，
> 早期实现据此读取导致 C₂ 恒为 `unknown`。现按 run_id glob 定位，并回退通用名/最新 jsonl。
> 观测仅取 `state_text` / `facts` / 工具原始 stdout，**排除** `observation.objective`
> （否则目标词必然自命中 → C₂ 恒判 `success`）与 brain 推理行（`kind`/`role`，红线③）。

**5.6.3 新增模块 `omni_core/local/signals.py`**（已落地；纯函数 + 分级判定，零写入任务原始数据）：

分类器采用**两级**：默认启发式（确定性、零额外 LLM 成本、可单测），brain 可用时用 LLM 覆盖；
LLM 失败静默回退启发式，并置模块级熔断 `_LLM_CLASSIFIER_BROKEN`，避免 brain 不可达时每轮干等超时。

- `classify_c1(agent_msg, next_user_msg, classifier=None)` → `approved`/`refuted`/`new_task`/`ambiguous`（启发式；`classifier` 可注入）。
- `classify_c2(objective, terminal_observation, classifier=None)` → `success`/`fail`/`unknown`；**仅** objective + 终态观测，绝不给轨迹/推理/结论。
- `classify_c1_llm(...) / classify_c2_llm(...)`：复用 `config["brain"]` 通道（LLM 级），返回 `None` 表示不可用 → 回退启发式。
- `consistency_contribution(a_success, c_label) -> dict`：返回 `{fake_success, miss_rate, divergence, uncertain, n}`，对应 §5.3。
  **校正**：`divergence` 仅在 C 可判（`approved`/`success`/`refuted`/`fail`）时计入；
  `C=unknown/ambiguous` 只记 `uncertain`，不虚高分歧率（早期实现误记，已修）。
- `calibrate_c1c2(sample) -> float`：C₃ 人工抽检 vs 分级不一致率（≤10% 方采信）。
- 采集/存储：`collect_run_signal(...)`（后端内嵌入口）、`write_run_signal`、`read_run_signal`、
  `update_aggregate`（按 `seen_runs` 幂等，因信号文件先于聚合写出，**不可回读文件判定是否已计**）、
  `read_terminal_observation(task_id, run_id)`、`query_signals(task_id)`、`query_summary()`。
- 配置：`runtime.signals.llm_classifier`（默认 `true`）、`brain.classify_timeout`（默认 15s）。

**5.6.4 派生信号存储**（可再生，task 私有，不碰 memory/ 原始层）：
- `tasks/<tid>/<run_id>.signal.json`：`{a, b, c1, c2, mode, labels, consistency, ts}`（实时内嵌采集写入；与同 run_id 的 `.run.json`、trajectory 同目录配对）。
- `memory/signals_aggregate.json`（前向内嵌采集持续累加，含按模式的假成功率 / 漏报 / 分歧 / 不可判 + C₁·C₂ 分布；`GET /signals/summary` 直接读此）。
- `timeline`：每 run 追加一点 `{ts, c1, c2, a, mode}`（上限 500），供 K5 介入频率衰减曲线按滚动窗口平滑绘制（见 §8）。

**5.6.5 后端端点**（沿用 `router_runtime.py` `/api/runtime` 前缀）：
|方法/路径|行为|
|---|---|
|`GET /signals?task_id=`|该任务各 run 的三实体一致率 + 标签|
|`GET /signals/summary`|聚合：假成功率（分模式）、漏报率、分歧率、C₁/C₂ 校准误差、不可判占比（带时间窗平滑与置信标注）|
|`GET /signals/steady`|K5：四信号 + 域收敛状态 + 介入频率时间线（见 §8）|

**5.6.6 回溯过滤 + 准入升级（⚠️ 未实现，列为已知缺口）**：
- 本次 K2 落地范围是「信号采集 + 聚合 + 端点 + 分级判定」；下列两项**均未实现**：
- 回溯过滤（未实现）：扫描 `MEMORY.md` 与 `rollouts/*.md`，凡溯源 run 的 C 判脏（refuted/unknown 主导）→ 标记并清除（写 `memory/filtered.log`，含原内容与原因；不动 `trajectory` 原始层）。
- 准入升级（未实现）：蒸馏准入从「普通 success」升为「用户批准的 success」=`A.success AND 下一轮 session 无证伪`。开关 `runtime.curator.admit="approved_success"`（当前仍 `"plain_success"`）。
- 延后理由：两项都依赖足够的 C₁ 判脏样本与稳态数据。当前样本极少，**先攒数据再切准入**更稳妥——切早了会把正常 success 误挡，反而断掉 K1 积累。

**5.6.7 自动采集机制（纯前向、无回溯、不碰 30 天窗口）**：
- 仅实时内嵌：agent 轮结束后、下一轮 user 消息到达即自动触发 C₁ 分类；run 结束自动触发 C₂ 分类，写入 `*.signal.json` 并累加 `signals_aggregate.json`。
- **不扫描历史 `tasks/`、不回溯已被 prune 的旧 trajectory、不在意 30 天窗口**——窗口只约束 K2 诞生前且已超龄的旧数据，与（只采集新任务的）本机制无关；代码执行不受存量 / 窗口任何分支影响。
- 用户零操作：随任务自然运行自动攒信号；新环境启动即空转，无副作用。

**5.6.8 K2 验收**：新任务运行即自动产出 `*.signal.json` 并累加到 `signals_aggregate.json`（无需人工触发、无启动扫描）；`GET /signals/summary` 返回 §5.3 全部指标，校准误差 ≤10% 标「采信」；回溯过滤有日志、可审计，`trajectory.jsonl` 未改（红线③）；pytest 覆盖分类器 + `consistency_contribution` 纯函数。

**实测（2026-09-19，真实 brain + 本地 executor）**：
- 自动采集 ✅：任务结束即写出 `*.signal.json` 并累加聚合；用户零操作、无启动扫描。
- C₂ 生效 ✅：自主段任务 `a=true` → `c2=success`（修 trajectory 定位前恒为 `unknown`）。
- C₁ 生效 ✅：纠偏轮「不对，重做」→ `c1=refuted`，该 run `a=true` → **`fake_success=1`（假成功被自动抓到）**。
- 聚合/端点 ✅：`total` 递增；C₁·C₂ 分布与分模式率正确；`timeline` 逐点增长。
- pytest ✅：`tests/test_k2_k5.py` 11 项通过（夹具内禁用 LLM 分类器，保持离线确定性）。
- 未做：C₃ 人工抽检校准（样本不足，`c1_calibration_error` 仍为 `null`）。

**5.6.9 风险与缓解**：R2 分类器噪声 → 时间窗平滑 + 置信标注 + C₃ 抽检兜底；R3 自报转录损失 → 中心假设，§5.5 分支修法留待 K2 数字难看时另立。（30 天窗口不列为风险：本机制纯前向采集、不回溯历史，窗口无关。）

---

## 6. K3 · 有效性裁决

- 蒸馏产物抽检（父计划 V2）：≥3 个 rollout，facts 可溯源、lessons 归因合理、格式合规。
- ablation（父计划 V3）：同域对照，注入关 vs 开各 ≥10 次，指标=成功率/平均步数/介入率/verify 失败。**判据：步数降 ≥15% 且成功率不降。**
- 解读口径：实验组优势 = 记忆注入带来的**稳态提前**（范式推论⑤）。
- 失败归因顺序：轨迹质量（K2 应拦）→ 蒸馏无信息量（V2 应拦）→ 注入干扰（降强度/换位置）→ 该域无增益（换域或接受降级）。
- **判决分两段**：管道验证（规则蒸馏链路是否成立）/ 价值终审（K6 LLM 蒸馏后复查）——规则蒸馏产物偏薄时不得据此否定记忆价值本身。

### 6.1 详细实施计划（脚本已落地）
- **V2 抽检**：`scripts/review_rollouts.py` 已落地。抽 ≥3 个 `memory/rollouts/<tid>.md`（`--limit`，默认 3，按 mtime 倒序），复核：facts 可溯源（成功须有 trajectory 引用）/ lessons 归因合理（失败·高重试须带 reason）/ 格式合规（含 `## facts`、`## lessons`、`## user_corrections` 三段）。输出 JSON，退出码 0/1；不通过 → 回 K0/K2 修蒸馏，不否定记忆价值。
- **V3 ablation**：同域（project_id 锁定）对照，注入关/开各 ≥10 次（共 ≥20，纯人工长跑，数周）。
- **新增模块** `scripts/effectiveness.py` 已落地：读 `telemetry` 运行指标（`tasks/<tid>/reports/*.json`）+ `memory/signals_aggregate.json`，聚合 V3 双侧指标并套判据（步数降 ≥15% 且成功率不降），输出「通过/不通过 + 归因建议」JSON，退出码 0/1。两种模式：
  - **自动模式**（默认）：按时间序切早/晚两半，近似「记忆注入前/后」对比（无需人工打标）。
  - **显式对照**（`--ablation file.jsonl`）：每行 `{"group":"on"|"off","steps":int,"success":bool}`，严格两组，对应设计的人工各 ≥10 次。
- **验收**：V2 抽检报告 ≥3 rollout 全过；V3 双侧各 ≥10 次跑完，`scripts/effectiveness.py` 判据输出明确；失败时有归因链（落到具体层）。
- **状态（2026-09-19）**：两脚本已落地，空数据下优雅退出。**V3 判据尚未跑出可用结论**——需 ≥20 次同域对照长跑，当前样本仅个位数（自动模式的早/晚分半仅为近似，不作终审依据）。
- **风险**：人工成本/周期（≥20 次对照数周，不与 green-light 捆绑，并行于 K1 积累）；域漂移（对照须同域）。

---

## 7. K4 · 纠偏采集（增强项最高优先）

phase-1 蒸馏增加第三来源——session 中用户纠偏消息（复用 C₁ 识别证伪+纠正内容 → lessons 直接入库）。「人喂资料」最具体落点；当前蒸馏只读 world facts + run_record，**最高质量原料漏采**。

### 7.1 详细实施计划（已落地）
- **目标与依赖**：蒸馏第三来源 = session 用户纠偏消息；依赖 K2（C₁ 就绪）。
- **改动点（最小侵入，已落地）**：
  - `curator.distill_task_memory(run_record)` 读 `run_record["user_corrections"]`，rollout md 新增段 `## user_corrections`（与 `## facts`/`## lessons` 同级；无内容写 `- (暂无)`，保持段落稳定）。
  - **双层开关（红线②）**：后端 `router_runtime._collect_user_corrections()` 在 `runtime.curator.corrective_source=false`（默认）时直接返回空；Curator 侧亦以 `self.corrective_source` 二次把关——即使上游误传也不入库。
  - 透传链：`_collect_user_corrections` → `TaskSpec.corrections` → `tool_loop._finish` 写入 `run_record["user_corrections"]` → `distill_task_memory`。
- **算法**：按 session 顺序重建 `(上一轮 assistant, user)` 配对 → `classify_c1` → 仅 `refuted` 且长度 ≥4 者入列（去重保序）。
- **验收**：pytest 覆盖「默认关→纠偏不入库」与「开启→纠偏入库」两条（`tests/test_k2_k5.py`）。
- **实现校正（2026-09-19 真机验证发现）**：原 `distill_task_memory` 开头的
  `if steps < min_steps_for_skill: return None` 是**无差别短路**，导致 1 步任务中的纠偏被整条丢弃
  （真机实测最近 run 均为 `steps=1`，纠偏始终进不了蒸馏）。
  已改为：**有纠偏待入库时不短路**（纠偏是人工提供的高质量原料，价值与轨迹长度无关）；
  无纠偏仍保持原短路语义，避免短任务噪声。
- **状态（2026-09-19）：✅ 真机验证通过**
  - 开启 `corrective_source` 后跑 3 个任务 / 共 **5 条纠偏** → 全部入库到对应
    `memory/rollouts/<tid>.md` 的 `## user_corrections` 段（可溯源到 task）。
  - **零误采**：同一会话中的正常认可「好的，继续」未被采集（仅作为该轮 objective 出现在头部，
    不在 `## user_corrections` 段内）。
  - 注：验证期间 `runtime.curator.corrective_source` 已**显式置为 true**（红线②要求显式开，
    非默认生效）；如不需要可置回 false。
- **风险**：误采（C₁ 噪声 + 「含纠正内容」双条件把关）；重复（合并去重沿用现有机制）。

---

## 8. K5 · 稳态运营

§1 四信号可查询 + 域收敛判据触发蒸馏降频 + 前端域健康度面板（介入频率衰减曲线为核心演示面）。**v1 显式声明单域假设即可**（个人验证场景天然单域，多域分区列远期，不阻塞）。

### 8.1 详细实施计划（已落地）
- **目标与依赖**：四信号可查询 + 域收敛判据触发蒸馏降频 + 域健康度面板；依赖 K2（信号）+ K4（纠偏）。
- **四信号计算（新增 `omni_core/local/steady_state.py`）**：

|信号|计算|实际来源|
|---|---|---|
|蒸馏去重命中率|新蒸馏 facts 中已被 MEMORY.md 覆盖比例|Curator 蒸馏前快照比对（`_compute_dedup_hit_rate`），滚动写 `memory/curator_metrics.json`（最近 50 条），steady 取最新一条|
|skill 晋升率|全局 skills 中 `active` 占比（candidate→active 转化的近似）|`global_skills()` 目录 + `curator_metrics` 的 `skills_promoted` 序列|
|步数方差|同域任务步数离散度|`tasks/<tid>/reports/*.json` 的 `steps`（前 200 个）|
|人工介入频率|交互段 C₁ `refuted` 轮占比|K2 `signals` 聚合的 `c1_dist.refuted / n_interactive`|

  另产出 **`intervention_timeline`**：对交互段 run 按滚动窗口（默认 5）逐点算 `refuted` 占比，
  即前端「介入频率衰减曲线」的数据源（目标 ≤5%）。

  域收敛判据（初值，标「实测校准」）：去重命中率 ≥80% 且晋升率趋平 且介入频率 ≤5% → 判稳态。
  **实现补充**：介入频率为 `null`（无交互样本）时不强行判收敛；结果持久化 `memory/steady_state.json`。
  稳态触发后 Curator **跳过蒸馏**并打日志 `[Curator][SteadyState] 已收敛：跳过蒸馏（低频维护）`；
  未收敛时合并阈值 5，收敛后提升至 10（更稀疏合并）。
- **接口与前端**：`GET /signals/steady`（四信号 + 时间线 + 收敛状态 + 各项 check）；SkillsAndTools 新增「稳态」Tab：四信号 chips + 收敛徽标 + 介入频率衰减曲线。**曲线为自绘 SVG，未引入第三方图表依赖**。v1 单域：域显示为常量 `single-domain(v1)`（未接 project_id slug，多域分区远期）。
- **验收**：模拟一域收敛 → 判据触发 → 蒸馏自动降频（Curator 日志可见）；`GET /signals/steady` 四信号可查；前端曲线渲染正确。
- **状态（2026-09-19）：✅ 收敛降频已验证（模拟法）**
  - 四信号均有值、时间线正常增长、`converged=false`，三项判据逐项可见。
  - **验证过程**：备份真实信号 → 伪造收敛态（去重命中率 0.92、介入频率 1/40 = 0.025）
    → `evaluate_steady()` 判 **`converged=true`**（`dedup_ge_80` / `promotion_flat` /
    `intervention_le_5` 全 True）→ 跑一个「未收敛时必然产生 rollout」的纠偏任务
    → **rollout 未被创建**，且 Curator 打印
    `[Curator][SteadyState] 已收敛：跳过蒸馏（低频维护）`（2 条，日志可见）
    → 复原真实信号，`converged` 回到 `false`。
  - 注：后端日志为 **GBK 编码**（Windows 控制台重定向），以 UTF-8 读取会显示乱码，
    排查时按 GBK/cp936 解码。
  - 剩余未验：**真实自然收敛**（非模拟）场景 —— 需长期积累到判据自然满足。
- **风险**：单域假设（v1 显式声明，多域分区远期）；判据初值漂移（标「初值，实测校准」，曲线与置信同呈）。

---

## 9. 与 M5 的关系及对外口径

M5（权重级）冻结至 K3 通过；解冻后语料优先级 = C₁ 批准对话（K2 产出）＞ C₂ 验证自主轨迹；K5 稳态判据作为触发策略 A 的输入之一。

**对外口径**：讲「**人机协同蒸馏至稳态**」，不讲「自主自进化」——可防御、可演示（介入衰减曲线）、与诚实边界一致。

### 业界坐标（2026-09 核实；用途 = 学习定位）
|对比方|他们的位置|我们的差异点|
|---|---|---|
|ChatGPT / Claude Memory|消费级记忆已大规模 shipped（云端）|本地优先 + agent 行为维度 + rollout 溯源|
|Mem0|记忆基础设施（无 agent 循环、LLM 蒸馏 + 多信号检索）|C₁ 用户批准准入、稳态指标、效果先验证；无检索层是已知天花板（单域成立）|
|Letta|Context Repositories ≈ MEMORY.md；sleep-time compute ≈ Curator 蒸馏|多了「用户在环批准 + 稳态工程化」，少了其 RL 前沿（对应 K6/M5 远期）|

---

## 10. 红线（贯穿全部 K 系列）
1. 内核零场景硬编码（注入内容全部来自运行时数据）；
2. 默认关 → 显式开（「默认关」只约束注入不约束查看）；
3. 原始层忠实、提炼层可再生（任何 K 不得破坏 trajectory 只读性）；
4. 用户直写唯一入口 `PUT /memory`，不校验内容（人可手改是定案），操作留审计行；
5. 对外口径统一（§9）。

---

## 11. 可行性分析（green-light 前评估，2026-09-18；部分结论已随决策更新）

> 本节为合并自原 `知识级自升级_K系列_可行性分析.md` 的评估，立场「只评估可行性，不实施」。其中 K0/K1 状态、K2 采集方式已随本文决策更新（见 §2）。

### 11.1 结论速览
|维度|结论|
|---|---|
|K0|✅ 已完工且代码与设计逐条对得上|
|K1|✅ 已落地（分支 `refactor/agent-core-framework`），契约清晰|
|K2|🟡 可行，纯前向内嵌采集（不回溯历史、不依赖 30 天窗口）+ 分类器噪声风险|
|K3|🟡 可行，人工成本高、周期长（数周），判据明确|
|K4|🟢 可行，增强项最高优先|
|K5|🟡 可行，v1 单域假设即可|
|K6|🔮 远期可选，不阻塞主线|
|红线|✅ 全部合理工程约束，无致命矛盾|

总体可行性：高（稳定跑通基准）。最小跑通路径 `K0 → K1 → K3` 成立；风险集中在 K2/K3 信号质量与人工成本，非工程可行性。

### 11.2 K0 契合度核对（已确认）
设计 §3 四项（存储路径 / 蒸馏 / 合并 / 注入）逐条对照仓库 `acb985e` 全对得上；47 单测全绿。原评估指出的「K0 状态标实现中已滞后」已在本文 §2 修正为 ✅ 已完工。

### 11.3 逐里程碑可行性（摘要）
- K2：C₁/C₂ + 三实体模型 + 回溯过滤 + 准入升级（**纯前向内嵌采集，不回溯历史、不依赖 30 天窗口**）；中心假设有推导支撑；**风险**=分类器噪声（校准 ≤10% 方采信）。
- K3：抽检 + ablation，判据清晰；**风险**=≥20 次同域对照纯人工长跑（数周），不与 green-light 捆绑。
- K4：复用 C₁，排 K2 后，无独立基础设施；**评级**高。
- K5：v1 单域假设即可；**评级**高，依赖 K2+K4。

### 11.4 跨序列风险与脆弱点
|风险|性质|影响|缓解|
|---|---|---|---|
|无检索层|已知天花板|单域成立，多域需检索|个人验证天然单域，远期扩展|
|30 天轨迹窗口|仅约束 K2 前超龄旧数据|与本纯前向采集无关|不回溯历史，N/A（§5.6.7）|
|C₁/C₂ 校准误差|信号质量|稳态判据受扰|时间窗平滑 + 置信 + C₃ 抽检|
|ablation 人工成本|周期|K3 慢|不绑 green-light，并行 K1|
|M5 解冻依赖 K3|时序|权重级未启用|不阻塞 K 主线|

### 11.5 一致性缺口与文档 drift（需人工决策）
1. **K0 状态**：已在 §2 记为已完工。

### 11.6 建议下一步（最小跑通）
1. ~~（待决策）补 §11.5 #1 父计划引用缺口~~ ✅ 已完成（2026-09-19：父计划入仓并归档）；
2. ~~K2 纯前向自动采集（实时内嵌，无回溯）+ 切准入 + 接 `GET /signals*`~~ ✅ 已完成（**准入升级除外**，见 §5.6.6）；
3. ~~K3 ablation 并行起跑（靠 K1 真机数据）~~ 🟡 脚本已就绪，**结论待跑**（≥20 次对照）；
4. ~~K4 在 K2 后立细规启动（本文 §7 已立）~~ ✅ 代码已落地 + 单测通过（真机 5 例未验）；
5. ~~K5 在 K2+K4 后接稳态面板（本文 §8 已立）~~ ✅ 已完成（收敛降频真机触发未验）；
6. M5 解冻等 K3 通过。

---

## 12. 跨 K 风险总表与 green-light 顺序

**跨 K 风险总表**（与 §11.4 对齐）：无检索层（单域成立）/ 30 天窗口（仅约束 K2 前超龄旧数据，纯前向采集 N/A）/ C₁/C₂ 校准误差（平滑+抽检）/ ablation 成本（并行不绑）/ M5 冻结待 K3。

**红线对照（每 K 自查）**：内核零场景硬编码（信号/稳态全来自运行时数据）；默认关→显式开（K4 `corrective_source`、K2 准入开关默认保守）；原始层忠实（`trajectory.jsonl` 只读，信号/聚合为派生层可再生）；`PUT /memory` 不校验；对外口径统一「人机协同蒸馏至稳态」。

**green-light 顺序（最小跑通）**：
1. K2 信号采集 = 后端启动自动补齐 + 实时内嵌（无手动脚本）；切准入「approved_success」+ 接 `GET /signals*`。
2. K3 ablation 并行起跑（靠 K1 真机数据）。
3. K4（高优先增强）在 K2 后启动（§7）。
4. K5 在 K2+K4 后接稳态面板（§8）。
5. M5 解冻等 K3 通过。

> （注：部分内容可能由 AI 生成）
