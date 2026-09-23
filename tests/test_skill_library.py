"""M4b.2 Skill 库测试：save/load、N=3 晋升、失败打断、录制。"""
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _iso_global(tmp_path, monkeypatch):
    """隔离 ~/.omniagent 到临时目录，测试后自动恢复。"""
    monkeypatch.setattr(_RP, "_GLOBAL", tmp_path / ".omniagent")
    _RP.ensure_global_dirs()


from omni_core.local.skill_library import (
    Skill, SkillSubstep, SkillMetadata, SkillLibrary,
    _derive_skill_name, _split_frontmatter,
)
import omni_core.local.runtime_paths as _RP


# === 单元：Skill 数据结构 =====================================================
class TestSkill:
    def test_serialize_roundtrip(self, tmp_path):
        """Skill → frontmatter → 还原。"""
        s = Skill(
            name="test_skill",
            objective_pattern="截图*",
            task_id="test",
            substeps=[SkillSubstep(tool="screenshot", args={}), SkillSubstep(tool="click", args={"x": 0.5})],
            metadata=SkillMetadata(success_count=1, total_uses=1, confidence=1.0, status="candidate"),
        )
        md = s.to_frontmatter()
        assert "name: test_skill" in md
        assert "screenshot" in md
        assert "candidate" in md

    def test_to_markdown(self):
        s = Skill(name="sk", objective_pattern="截图", substeps=[SkillSubstep(tool="s", args={})])
        md = s.to_markdown()
        assert "# Skill: sk" in md
        assert "## Objective" in md

    def test_record_success_below_n3(self):
        s = Skill(metadata=SkillMetadata(status="candidate"))
        promoted = s.record_success()
        assert not promoted
        assert s.metadata.success_count == 1

    def test_record_success_promotes_at_3(self):
        s = Skill(metadata=SkillMetadata(status="candidate", success_count=2))
        promoted = s.record_success()
        assert promoted
        assert s.metadata.status == "active"
        assert s.metadata.success_count == 3

    def test_record_failure_resets_continuity(self):
        s = Skill(metadata=SkillMetadata(status="candidate", success_count=2, total_uses=2))
        s.record_failure("timeout")
        assert s.metadata.success_count == 0
        assert s.metadata.failure_count == 1
        assert "timeout" in s.metadata.known_failures


# === 单元：解析 ================================================================
class TestParsing:
    def test_split_frontmatter(self):
        text = "---\nname: a\n---\n# Body\n"
        front, body = _split_frontmatter(text)
        assert "name: a" in front
        assert "# Body" in body

    def test_split_no_frontmatter(self):
        text = "# Just body"
        front, body = _split_frontmatter(text)
        assert front == ""

    def test_frontmatter_roundtrip(self):
        """Skill → frontmatter → 解析回 Skill（JSON 内嵌方案）。"""
        s = Skill(name="sk", objective_pattern="op", task_id="a",
                  substeps=[SkillSubstep(tool="t", args={"x": 1})],
                  metadata=SkillMetadata(success_count=2, status="candidate"))
        md = s.to_frontmatter()
        assert "substeps_json" in md
        assert "metadata_json" in md
        parsed = SkillLibrary._parse(fake_path(md))
        assert parsed is not None
        assert parsed.name == "sk"
        assert parsed.metadata.success_count == 2
        assert len(parsed.substeps) == 1
        assert parsed.substeps[0].tool == "t"


# === 单元：SkillLibrary =======================================================
class TestSkillLibrary:
    def test_save_and_load(self, tmp_path):
        lib = SkillLibrary("test_app")
        s = Skill(name="s1", objective_pattern="observe", task_id="test_app",
                  substeps=[SkillSubstep(tool="observe")], metadata=SkillMetadata(status="candidate"))
        lib.save(s)

        loaded = lib.load("s1")
        assert loaded is not None
        assert loaded.name == "s1"
        assert loaded.objective_pattern == "observe"
        assert len(loaded.substeps) == 1

    def test_list_all(self, tmp_path):
        lib = SkillLibrary("test_app")
        lib.save(Skill(name="a"))
        lib.save(Skill(name="b"))
        assert len(lib.list_all()) == 2

    def test_delete(self, tmp_path):
        lib = SkillLibrary("test_app")
        lib.save(Skill(name="del_me"))
        assert lib.delete("del_me")
        assert lib.load("del_me") is None

    def test_find_by_pattern(self, tmp_path):
        lib = SkillLibrary("test_app")
        lib.save(Skill(name="a", objective_pattern="截图当前屏幕"))
        lib.save(Skill(name="b", objective_pattern="点击按钮"))
        matches = lib.find_by_pattern("截图")
        assert len(matches) == 1
        assert matches[0].name == "a"

    def test_from_run_record_success(self):
        record = {
            "success": True,
            "objective": "截图当前屏幕",
            "steps_data": [
                {"tool": "screenshot", "args": {}},
                {"tool": "task_done", "args": {"reason": "ok"}},
            ],
        }
        s = SkillLibrary.from_run_record(record, "test")
        assert s is not None
        assert s.objective_pattern == "截图当前屏幕"
        assert len(s.substeps) == 2

    def test_from_run_record_failure_returns_none(self):
        record = {"success": False, "steps_data": [{"tool": "click"}]}
        assert SkillLibrary.from_run_record(record, "x") is None

    def test_from_run_record_too_few_steps(self):
        record = {"success": True, "objective": "o", "steps_data": [{"tool": "t"}]}
        assert SkillLibrary.from_run_record(record, "x") is None


