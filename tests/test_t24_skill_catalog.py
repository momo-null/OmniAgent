"""T2.4（O4'）：Skill 目录化 + load_skill 加载工具验收。

覆盖：目录枚举（私有优先/全局兜底/描述截断）、disable_model_invocation 隐藏与
拒绝加载、目录 User 消息插入位置、load_skill 完整加载与截断、总开关关闭回归。
"""
import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.local.knowledge_inject import (
    build_skill_catalog,
    format_skill_catalog_message,
)
from omni_core.local.runtime_paths import DEFAULT_PROJECT_ID, global_skills, project_skills
from omni_core.tools.base import TOOL_REGISTRY, build_plugin_registry
from omni_core.tools.skill_tool import load_skill, set_skill_task_context


def _write(dirpath, filename, name, desc, body="技能正文", disable=None):
    dirpath.mkdir(parents=True, exist_ok=True)
    disable_line = "" if disable is None else f"disable_model_invocation: {disable}\n"
    (dirpath / filename).write_text(
        "---\n"
        f"name: {name}\n"
        f"objective_pattern: {desc}\n"
        f"description: {desc}\n"
        f"{disable_line}"
        "tags_json: []\n"
        "substeps_json: []\n"
        "metadata_json: {}\n"
        "---\n\n" + body,
        encoding="utf-8",
    )


def _gate(ok=True):
    return type("G", (), {
        "verify_done": lambda self: (ok, "ok"),
        "verify": lambda self, c: (ok, "ok"),
        "peek": lambda self: (False, ""),
        "note": lambda self, t: {"ok": True},
        "verify_count": 0,
        "has_condition": False,
        "no_confidence": False,
    })()


# === 1. 目录枚举 / 私有优先 / 描述截断 ========================================
def test_catalog_private_overrides_global():
    tid = "t24_pri"
    _write(project_skills(DEFAULT_PROJECT_ID), "demo.md", "demo", "私有版本描述")
    _write(global_skills(), "demo.md", "demo", "全局版本描述")
    _write(global_skills(), "only_global.md", "only_global", "仅全局技能")

    cat = build_skill_catalog(tid)
    names = [s["name"] for s in cat]
    assert "demo" in names
    assert "only_global" in names
    demo = next(s for s in cat if s["name"] == "demo")
    assert "私有" in demo["description"]  # 同名 project 覆盖全局


def test_catalog_truncates_description():
    tid = "t24_trunc"
    _write(project_skills(DEFAULT_PROJECT_ID), "t.md", "t", "描" * 500)
    cat = build_skill_catalog(tid, desc_limit=50)
    desc = next(s for s in cat if s["name"] == "t")["description"]
    assert len(desc) == 50


# === 2. disable_model_invocation ============================================
def test_disabled_skill_hidden_and_load_rejected():
    tid = "t24_dis"
    _write(project_skills(DEFAULT_PROJECT_ID), "hidden.md", "hidden", "禁用技能", disable="true")
    _write(project_skills(DEFAULT_PROJECT_ID), "ok.md", "ok", "可用技能")

    names = [s["name"] for s in build_skill_catalog(tid)]
    assert "hidden" not in names
    assert "ok" in names

    set_skill_task_context(tid)
    res = load_skill("hidden")
    assert res["ok"] is False
    assert "禁用" in res["error"]


def test_malformed_disable_value_treated_as_true():
    tid = "t24_bad"
    _write(project_skills(DEFAULT_PROJECT_ID), "weird.md", "weird", "脏值技能", disable="maybe")
    assert "weird" not in [s["name"] for s in build_skill_catalog(tid)]


# === 3. 目录消息为 User 角色且插在 history 之后、user_input 之前 ==============
def test_catalog_message_is_user_role_and_inserted_in_order(monkeypatch):
    captured = {}

    class _FakeRes:
        def to_input_list(self):
            return list(captured["items"])

    class _FakeRunner:
        @staticmethod
        def run(agent, items, **kw):
            captured["items"] = list(items)

            async def _c():
                return _FakeRes()

            return _c()

    monkeypatch.setattr(sl, "Runner", _FakeRunner)
    catalog = format_skill_catalog_message(
        [{"name": "a", "description": "desc-a", "objective_pattern": ""}])

    sl.run_subtask_sdk(
        brain={"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
        instructions="sys",
        user_input="本轮问题",
        tools=[],
        gate=_gate(),
        history_items=[{"role": "user", "content": "历史消息"}],
        skill_catalog=catalog,
        max_steps=5,
    )

    items = captured["items"]
    contents = [i.get("content", "") for i in items]
    roles = [i.get("role") for i in items]
    assert contents[0] == "历史消息"
    assert "<available_skills>" in contents[1]
    assert contents[2] == "本轮问题"
    assert roles == ["user", "user", "user"]


# === 4. load_skill 加载完整内容 / 超长截断 ===================================
def test_load_skill_returns_full_content():
    tid = "t24_load"
    _write(project_skills(DEFAULT_PROJECT_ID), "full.md", "full", "完整技能", body="正文ABC")
    set_skill_task_context(tid)
    res = load_skill("full")
    assert res["ok"] is True
    assert "正文ABC" in res["content"]
    assert res["mode"] == "playbook"  # §3.2：正文即 playbook 文字引导
    assert res["truncated"] is False


def test_load_skill_truncates_long_content():
    tid = "t24_long"
    _write(project_skills(DEFAULT_PROJECT_ID), "big.md", "big", "超长技能", body="字" * 20000)
    set_skill_task_context(tid)
    res = load_skill("big")
    assert res["ok"] is True
    assert res["truncated"] is True
    assert len(res["content"]) <= res["limit"] + 100
    assert "已截断" in res["content"]


def test_load_skill_falls_back_to_sanitized_action_sequence():
    """§3.2：无 playbook（正文）→ 回退脱敏动作序列（A1 归一化，易变值不外显）。"""
    from omni_core.local.skill_library import Skill, SkillLibrary, SkillSubstep

    tid = "t24_seq"
    lib = SkillLibrary(task_id=tid)
    lib.save(Skill(
        name="skill_seq", objective_pattern="输入并提交",
        substeps=[SkillSubstep(tool="type_text", args={"text": "机密内容"}),
                  SkillSubstep(tool="click", args={"x": 3, "y": 4})],
    ))
    set_skill_task_context(tid)
    res = load_skill("skill_seq")
    assert res["ok"] is True
    assert res["mode"] == "action_sequence"
    assert "type_text" in res["content"] and "click" in res["content"]
    assert "机密内容" not in res["content"]  # 归一化：<text>


def test_load_skill_records_total_uses():
    """§3.2 命中簿记：load_skill 成功即记 total_uses（效用淘汰的输入）。"""
    from omni_core.local.skill_library import SkillLibrary

    tid = "t24_use"
    _write(project_skills(DEFAULT_PROJECT_ID), "used.md", "used", "簿记技能")
    set_skill_task_context(tid)
    load_skill("used")
    load_skill("used")
    s = SkillLibrary(task_id=tid).load("used")
    assert s.metadata.total_uses == 2


# === 5. skill 是 core 能力：常开，不再有分组总开关 ==========================
def test_skill_tool_always_registered():
    """分组门禁已删（门禁＝环境单选 + 插件开关）；skill 属 core，永远在注册表里。"""
    reg = build_plugin_registry()
    assert "load_skill" in reg.names()
    assert TOOL_REGISTRY["load_skill"].unit == "skill"


def test_empty_catalog_produces_no_message():
    assert format_skill_catalog_message([]) == ""
    assert format_skill_catalog_message(
        build_skill_catalog("t24_nonexistent_task")) == ""
