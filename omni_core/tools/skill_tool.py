"""load_skill —— 按名加载技能指令（目录预览 + 按需加载）。

技能目录由 ``knowledge_inject.build_skill_catalog`` 以固定模板 User 消息注入；
模型认为某技能匹配当前任务时，再调本工具加载完整指令。

加载形态：有 ``playbook``（技能正文，引导型文字）返回**文字引导**；
正文为空才回退**脱敏动作序列**（归一化白名单渲染，易变值不外显）。
命中即记 ``total_uses``（簿记——否则命中率不可观测，效用淘汰没有输入）。

skill 轴消费侧：
- ``search_skill``：关键词检索（发现层；身份/晋升仍只用 entry_id 哈希）；
- ``replay_skill``：回放执行器——白名单内确定性工具按序硬重放（经 call_tool 走
  既有工具运行时，S0/S2 审批照常生效），白名单外（GUI 坐标类等）降级「参考建议」。
  价值 = 回放省步，不是注入收益。

project 优先、全局兜底：优先读所属项目技能目录 projects/<pid>/skills/
（按 task 归属解析），找不到再读全局 ~/.omniagent/skills/。
``disable_model_invocation=True`` 的技能返回不可用提示（模型不得直接调用）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from omni_core.tools.base import function_tool

#: 当前运行绑定的 task_id（供「私有优先」定位任务私有技能目录）。
#: 由 ToolLoop 每次运行前注入；空串表示未绑定（只查全局目录）。
_current_task_id: Dict[str, str] = {"value": ""}

#: 回放硬重放默认白名单（确定性工具；GUI 坐标类天然不在列 → 降参考建议）。
#: 可用 config ``skill.replay_allow_tools`` 覆盖（工具白名单制）。
_REPLAY_DEFAULT_TOOLS = ["shell_exec", "read_file", "write_file", "list_dir", "search_content"]


def set_skill_task_context(task_id: str) -> None:
    """绑定当前运行的 task_id（load_skill 私有优先需要它）。"""
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
    description=("按名称加载技能的完整指令（正文引导；无正文时返回脱敏动作序列）。"
                 "仅当技能目录摘要不足以指导执行时调用。"),
    unit="skill",
)
def load_skill(skill_name: str) -> Dict[str, Any]:
    """加载指定技能的完整指令内容（playbook 文字引导优先，空则回退脱敏动作序列）。

    优先读取所属项目技能（projects/<pid>/skills/，按 task 归属解析），找不到再兜底
    全局技能（~/.omniagent/skills/）。被标记 disable_model_invocation 的技能返回不可用提示。

    Args:
        skill_name: 技能名称（取技能目录 <available_skills> 中列出的名称）
    """
    try:
        from omni_core.local.skill_library import SkillLibrary, _normalize_args

        name = (skill_name or "").strip()
        if not name:
            return {"ok": False, "error": "skill_name 不能为空"}

        tid = current_skill_task_id()
        lib = SkillLibrary(tid)
        # 私有优先、全局兜底（SkillLibrary.load 已实现该顺序）
        skill = lib.load(name)
        if skill is None:
            return {"ok": False, "error": f"未找到技能: {name}"}
        if getattr(skill, "disable_model_invocation", False):
            return {
                "ok": False,
                "error": f"技能 {name} 已禁用模型直接调用（disable_model_invocation=true）",
                "disabled": True,
            }

        # playbook（正文引导型文字）优先；无正文回退脱敏动作序列。
        # 旧格式落盘的是结构化档案（"# Skill:" 开头），不算 playbook → 同样回退。
        body = (skill.body or "").strip()
        if body and not body.startswith("# Skill:"):
            content, mode = body, "playbook"
        elif skill.substeps:
            seq = " → ".join(
                f"{s.tool}({json.dumps(_normalize_args(s.args), ensure_ascii=False)})"
                for s in skill.substeps)
            content, mode = (seq or skill.to_markdown()), "action_sequence"
        else:
            content, mode = skill.to_markdown(), "action_sequence"

        limit = skill_load_limit()
        truncated = len(content) > limit
        if truncated:
            content = content[:limit] + f"\n\n...[已截断，原长 {len(content)} 字符，上限 {limit}]"

        # 命中簿记：只记使用、不改成败；写盘失败不影响返回
        try:
            skill.record_use()
            lib.save(skill)
        except Exception:
            pass

        return {
            "ok": True,
            "name": skill.name,
            "content": content,
            "mode": mode,
            "truncated": truncated,
            "limit": limit,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _replay_allow_tools() -> List[str]:
    """硬重放白名单（config skill.replay_allow_tools 覆盖；缺省用模块默认）。"""
    try:
        import config
        cfg = config.get_config("skill.replay_allow_tools", None)
        if isinstance(cfg, list) and cfg:
            return [str(t) for t in cfg]
    except Exception:
        pass
    return list(_REPLAY_DEFAULT_TOOLS)


def _clip_result(result: Any, limit: int = 2000) -> Any:
    """回放结果裁剪：dict/str 过大时截断为带标注的字符串（控载荷）。"""
    try:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    except Exception:
        text = str(result)
    if len(text) <= limit:
        return result
    return text[:limit] + f"...[截断，原长 {len(text)} 字符]"


@function_tool(
    name="search_skill",
    description=("按关键词检索可复用技能（匹配名称/描述/标签），返回摘要列表。"
                 "执行详情用 load_skill 加载；动作序列回放用 replay_skill。"),
    unit="skill",
)
def search_skill(query: str, limit: int = 5) -> Dict[str, Any]:
    """关键词检索技能（发现层；不参与技能身份判定）。

    Args:
        query: 关键词（匹配技能名 / 描述 / 标签，不区分大小写）
        limit: 返回条数上限（默认 5，最大 8）
    """
    try:
        from omni_core.local.skill_library import SkillLibrary

        q = (query or "").strip().lower()
        if not q:
            return {"ok": False, "error": "query 不能为空"}
        lib = SkillLibrary(current_skill_task_id())
        ranked = []
        for s in lib.list_all():
            name = (s.name or "").lower()
            routine = name.removeprefix("skill_")
            desc = (s.description or "").lower()
            tags = "|".join(s.tags or []).lower()
            tools = "|".join(sorted({st.tool for st in s.substeps})).lower()
            score = 0
            if q in name or q in routine:
                score += 3
            if q in tags:
                score += 2
            if q in desc:
                score += 1
            if q in tools:
                score += 1
            if score:
                ranked.append((score, s))
        ranked.sort(key=lambda pair: -pair[0])
        hits = [{
            "name": s.name,
            "description": s.description,
            "status": s.metadata.status,
            "scope": s.metadata.scope,
            "steps": len(s.substeps),
            "tools": sorted({st.tool for st in s.substeps}),
        } for _, s in ranked[: max(1, min(int(limit or 5), 8))]]
        return {"ok": True, "query": q, "total": len(ranked), "hits": hits}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@function_tool(
    name="replay_skill",
    description=("按名称回放技能的动作序列：白名单内确定性工具按序硬重放（每步照常"
                 "过安全审批），白名单外步骤（如 GUI 坐标类）降级为参考建议返回。"),
    unit="skill",
)
def replay_skill(skill_name: str) -> Dict[str, Any]:
    """回放执行器：可重放宏的价值 = 回放省步。

    硬重放走 ``call_tool``——与 agent 路径同一 SDK FunctionTool（wrap_tool 统一包裹），
    S0 只读门 / S2 审批卡照常生效，回放不是免检通道。

    Args:
        skill_name: 技能名称（取技能目录或 search_skill 结果中的名称）
    """
    try:
        from omni_core.local.skill_library import SkillLibrary
        from omni_core.tools.base import call_tool

        name = (skill_name or "").strip()
        if not name:
            return {"ok": False, "error": "skill_name 不能为空"}
        lib = SkillLibrary(current_skill_task_id())
        skill = lib.load(name)
        if skill is None:
            return {"ok": False, "error": f"未找到技能: {name}"}
        if getattr(skill, "disable_model_invocation", False):
            return {"ok": False, "error": f"技能 {name} 已禁用模型直接调用", "disabled": True}
        if not skill.substeps:
            return {"ok": False, "error": f"技能 {name} 无可回放动作序列（纯引导型技能用 load_skill）"}

        allow = set(_replay_allow_tools())
        steps_out = []
        replayed = 0
        for i, st in enumerate(skill.substeps, 1):
            if st.tool in allow:
                result = call_tool(st.tool, dict(st.args or {}))
                replayed += 1
                steps_out.append({"step": i, "tool": st.tool, "args": st.args,
                                  "replayed": True, "result": _clip_result(result)})
            else:
                steps_out.append({
                    "step": i, "tool": st.tool, "args": st.args, "replayed": False,
                    "reference": f"{st.tool}({json.dumps(st.args, ensure_ascii=False)})",
                })
        # 命中簿记：只记使用，不改成败（成败由任务结果经 record_success/failure 记）
        try:
            skill.record_use()
            lib.save(skill)
        except Exception:
            pass
        reference = len(steps_out) - replayed
        return {"ok": True, "name": skill.name,
                "replayed_steps": replayed, "reference_steps": reference,
                "steps": steps_out}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
