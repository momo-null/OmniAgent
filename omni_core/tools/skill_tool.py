"""T2.4（O4'）：load_skill —— 按名加载技能完整指令（目录预览 + 按需加载）。

技能目录由 ``knowledge_inject.build_skill_catalog`` 以固定模板 User 消息注入；
模型认为某技能匹配当前任务时，再调本工具加载其完整 frontmatter + 正文。

私有优先、全局兜底：优先读任务私有目录 tasks/<task_id>/skills/，
找不到再读全局 ~/.omniagent/skills/。``disable_model_invocation=True`` 的技能
返回不可用提示（模型不得直接调用）。
"""
from __future__ import annotations

from typing import Any, Dict

from omni_core.tools.base import function_tool

#: 当前运行绑定的 task_id（供「私有优先」定位任务私有技能目录）。
#: 由 ToolLoop 每次运行前注入；空串表示未绑定（只查全局目录）。
_current_task_id: Dict[str, str] = {"value": ""}


def set_skill_task_context(task_id: str) -> None:
    """绑定当前运行的 task_id（T2.4：load_skill 私有优先需要它）。"""
    _current_task_id["value"] = task_id or ""


def current_skill_task_id() -> str:
    return _current_task_id.get("value", "") or ""


def skill_load_limit() -> int:
    """技能正文截断上限（runtime.tools.skill_load_limit，默认 8000 字符）。"""
    try:
        import config
        return int(config.get_config("runtime.tools.skill_load_limit", 8000) or 8000)
    except Exception:
        return 8000


@function_tool(
    name="load_skill",
    description=("按名称加载技能的完整指令（frontmatter + 正文）。"
                 "仅当技能目录摘要不足以指导执行时调用。"),
    unit="skill",
)
def load_skill(skill_name: str) -> Dict[str, Any]:
    """加载指定技能的完整指令内容。

    优先读取当前任务私有技能（tasks/<task_id>/skills/），找不到再兜底全局技能
    （~/.omniagent/skills/）。被标记 disable_model_invocation 的技能返回不可用提示。

    Args:
        skill_name: 技能名称（取技能目录 <available_skills> 中列出的名称）
    """
    try:
        from omni_core.local.runtime_paths import task_skills, global_skills
        from omni_core.local.skill_library import SkillLibrary, _safe_filename

        name = (skill_name or "").strip()
        if not name:
            return {"ok": False, "error": "skill_name 不能为空"}

        tid = current_skill_task_id()
        # 私有优先、全局兜底（SkillLibrary.load 已实现该顺序）
        skill = SkillLibrary(tid).load(name)
        if skill is None:
            return {"ok": False, "error": f"未找到技能: {name}"}
        if getattr(skill, "disable_model_invocation", False):
            return {
                "ok": False,
                "error": f"技能 {name} 已禁用模型直接调用（disable_model_invocation=true）",
                "disabled": True,
            }

        # 完整原文（frontmatter + 正文）；文件缺失时用结构化重建兜底
        text = ""
        for d in (task_skills(tid), global_skills()):
            p = d / f"{_safe_filename(name)}.md"
            if p.exists():
                text = p.read_text(encoding="utf-8")
                break
        if not text:
            text = skill.to_frontmatter() + "\n\n" + (skill.body or skill.to_markdown())

        limit = skill_load_limit()
        truncated = len(text) > limit
        if truncated:
            text = text[:limit] + f"\n\n...[已截断，原长 {len(text)} 字符，上限 {limit}]"
        return {
            "ok": True,
            "name": skill.name,
            "content": text,
            "truncated": truncated,
            "limit": limit,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
