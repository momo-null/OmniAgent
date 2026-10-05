"""知识层注入：纯函数解耦，支持无磁盘单测。

设计要点：
- 弱注入语义：注入内容携带弱化说明「历史记忆/技能（可能过时，以实际观测为准）」，
  避免历史经验干扰实时决策。
- 三来源统一挂载 system prompt 附加块（v2 定稿，取消 A/B 测试）。
- 全部函数异常兜底返回空，保证注入失败不影响主任务执行。
- 合规红线：运行时知识注入无内核硬编码场景，omni_core 层零固定领域词汇，
  所有注入内容均来自运行时动态数据。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from omni_core.local.runtime_paths import (
    character_card, global_skills, project_skills, user_profile,
)
from omni_core.local.skill_library import SkillLibrary
from omni_core.local import llm_judge


# 弱注入措辞（「相符即沿用」——只声明沿用条件，不淡化内容价值）
_WEAKEN_HINT = "历史沉淀知识（与当前任务相符时沿用；不符时以实际观测为准）"

_SUMMARY_TRUNCATE = 20000
_PROFILE_TRUNCATE = 20000
_CHARACTER_TRUNCATE = 20000
#: 技能目录 top-k 上限（预算化召回；0 = 不限）。注入形态比检索精度更关键：
#: 目录收敛到 3 条、正文一律按需 load_skill。
_CATALOG_MAX_ITEMS = 3
#: 常驻注入**总额**预算（技能目录 + 记忆段，2000 字符）：sdk 组装路径
#: 先建目录、把余量让给记忆段；图编排回退路径等未传预算处直接用本值。
#: 送模型选档的候选上限（预算化 prompt：效用预筛后的 top-N 交模型做语义终排）
_SELECT_MAX_CANDIDATES = 60
_SELECT_FACT_MIN = 4        # 候选 ≤ 此数不发起选档调用（无可排之分）
_SELECT_SKILL_MIN = 2


def _resolve_project_id(task_id: str) -> str:
    """task → project 归属（task.json 优先，缺省 default）。失败兜底 default。"""
    try:
        from omni_core.local.task_store import TaskStore
        return TaskStore.project_of(task_id) or "default"
    except Exception:
        return "default"


def load_profile_text() -> str:
    """读取全局用户画像 user_profile.md 并截断管控；不存在/异常返回空。

    画像为**独立注入块**（与记忆段分开），携带弱注入语义；
    未落地/空文件时返回空串（零注入），不影响主流程。
    """
    try:
        p = user_profile()
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")[:_PROFILE_TRUNCATE]
    except Exception:
        return ""


def load_character_text() -> str:
    """读取角色卡 character.md 并截断管控；不存在/异常返回空。

    角色卡是**稳定人格设定**（用户单写、run 内不变），随 system prompt 注入
    （与 AGENTS.md 同类，命中前缀缓存）；未落地/空文件时返回空串（零注入）。
    """
    try:
        p = character_card()
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")[:_CHARACTER_TRUNCATE]
    except Exception:
        return ""


# --- F4.1b/F4.2：注入块组装（纪律文件 → system；辅助知识 → 会话流尾部） -----------
# 放置原则（按「授权面 + 变更面」，2026-09-21 定论）：
#   * 用户单写、run 内不变的**稳定纪律**（AGENTS.md）→ system prompt
#     （缓存命中 + 覆盖语义正确「用户当轮指令覆盖一切」+ 注入面收敛）；
#   * agent 自维护的**增长内容**（画像等）→ 会话流（尾部重插），可被压缩管控。
_MEMORY_HEADER = (
    "# 运行期知识（尾部注入）\n"
    "以下为运行期注入的辅助知识：与当前任务相符时沿用，不符时以实际观测为准；"
    "对 agent 只读，禁止写入或覆盖同名文件。"
)

_AGENTS_HEADER = (
    "# 任务纪律（AGENTS.md）\n"
    "以下为运行期纪律约束，本次运行内内容已锁定（改动自下一次运行起生效）；"
    "对 agent 只读，禁止写入或覆盖同名文件。"
)


def _sha1(text: str) -> str:
    """内容摘要（只用于「run 内是否被改动」的只读比对，不参与注入）。"""
    return hashlib.sha1(str(text or "").encode("utf-8", "replace")).hexdigest()[:12]


def _read_text(path: Any) -> str:
    """读文件文本；不存在 / 非文件 / 读取异常 → 空串（缺失也参与比对）。"""
    try:
        p = Path(str(path)).expanduser()
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


def compose_injection_block(parts: List[Tuple[str, str]], limit: int = 8192,
                            title: str = "") -> Tuple[str, List[str]]:
    """把 ``(来源标签, 文本)`` 分节拼接为注入块（块内标注来源层级），超限截断并标注。

    Args:
        title: 块首标题；缺省用记忆块标题（尾部重插路径），纪律块传入 ``_AGENTS_HEADER``。

    Returns:
        ``(block_text, labels)``；无任何内容时返回 ``("", [])``（调用方据此零注入）。
    """
    clean = [(str(lbl), (txt or "").strip()) for lbl, txt in (parts or []) if (txt or "").strip()]
    if not clean:
        return "", []
    labels = [lbl for lbl, _ in clean]
    body = "\n\n".join(f"[来源: {lbl}]\n{txt}" for lbl, txt in clean)
    block = f"{title or _MEMORY_HEADER}\n\n{body}\n"
    limit = int(limit or 0)
    if limit > 0 and len(block) > limit:
        block = block[:limit] + f"\n…[注入块已按上限 {limit} 字符截断]"
    return block, labels


@dataclass
class AgentsSnapshot:
    """F4.1b：run 级纪律文件快照——读一次、run 内**锁定**。

    设计（2026-09-21 定论）：纪律块进 system prompt，必须 run 内**逐字节稳定**
    才能全程命中前缀缓存；故 run 开始时读文件并记内容摘要，run 内不重读用于注入。

    ``check_drift()`` 仅做**只读比对**（重读文件比摘要），供发现「run 中途被改动」
    时告警——告警后仍沿用起始快照，用户改动自下一个 run 生效
    （与 Claude Code / Cursor rules「启动读取」语义对齐）。
    """

    block: str = ""
    labels: List[str] = field(default_factory=list)
    #: 参与比对的层：``(来源标签, 路径字符串)``
    layers: List[Tuple[str, str]] = field(default_factory=list)
    #: 层标签 -> 起始内容摘要（缺失文件记空串摘要，故「中途新建」同样算改动）
    digests: Dict[str, str] = field(default_factory=dict)
    limit: int = 8192

    @property
    def injected(self) -> bool:
        """块非空 = 本次运行确有纪律注入（空块 → instructions 逐字节不变）。"""
        return bool(self.block)

    def check_drift(self) -> List[str]:
        """只读比对：返回相对起始快照内容变了的层标签；无改动返回空列表。"""
        changed: List[str] = []
        for label, path in self.layers or []:
            if self.digests.get(label, "") != _sha1(_read_text(path)):
                changed.append(str(label))
        return changed

    def fingerprint(self) -> str:
        """快照指纹：纪律块内容的稳定摘要（供跨 run 比对 / 审计）。"""
        return _sha1(self.block)


def load_agents_snapshot(layers: List[Tuple[str, Any]], limit: int = 8192) -> AgentsSnapshot:
    """读取各层纪律文件（``(来源标签, 路径)``）→ 组装块 + 记录内容摘要（各读一次）。

    文件不存在 / 非文件 / 读取异常 → 该层计入比对但**不参与拼接**；
    两层均无内容 → 空块（零注入，默认行为不变）。
    """
    norm: List[Tuple[str, str]] = []
    digests: Dict[str, str] = {}
    parts: List[Tuple[str, str]] = []
    for label, path in layers or []:
        lbl = str(label)
        try:
            p = Path(str(path)).expanduser()
        except Exception:
            continue
        raw = _read_text(p)
        norm.append((lbl, str(p)))
        digests[lbl] = _sha1(raw)
        if raw.strip():
            parts.append((lbl, raw.strip()))
    block, labels = compose_injection_block(parts, limit=limit, title=_AGENTS_HEADER)
    return AgentsSnapshot(block=block, labels=labels, layers=norm,
                          digests=digests, limit=int(limit or 0))


def build_knowledge_block(memory_text: str, skills: List[Dict[str, Any]]) -> str:
    """组装知识注入块；无内容返回空字符串（调用方自动跳过）。

    输出携带「相符即沿用」措辞，技能条目统一格式化：名称 + 匹配规则 + 核心操作序列。
    操作序列参数经归一化渲染（白名单：易变值不外显，type_text → <text>）。
    """
    memory_text = (memory_text or "").strip()
    skills = skills or []
    if not memory_text and not skills:
        return ""

    parts = [f"# 历史知识参考（{_WEAKEN_HINT}）", ""]
    if memory_text:
        parts.append("## 历史记忆")
        parts.append(memory_text)
        parts.append("")
    if skills:
        parts.append("## 相关技能")
        for s in skills:
            name = s.get("name", "")
            rule = s.get("objective_pattern") or s.get("description") or s.get("name", "")
            ops = s.get("ops", "")
            parts.append(f"- **{name}**（匹配：{rule}）")
            if ops:
                parts.append(f"  操作序列：{ops}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _steps_summary(substeps: List[Any], max_steps: int = 8) -> str:
    """动作序列摘要（同一归一化白名单：易变值不外显，text → <text>）。"""
    try:
        from omni_core.local.skill_library import SkillSubstep, _normalize_args

        parts = []
        for s in (substeps or [])[:max_steps]:
            if not isinstance(s, SkillSubstep):
                continue
            parts.append(f"{s.tool}({json.dumps(_normalize_args(s.args), ensure_ascii=False)})")
        if len(substeps or []) > max_steps:
            parts.append("…")
        return " → ".join(parts)
    except Exception:
        return ""


def build_skill_catalog(task_id: str, query: str = "", desc_limit: int = 300,
                        max_items: int = 0,
                        brain_cfg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """枚举技能目录供模型按需加载。

    优先级：project 技能（projects/<pid>/skills/，按 task 归属解析）> 全局技能
    （~/.omniagent/skills/），同名/同 entry_id project 覆盖全局。解析每个技能
    frontmatter 的 ``disable_model_invocation`` 布尔字段（默认 False，脏值视为 True）
    → 为 True 的技能不出现在目录（不可被模型直接调用）。

    排序（选档交模型，不做词面打分）：
    - 无模型 / 无 query：按 N=3 晋升的效用信号（success_count 降序）稳定排序，
      同数保持枚举原序；
    - 带 query 且有 brain_cfg：LLM 选档判定相关技能并重排；调用失败 →
      保持效用序。
    截 top-k（``max_items``，缺省 ``_CATALOG_MAX_ITEMS``；0 = 用默认，负数 = 不限）。
    每条附 ``steps_summary``（归一化动作序列摘要；渲染时仅目录 top-1 外显）。
    description 截断管控。异常兜底返回空列表，保证注入失败不影响主流程。
    """
    try:
        seen_names: set = set()
        seen_entries: set = set()
        catalog: List[Dict[str, Any]] = []
        # project 目录优先（先列，先进入 seen，后续同名的全局被跳过）；
        # 携带 source 标签（project/global）供埋点统计各来源条数。
        pid = _resolve_project_id(task_id)
        for d, src in ((project_skills(pid), "project"), (global_skills(), "global")):
            if not d.exists():
                continue
            for f in sorted(d.glob("*.md")):
                if f.name in seen_names:
                    continue
                seen_names.add(f.name)
                s = SkillLibrary._parse(f)
                if s is None:
                    continue
                if s.entry_id:
                    if s.entry_id in seen_entries:
                        continue
                    seen_entries.add(s.entry_id)
                if getattr(s, "disable_model_invocation", False):
                    continue
                desc = (s.description or s.objective_pattern or "")[:desc_limit]
                catalog.append({
                    "name": s.name,
                    "description": desc,
                    "objective_pattern": s.objective_pattern,
                    "source": src,
                    "disable_model_invocation": False,
                    "success_count": int(s.metadata.success_count),
                    "steps_summary": _steps_summary(s.substeps),
                })
        # 效用信号兜底序（结构信号，脚本可算；不猜测语义相关性）；LLM 选档在其上重排
        catalog.sort(key=lambda c: -c["success_count"])
        if query.strip() and brain_cfg is not None and len(catalog) >= _SELECT_SKILL_MIN:
            ranked = _select_skills_llm(query, catalog, brain_cfg)
            if ranked:
                catalog = ranked
        if max_items >= 0:
            cap = max_items or _CATALOG_MAX_ITEMS
            catalog = catalog[:cap]
        return catalog
    except Exception:
        return []


def _select_skills_llm(query: str, catalog: List[Dict[str, Any]],
                       brain_cfg: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """LLM 选档：模型判定与目标相关的技能并重排；失败/无模型 → 空表（保持效用序）。"""
    if brain_cfg is None or not query.strip() or not catalog:
        return []
    lines = "\n".join(
        f"{i + 1}. {c.get('name', '')}: {c.get('description', '')}"
        for i, c in enumerate(catalog))
    system = (
        "你是技能选档器。给你任务目标和候选技能列表。"
        "判断哪些技能与完成该目标相关（语义相关即可，不要求字面重合），"
        "按相关度从高到低返回编号。无关技能不要返回。"
        '只返回 JSON：{"relevant": [编号...]}；没有相关的返回 {"relevant": []}。'
    )
    idx = llm_judge.rank_indices(brain_cfg, system,
                                 f"任务目标：{query}\n\n候选技能：\n{lines}",
                                 count=len(catalog))
    if not idx:
        return []
    pos = {i: p for p, i in enumerate(idx)}
    # 模型选中的按相关度序排前，未选中的保持原序跟随（稳定）
    return [c for i, c in
            sorted(enumerate(catalog), key=lambda t: (pos.get(t[0], len(idx)), t[0]))]


def format_skill_catalog_message(catalog: List[Dict[str, Any]]) -> str:
    """把目录渲染成固定模板 User 消息（目录不进 system，改为 User 消息注入）。

    仅目录 top-1 附动作序列摘要（归一化渲染，易变值不外显）；
    完整正文一律按需 load_skill。
    """
    if not catalog:
        return ""
    lines = ["<available_skills>"]
    for i, s in enumerate(catalog):
        desc = s.get("description") or s.get("objective_pattern") or ""
        lines.append(f"- `{s.get('name', '')}`: {desc}")
        if i == 0 and s.get("steps_summary"):
            lines.append(f"  操作序列摘要：{s['steps_summary']}")
    lines.append("</available_skills>")
    lines.append(
        "以上为可用技能目录（仅摘要）。技能的完整指令需通过 load_skill 工具加载；"
        "仅凭摘要推断或执行技能内容，可能得到不完整或过时的步骤。"
    )
    return "\n".join(lines)
