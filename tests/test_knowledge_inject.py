"""§3.4 知识层注入单测：纯函数 + 磁盘隔离。

不依赖真实网络 / 模型 / 模拟器；用 tmp_path 隔离 ~/.omniagent。
"""
import pytest

from omni_core.local import runtime_paths as _RP
from omni_core.local.knowledge_inject import (
    build_knowledge_block,
    load_memory_text,
)
from omni_core.local.skill_library import SkillLibrary, Skill, SkillSubstep, SkillMetadata


@pytest.fixture(autouse=True)
def _iso_global(tmp_path, monkeypatch):
    """隔离 ~/.omniagent 到临时目录，测试后自动恢复。"""
    monkeypatch.setattr(_RP, "_GLOBAL", tmp_path / ".omniagent")
    _RP.ensure_global_dirs()


# === build_knowledge_block（纯函数） ========================================

class TestBuildKnowledgeBlock:
    def test_empty_returns_blank(self):
        assert build_knowledge_block("", []) == ""
        assert build_knowledge_block("  ", None) == ""

    def test_memory_only(self):
        block = build_knowledge_block("已知 A 界面有按钮", [])
        assert "历史知识参考" in block
        assert "可能过时" in block
        assert "## 历史记忆" in block
        assert "已知 A 界面有按钮" in block

    def test_skills_only(self):
        skills = [{"name": "skill_x", "objective_pattern": "点击X", "ops": "observe → click"}]
        block = build_knowledge_block("", skills)
        assert "## 相关技能" in block
        assert "skill_x" in block
        assert "点击X" in block
        assert "observe → click" in block

    def test_both_sections(self):
        block = build_knowledge_block(
            "mem", [{"name": "s", "objective_pattern": "p", "ops": "a"}]
        )
        assert "## 历史记忆" in block
        assert "## 相关技能" in block


# === load_memory_text（磁盘） ================================================

class TestLoadMemoryText:
    def test_missing_summary_returns_blank(self):
        assert load_memory_text() == ""

    def test_reads_summary(self):
        _RP.memory_summary().write_text("历史事实一\n历史事实二", encoding="utf-8")
        assert "历史事实一" in load_memory_text()


# === 技能弱匹配召回（SkillLibrary.find_by_pattern） =========================
# 旧 `load_matching_skills` 注入助手已删（技能目录 + load_skill 工具取代它）。

class TestSkillRecallByPattern:
    def test_empty_objective_returns_blank(self):
        assert SkillLibrary(task_id="t1").find_by_pattern("") == []

    def test_matches_objective(self):
        lib = SkillLibrary(task_id="t_match")
        lib.save(Skill(
            name="skill_open",
            objective_pattern="请打开应用主页并操作",
            substeps=[SkillSubstep(tool="observe", args={}), SkillSubstep(tool="click", args={"x": 1})],
            metadata=SkillMetadata(status="active"),
        ))
        matched = lib.find_by_pattern("请打开应用主页")
        assert [s.name for s in matched] == ["skill_open"]
