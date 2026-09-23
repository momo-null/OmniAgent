"""§3.4 知识层注入：纯函数解耦，支持无磁盘单测。

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

from omni_core.local.runtime_paths import memory_summary, task_skills, global_skills
from omni_core.local.skill_library import SkillLibrary


# 弱注入弱化提示（固定文案，避免历史经验喧宾夺主）
_WEAKEN_HINT = "历史记忆/技能（可能过时，以实际观测为准）"

_SUMMARY_TRUNCATE = 20000
_SKILL_OP_LIMIT = 12


def load_memory_text() -> str:
    """读取注入视图 summary 并截断管控；文件不存在/读取异常返回空。"""
    try:
        p = memory_summary()
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")[:_SUMMARY_TRUNCATE]
    except Exception:
        return ""


# --- F4.1b/F4.2：注入块组装（纪律文件 → system；记忆 → 会话流尾部） -----------
# 放置原则（按「授权面 + 变更面」，2026-09-21 定论）：
#   * 用户单写、run 内不变的**稳定纪律**（AGENTS.md）→ system prompt
#     （缓存命中 + 覆盖语义正确「用户当轮指令覆盖一切」+ 注入面收敛）；
#   * agent 自维护的**增长内容**（memory）→ 会话流（尾部重插），可被压缩管控。
_MEMORY_HEADER = (
    "# 运行期记忆（尾部注入）\n"
    "以下为运行期注入的历史记忆，仅作参考（可能过时，以实际观测为准）；"
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


def load_matching_skills(objective: str, task_id: str, limit: int = 3) -> List[Dict[str, Any]]:
    """基于当前任务弱匹配召回技能；异常兜底返回空列表。

    严格传递真实 task_id（SkillLibrary 内部已做路径安全校验），规避路径报错。
    上限 ``limit`` 条，技能条目统一抽取「名称 + 匹配规则 + 核心操作序列」。
    """
    try:
        if not objective:
            return []
        lib = SkillLibrary(task_id=task_id)
        matched = lib.find_by_pattern(objective)
        out: List[Dict[str, Any]] = []
        for s in matched[:limit]:
            steps = getattr(s, "substeps", None) or []
            ops = " → ".join(getattr(st, "tool", "") for st in steps[:_SKILL_OP_LIMIT])
            out.append({
                "name": getattr(s, "name", ""),
                "objective_pattern": getattr(s, "objective_pattern", ""),
                "description": getattr(s, "description", ""),
                "ops": ops,
            })
        return out
    except Exception:
        return []


def build_knowledge_block(memory_text: str, skills: List[Dict[str, Any]]) -> str:
    """组装知识注入块；无内容返回空字符串（调用方自动跳过）。

    输出携带弱化提示，技能条目统一格式化：名称 + 匹配规则 + 核心操作序列。
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


def build_skill_catalog(task_id: str, desc_limit: int = 300) -> List[Dict[str, Any]]:
    """枚举技能目录供模型按需加载（T2.4）。

    优先级：任务私有技能（tasks/<task_id>/skills/）> 全局技能（~/.omniagent/skills/），
    同名私有覆盖全局。解析每个技能 frontmatter 的 ``disable_model_invocation`` 布尔字段
    （默认 False，脏值视为 True）→ 为 True 的技能不出现在目录（不可被模型直接调用）。

    返回结构化目录列表（每项含 name/description/objective_pattern），description 截断管控。
    异常兜底返回空列表，保证注入失败不影响主流程。
    """
    try:
        seen: set = set()
        catalog: List[Dict[str, Any]] = []
        # 私有目录优先（先列，先进入 seen，后续同名的全局被跳过）；
        # 携带 source 标签（task/global）供埋点统计各来源条数。
        for d, src in ((task_skills(task_id), "task"), (global_skills(), "global")):
            if not d.exists():
                continue
            for f in sorted(d.glob("*.md")):
                if f.name in seen:
                    continue
                seen.add(f.name)
                s = SkillLibrary._parse(f)
                if s is None:
                    continue
                if getattr(s, "disable_model_invocation", False):
                    continue
                desc = (s.description or s.objective_pattern or "")[:desc_limit]
                catalog.append({
                    "name": s.name,
                    "description": desc,
                    "objective_pattern": s.objective_pattern,
                    "source": src,
                    "disable_model_invocation": False,
                })
        return catalog
    except Exception:
        return []


def format_skill_catalog_message(catalog: List[Dict[str, Any]]) -> str:
    """把目录渲染成固定模板 User 消息（T2.4：目录不再注入 system，改为 User 消息）。"""
    if not catalog:
        return ""
    lines = ["<available_skills>"]
    for s in catalog:
        desc = s.get("description") or s.get("objective_pattern") or ""
        lines.append(f"- `{s.get('name', '')}`: {desc}")
    lines.append("</available_skills>")
    lines.append(
        "以上为可用技能目录（仅摘要）。若任务与某技能描述匹配，请先调用 load_skill 工具加载其"
        "完整指令再行动；不得仅凭摘要推断或执行技能内容。"
    )
    return "\n".join(lines)