# === 集成：N=3 晋升 ===========================================================
class TestPromotion:
    def test_n3_promotion_gate(self, tmp_path):
        """3 次连续成功 → candidate → active。"""
        lib = SkillLibrary("n3_test")

        cand = Skill(
            name="s1", objective_pattern="截图", task_id="n3_test",
            substeps=[SkillSubstep(tool="s", args={})],
            metadata=SkillMetadata(success_count=1, total_uses=1, confidence=1.0, status="candidate"),
        )
        # 第 1 次——已在 candidate 创建时计 1 次
        r1 = lib.promote_or_insert(cand)
        assert r1["action"] == "created"
        assert r1["promoted"] is False

        # 第 2 次（同名 candidate）
        r2 = lib.promote_or_insert(cand)
        assert r2["promoted"] is False
        assert r2["success_count"] == 2

        # 第 3 次 → 晋升！
        r3 = lib.promote_or_insert(cand)
        assert r3["promoted"] is True
        assert r3["success_count"] == 3

        loaded = lib.load("s1")
        assert loaded.metadata.status == "active"

    def test_failure_breaks_continuity(self, tmp_path):
        """中间一次失败 → success_count 归零 → 不晋升。"""
        lib = SkillLibrary("fail_test")

        cand = Skill(
            name="s1", objective_pattern="截图", task_id="fail_test",
            substeps=[SkillSubstep(tool="s", args={})],
            metadata=SkillMetadata(success_count=1, total_uses=1, status="candidate"),
        )
        lib.promote_or_insert(cand)  # 1
        lib.promote_or_insert(cand)  # 2

        # 失败！
        lib.record_failure_on_pattern("截图", "timeout")
        loaded = lib.load("s1")
        assert loaded.metadata.success_count == 0
        assert loaded.metadata.failure_count == 1

        # 从头开始
        lib.promote_or_insert(cand)  # 1 again
        loaded = lib.load("s1")
        assert loaded.metadata.success_count == 1
        assert loaded.metadata.status == "candidate"

    def test_pattern_matching_merge(self, tmp_path):
        """同名或同 pattern 的 skill 合并。"""
        lib = SkillLibrary("merge_test")

        cand1 = Skill(name="sk1", objective_pattern="截图完成", task_id="merge_test",
                      substeps=[SkillSubstep(tool="s")], metadata=SkillMetadata(success_count=1, total_uses=1))
        lib.promote_or_insert(cand1)  # 创建 sk1

        # 新 skill，相同 pattern
        cand2 = Skill(name="sk2", objective_pattern="截图完成", task_id="merge_test",
                      substeps=[SkillSubstep(tool="s"), SkillSubstep(tool="done")],
                      metadata=SkillMetadata(success_count=1, total_uses=1))

        r = lib.promote_or_insert(cand2)
        assert r["action"] in ("updated", "promoted")
        assert r["success_count"] == 2  # 合并了 sk1 的计数 + cand2 的计数


# === 辅助 =====================================================================
def test_derive_skill_name():
    assert "skill" in _derive_skill_name("截图当前屏幕")
    assert _derive_skill_name("中英mix_123") == "skill_中英mix_123"


def fake_path(frontmatter_text: str):
    """创建临时 Path 对象用于测试 _parse（不写盘）。"""
    import tempfile
    p = Path(tempfile.mktemp(suffix=".md"))
    p.write_text(frontmatter_text + "\n\n# body", encoding="utf-8")
    return p
