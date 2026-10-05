> ✅ **本文件（编码规范）在 X2→X3 升级后仍 100% 适用，属 X3 现行工程规范基线。** 架构/设计决策以 `doc/agent-control-master-spec-2026-07-26.md`（X3 架构权威）为单一事实来源；本文件被其引用为工程约束（禁止硬编码 / Type Hints / Google Docstring / 指定异常 / 配置从 config.yaml / 临时文件入 temp/ / 低耦合 / 相对路径）。

# 编码规范 | OmniAgent X3
- 缩进：4 空格
- 行宽 ≤ 120
- 函数必须加 Type Hints
- 函数必须加 Google Docstring
- 禁止裸 except，需指定异常类型（如 `except Exception as e`）
- 配置必须从 config.yaml 读取（使用 PyYAML）
- 临时文件必须放入 temp/
- 模块低耦合，核心逻辑与工具函数分离
- 模型加载路径使用相对路径（`./models/xxx`），禁止绝对路径
- 推理参数（如 `max_new_tokens`、`conf_threshold`）必须从 config.yaml 读取

## 沿用自 X2 的工程约束（X3 现行有效）

以下条目原散见于 X2 时代的 /spec/ 规范（core_constraints / test_flow / project_structure）。其**架构相关**部分（X2 管线、插件接口、仅 GGUF 离线、HF→LoRA→GGUF 标准化）已被 X3 推翻；**工程约束**部分仍适用，合并于此，避免散落旧文档：

- 禁止硬编码：所有参数从 `config.yaml` 读取（PyYAML）。
- 必须使用 Type Hints + Google 风格 Docstrings。
- 必须包含完整异常捕获（禁止裸 `except`，指定异常类型）。
- 测试与运行必须使用项目虚拟环境 `./.venv`。
- 路径约定：执行轨迹等数据落 `apps/<app_id>/data/trajectory.jsonl`；`models/` 用相对路径。
- 模块边界：`backend/` / `model_hub/` / `web/` / `llm_runtime/` 各自职责清晰；`backend` 与 `model_hub` 不 import `web/` 的 Python 模块（`ui/` 已于 X2 重组删除）；`web/` 只经 HTTP 与后端交互。
- 临时文件入 `temp/`，优先相对路径、保持模块低耦合。

## 新增「平面」（文件 / 数据面）的准入条件（B5，2026-10-02 起）

任何新增的落盘平面（文件 / 目录 / JSON 台账）在合入前必须能回答三问，
并写在该平面的模块 docstring 或所属设计文档里（范例：`omni_core/local/fact_stats.py`）：

1. **谁写**：唯一（或枚举的）生产者是谁？运行时哪条链路触发写入？
2. **谁读**：消费方是谁？没有读者 = 死平面，不许合入（历史教训：
   `profile_candidates.md` 曾是无生产者死平面、`load_profile_text` 断言零调用被实测推翻）。
3. **读不到会怎样**：文件缺失 / 损坏时的降级行为必须显式定义（通常：空值 + 零影响主流程）。

另两条纪律：

- **机制跨场景复用必须重新校准并验证**：把为 A 场景校准的阈值 / 触发条件搬到 B 场景，
  必须按 B 场景实测样本重新标定，否则属于「滥用」而非「借鉴」。
- **预留机制不写代码**：设计上「暂不启用」的机制（如 skill 跨域晋升）只留文档说明，
  不实现无调用者的代码路径。
