# 向量召回臂（vector-recall）设计方案

> 分支 `feat/vector-recall`（自 main@`d61496c` 切出）。分支纪律：每步可验证、
> **A/B 报告达标才合回 main**。本文是该分支的实施规格；过程记录走 worklog。
> 关联：memory-architecture.md §2.2「检索升级路径」——其触发条件
> 「实测同义漏召回」已于 2026-11-13 用三个转述案例满足（§1.1），正式立项。

## 1. 背景与实测依据

### 1.1 同义漏召回实测（2026-11-13，真库转述问句）

| 问句 | 期望记忆 | 实际 |
|---|---|---|
| 「白色的鞋子」 | 白色带蝴蝶结玛丽珍鞋（doro 穿搭） | ❌ 未命中（词面不相交） |
| 「二手的裙子是不是更划算」 | 日牌保值/中古原子 | ⚠️ 正确记忆在 top2，top1 被泛域原子抢占 |
| 「手作的东西能不能拿去卖」 | 手作/山正原子 | ❌ top1 跑偏（AI 长片原子） |

对照（词面命中区工作正常）：「玛丽珍鞋」直击 ✓、「统计假设检验」转述命中 ✓（该记忆
措辞与问句恰好同构）。结论：**词法单通道的失败模式 = 措辞不相交**，与 11-12 深夜
「白鞋 vs 白色带蝴蝶结玛丽珍」案例同根因。

### 1.2 测量尺过期（伴随问题）

P0 分布断言（40 样本）锚定 2026-11-02 的旧语料（ETF/工程域），三项 FAIL 大半是
「问库里已不存在的内容」。本分支 S1 先把测量尺改成**自锚定**（从现库采样），否则
向量臂的收益无法被诚实地度量。

## 2. 目标与非目标

**目标**：转述/同义问句在 L0（chat+observation）与 L1（atoms）的召回命中率达到
可验证的提升；FTS 通道与全部既有不变量零退化；语音链路零感知。

**非目标**：不退役 FTS（精确词/编号/专名唯一可靠通道 + 降级底线，见 §7）；不做
cross-encoder 重排（phase 2 备选）；不动 narrative/scene（量小、词面够）；不做
隐性触发（「她没发起检索」问题，已挂起的独立项）；不做云 API。

## 3. 架构总览

```
查询 q ──┬─ FTS 通道（现状，simple 分词）──────────┐
         │                                        ├─ RRF(k=60) 融合排序
         └─ 向量通道：embed(q) → 余弦 top-2k ──────┘        ↓
                                              floor / 去冗余 / 轮换环（原样保留）
```

- **只做候选生成**：向量是并列的候选通道，不引入语义规则；融合后的机制排序
  （floor/去冗余/轮换/加权不过滤）一字不改（INV-3/INV-14 同纪律）。
- **接入点**：`memory_brain.search_l0()`（conversation_search / observation_search
  底座）与 `search_brain()`（memory_recall 底座）。narrative/scene 二期再议。
- **融合算法**：RRF `score(d) = Σ 1/(k + rank_i(d))`，k=60，两通道各取 top-2k 进融合。

## 4. 嵌入服务（`omni_core/local/embedder.py`）

- **模型**：默认 `bge-small-zh-v1.5`（BAAI，中文专精，**24M 参数 / 512 维 / ~0.1GB
  显存 / CPU 单条 5-20ms**）——在她的规模（短文本、中文查询、库为千级）下质量足够
  且资源占用低一个数量级；`bge-m3`（568M/1024 维/多语）作为**配置升级选项**——
  语料含日文专名（品牌/地名）是 m3 的相对优势，A/B 报告显示小模型天花板不够时
  一键换模型（换模型自动重嵌，§5）。模型文件放 model_hub 约定目录，
  **不进 requirements 主链**（C-2：sentence-transformers/torch 未装 →
  `ready()=False` → 全链静默降级 FTS-only，工具照常返回）。
- **设备降级链（配置驱动）**：
  ```
  knowledge.relations.vector.device = auto | gpu | cpu   （缺省 auto）
  auto: CUDA 可用 且 空闲显存 ≥ 阈值(1.0GB，给语音引擎留共存空间) → GPU(fp16)
        否则 → CPU（small-zh 在 CPU 上 5-20ms/条，完全可用）
  gpu:  尝试 CUDA；加载失败/超时 → 降 CPU 并记日志
  cpu:  强制 CPU
  运行中：encode 抛 CUDA OOM → 自动落 CPU 重试并钉住（本进程内不再上 GPU）
  任何一层不可用 → ready()=False → FTS-only
  ```
- **显存共存分析**：5060 8GB 上还常驻语音引擎（GPT-SoVITS）。small-zh 约 0.1GB——
  与语音共存几乎零竞争，`auto` 的显存探测只是防御位；若升级 bge-m3（fp16 约
  1.5GB），探测阈值的意义才真正显现，OOM 回落 CPU 是兜底而不是异常。
- **进程内单例懒加载**（网关/后端各自一份；首条查询时加载，之后常驻）。
- **query 嵌入延迟**：GPU 1-3ms / CPU 5-20ms——尾巴无感；写入即嵌也因此重新
  可行，但欠账模型（§5）仍保留为更稳形态。

## 5. 存储与欠账模型

- **新表**（brain.db 同库，schema 版本推进）：
  ```sql
  CREATE TABLE vectors(
    source TEXT NOT NULL,      -- 'l0' | 'atom'
    record_id TEXT NOT NULL,
    vec BLOB NOT NULL,         -- float32 little-endian
    model TEXT NOT NULL, dim INTEGER NOT NULL,
    created TEXT NOT NULL,
    PRIMARY KEY(source, record_id)
  );
  ```
- **写入零开销**：`index_l0_message` / `_insert_atom` **不**同步嵌（append_message
  在语音主链路上）。欠账 = `l0_messages`/`atoms` 有行而 `vectors` 缺行。
- **补齐时机**（两路，均不占主链）：
  1. **查询前 lazy 补**：`search_l0`/`search_brain` 入口发现欠账 → 先批量补
     （≤64 条/次）再查——**保证自己写下的东西自己立刻查得到**；
  2. **心跳批嵌**：life-beat 收尾顺带消化积压（无上限，量小）。
- **模型变更**：`model`/`dim` 不匹配的旧向量视为欠账（换模型自动重嵌）。

## 6. 检索融合细节

- `search_l0(keyword, ...)` / `search_brain(query, ...)`：模型就绪时，query 向量化 →
  `vectors` 余弦 top-2k（含 lazy 补齐）→ 与 FTS 结果 RRF 融合 → 交给既有
  floor/去冗余/轮换。模型未就绪 → 现状路径，零行为差异。
- **不变量保持**：融合只是改变了**候选的来源与排序**——floor 阈值、`(kind, peer,
  content[:12])` 去冗余、轮换环、加权不过滤（INV-3）、state='active' 过滤全部原样。
- **成本**：向量通道 <20ms（numpy 暴力，万级内）+ query 嵌入（§4）。

## 7. 为什么 FTS 不退役（定稿）

1. **精确词召回**：BV号、自建记忆标记（「ZQ7-沙漏-1126」）、牌子（ap15/Baby）、
   店名（Closet Child）——embedding 子词平滑会淹没精确 token，词法通道不可替代；
2. **降级底线**：C-2 链的终点；
3. **可解释性**：FTS 命中可指认原文位置，符合 INV-14 追溯纪律；
4. 业界定论：BM25+向量混合各补对方失败模式（Elastic/Azure/Weaviate 默认形态）。
   「simple」分词器实现本身可换（simple|trigram 配置位），通道不退役。

## 8. 回填与增量

- 存量：`scripts/backfill_vectors.py`（全量欠账补嵌，GPU 分钟级/CPU 数分钟，
  `--dry-run` 报告欠账量）。
- 增量：§5 两路；换模型 → 旧 `model` 行整体作废重嵌。

## 9. 配置（含 Web 可见性）

| 键 | 缺省 | 说明 |
|---|---|---|
| `runtime.knowledge.memory.vector.enabled` | False | 总开关（缺省关=行为与今日完全一致；设置页「伙伴→记忆」加「向量召回」开关） |
| `runtime.knowledge.memory.vector.model` | bge-small-zh-v1.5 | 模型名（model_hub 目录解析；可切 bge-m3，换模型自动重嵌） |
| `runtime.knowledge.memory.vector.device` | auto | auto / gpu / cpu（§4 降级链） |

Web 可见性原则（configs-must-be-web-visible）：设置页记忆节加开关 + 设备下拉，
`enabled=False` 时其余键隐藏。

## 10. 验收

- **S1 测量尺自锚定**：`verify_memory_recall.py` 的 40 样本改为从现库采样生成
  （query=记忆内容的转述模板），保留原四项分布断言口径；产出 FTS-only 基线报告。
- **S2 embedder + 表 + 回填**：单测（设备降级链/缓存/欠账判定/模型换版）。
- **S3 `search_l0` 融合**：单测（RRF 排序/降级/lazy 补齐/零行为差异开关关）。
- **S4 `search_brain` 融合**：同上。
- **S5 探针进回归 + A/B 报告**：白鞋/二手/手作三案例 + 自锚定样本，
  FTS-only vs 融合对比表。
- **合入标准（三条全过才合 main）**：
  1. 自锚定样本上同义命中率显著提升（目标：三案例全中）；
  2. 词面断言不退化（原 P0 词法断言 + 直击样本）；
  3. 语音链路零感知（append_message 路径零新增耗时；开关关闭逐字节现状）。

## 11. 红线合规核对

| 红线 | 状态 |
|---|---|
| 本地优先禁云 | ✓ bge 本地推理，无任何云调用 |
| C-2 可选依赖 | ✓ torch/sentence-transformers 不进主链；未装降级 FTS |
| 只做候选生成，排序是机制 | ✓ 向量只是并列候选通道，机制排序原样 |
| 加权不过滤（INV-3） | ✓ 不新增任何 WHERE |
| 一行一条 FTS（INV-5） | ✓ vectors 表独立，不碰 atoms_fts |
| 出处追溯（INV-14） | ✓ 向量命中行照带 provenance；可解释通道（FTS）并存 |
| 真实标识不落库 | ✓ vectors 只存 record_id + 浮点向量 |

## 12. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 显存与语音引擎竞争 | auto 探测空闲显存 + OOM 自动落 CPU（§4） |
| 短文本（气泡 ≤300 字）嵌入质量 | bge-m3 对短句稳健；A/B 报告实测，不达标调 prompt 前缀（「检索意图：」）而非换模型 |
| 向量噪声挤掉词面精确命中 | RRF 双通道并列，词面 top 仍在融合池；floor 兜底 |
| 依赖体积（torch ~2GB 盘） | model_hub 约定目录 + 文档说明；不装即降级 |
| l0 无界增长 → 向量表膨胀 | 与 l0 本体同寿命；量级到 10 万再议 ANN（接口不变） |

## 13. 明确不做

- cross-encoder 重排（phase 2 备选，先量测）；
- 云 embedding API（红线）；
- 向量召回做**过滤**条件（INV-3）；对 embedding 分数做语义解释/规则化；
- narrative/scene 向量化（二期再议）；
- 隐性触发（她没发起检索的问题）——独立项，本方案不碰。
