"""Skill 库测试：save/load、候选宏缓存、失败打断、录制。"""
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
    _split_frontmatter, compute_entry_id,
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

    def test_record_failure_cumulative(self):
        """失败**不清零** success_count（累计晋升语义；惩罚归效用分 A5）。"""
        s = Skill(metadata=SkillMetadata(status="candidate", success_count=2, total_uses=2))
        s.record_failure("timeout")
        assert s.metadata.success_count == 2
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
        """Skill → frontmatter → 解析回 Skill（JSON 内嵌方案 + entry_id）。"""
        s = Skill(name="sk", objective_pattern="op", task_id="a", entry_id="abcd1234",
                  substeps=[SkillSubstep(tool="t", args={"x": 1})],
                  metadata=SkillMetadata(success_count=2, status="candidate"),
                  disable_model_invocation=True)
        md = s.to_frontmatter()
        assert "substeps_json" in md
        assert "metadata_json" in md
        parsed = SkillLibrary._parse(fake_path(md))
        assert parsed is not None
        assert parsed.name == "sk"
        assert parsed.entry_id == "abcd1234"
        assert parsed.disable_model_invocation is True  # B3：保存不再丢禁用标记
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




    def test_n3_promotion_gate(self, tmp_path):
        """3 次累计成功 → candidate → active（entry_id 命中同一档）。

        2026-10-04 语义修正：created 分支由 record_success 记账首次成功，
        candidate 不再需要预置计数（旧写法预置 1 会让门变成第 4 次才触发）。
        """
        lib = SkillLibrary("n3_test")

        cand = Skill(
            name="s1", objective_pattern="截图", task_id="n3_test",
            substeps=[SkillSubstep(tool="s", args={})],
        )
        # 第 1 次——创建即记账首次成功
        r1 = lib.promote_or_insert(cand)
        assert r1["action"] == "created"
        assert r1["promoted"] is False
        assert r1["success_count"] == 1

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

    def test_failure_does_not_reset_cumulative_count(self, tmp_path):
        """失败不清零：累计成功继续向 3 累进（惩罚归效用分）。"""
        lib = SkillLibrary("fail_test")

        cand = Skill(
            name="s1", objective_pattern="截图", task_id="fail_test",
            substeps=[SkillSubstep(tool="s", args={})],
        )
        lib.promote_or_insert(cand)  # 1（创建即记账）
        lib.promote_or_insert(cand)  # 2

        # 失败——只记 failure_count，不清 success_count（record_failure_on_pattern
        # 已随 B4 删除，失败记账直接走 skill.record_failure）
        loaded = lib.load("s1")
        loaded.record_failure("timeout")
        lib.save(loaded)
        loaded = lib.load("s1")
        assert loaded.metadata.success_count == 2
        assert loaded.metadata.failure_count == 1

        # 第 3 次成功 → 晋升
        r = lib.promote_or_insert(cand)
        assert r["promoted"] is True
        loaded = lib.load("s1")
        assert loaded.metadata.status == "active"

    def test_pattern_matching_merge(self, tmp_path):
        """同名或同 pattern 的 skill 合并（entry_id 不同时的 legacy 兜底）。

        2026-10-04 语义修正：created 分支同样记账首次成功（否则 N=3 门实际
        要 4 次成功才触发）。candidate 按真实流程不预置计数：创建=1，合并再 +1=2。
        """
        lib = SkillLibrary("merge_test")

        cand1 = Skill(name="sk1", objective_pattern="截图完成", task_id="merge_test",
                      substeps=[SkillSubstep(tool="s")])
        lib.promote_or_insert(cand1)  # 创建 sk1（首次成功已记账）

        # 新 skill，相同 pattern
        cand2 = Skill(name="sk2", objective_pattern="截图完成", task_id="merge_test",
                      substeps=[SkillSubstep(tool="s"), SkillSubstep(tool="done")])

        r = lib.promote_or_insert(cand2)
        assert r["action"] in ("updated", "promoted")
        assert r["success_count"] == 2  # 创建 1 次 + 合并 1 次


# === 集成：entry_id 技能身份（A1）=============================================
class TestEntryId:
    def test_entry_id_stable_across_volatile_args(self):
        """text/坐标/path 等易变值不进身份。"""
        a = [SkillSubstep(tool="type_text", args={"text": "hello"}),
             SkillSubstep(tool="click", args={"x": 0.5, "y": 0.7})]
        b = [SkillSubstep(tool="type_text", args={"text": "world"}),
             SkillSubstep(tool="click", args={"x": 0.1, "y": 0.9})]
        assert compute_entry_id(a) == compute_entry_id(b)

    def test_entry_id_distinguishes_structure(self):
        """结构性键（resource_id）保留原值——不同控件 = 不同套路。"""
        a = [SkillSubstep(tool="tap_by_id", args={"resource_id": "com.x:id/btn"})]
        b = [SkillSubstep(tool="tap_by_id", args={"resource_id": "com.x:id/ok"})]
        assert compute_entry_id(a) != compute_entry_id(b)

    def test_promote_matches_by_entry_id_across_names(self, tmp_path):
        """同一动作序列（仅易变 text 不同）、不同 objective 措辞 → 合并到同一档。"""
        lib = SkillLibrary("eid_test")
        cand1 = Skill(name="sk_jietu", objective_pattern="截图当前屏幕",
                      substeps=[SkillSubstep(tool="screenshot"),
                                SkillSubstep(tool="type_text", args={"text": "备注甲"}),
                                SkillSubstep(tool="task_done")])
        assert lib.promote_or_insert(cand1)["action"] == "created"

        cand2 = Skill(name="sk_jieping", objective_pattern="帮我截个屏",
                      substeps=[SkillSubstep(tool="screenshot"),
                                SkillSubstep(tool="type_text", args={"text": "备注乙"}),
                                SkillSubstep(tool="task_done")])
        r2 = lib.promote_or_insert(cand2)
        assert r2["action"] == "updated", r2
        assert r2["success_count"] == 2
        assert len(lib.list_all()) == 1, "同序列不得产生第二档"

    def test_filename_has_entry_suffix_and_roundtrip(self, tmp_path):
        """有身份的技能文件名带 entry8 后缀；load 按名仍可寻回（落 project 层）。"""
        lib = SkillLibrary("fn_test")
        s = Skill(name="sk", objective_pattern="op",
                  substeps=[SkillSubstep(tool="t", args={"text": "x"})])
        lib.save(s)
        skills_dir = tmp_path / ".omniagent" / "projects" / "default" / "skills"
        files = list(skills_dir.glob("*.md"))
        assert len(files) == 1
        assert files[0].stem.startswith("sk-"), files[0].name
        loaded = lib.load("sk")
        assert loaded is not None
        assert loaded.entry_id == s.entry_id

    def test_list_all_project_priority(self, tmp_path):
        """同名/同身份技能 project 胜出（project 目录先到先得）。"""
        lib = SkillLibrary("prio_test")
        g = Skill(name="dup", objective_pattern="from_global",
                  metadata=SkillMetadata(scope="global"))
        lib.save_global(g)
        p = Skill(name="dup", objective_pattern="from_task",
                  metadata=SkillMetadata(scope="project"))
        lib.save(p)
        skills = lib.list_all()
        assert len(skills) == 1
        assert skills[0].objective_pattern == "from_task"

    def test_promote_to_global_moves_and_unshadows(self, tmp_path):
        """A7「设为全局」：project 副本落全局并移除——否则 project 优先会遮蔽提升。"""
        lib = SkillLibrary("prom_test")
        s = Skill(name="moveme", objective_pattern="op",
                  substeps=[SkillSubstep(tool="t")],
                  metadata=SkillMetadata(scope="project"))
        lib.save(s)
        res = lib.promote_to_global("moveme")
        assert res["ok"] is True and res["removed_project_copy"] is True
        # project 层已无副本，全局层有了
        assert lib.load("moveme") is not None
        gpath = tmp_path / ".omniagent" / "skills"
        gfiles = list(gpath.glob("moveme*.md"))
        assert len(gfiles) == 1
        assert not list((tmp_path / ".omniagent" / "projects" / "default" / "skills").glob("moveme*"))
        gloaded = lib.load("moveme")
        assert gloaded.metadata.scope == "global"
        assert '"scope":"global"' in gfiles[0].read_text(encoding="utf-8")  # metadata_json 紧凑分隔符

    def test_promote_to_global_missing_skill(self, tmp_path):
        lib = SkillLibrary("prom_none")
        res = lib.promote_to_global("ghost")
        assert res["ok"] is False


# === 辅助 =====================================================================

def fake_path(frontmatter_text: str):
    """创建临时 Path 对象用于测试 _parse（不写盘）。"""
    import tempfile
    p = Path(tempfile.mktemp(suffix=".md"))
    p.write_text(frontmatter_text + "\n\n# body", encoding="utf-8")
    return p


# === 结构化宏提取 =============================================================
class TestSubstepsFromTrajectory:
    def _write_run(self, task_id, run_id, success, end_ts, step_records):
        import json as _json
        d = _RP.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        traj = d / f"2026-10-04_{run_id}.jsonl"
        traj.write_text(
            "\n".join(_json.dumps(r, ensure_ascii=False) for r in step_records) + "\n",
            encoding="utf-8")
        (d / f"{run_id}.run.json").write_text(_json.dumps({
            "run_id": run_id, "success": success, "end_ts": end_ts,
            "trajectory_file": str(traj)}), encoding="utf-8")
        return traj

    def test_no_task_returns_empty(self):
        from omni_core.local.skill_library import substeps_from_trajectory
        assert substeps_from_trajectory("t_no_such") == []

    def test_extracts_raw_args_filters_meta_and_failed(self):
        from omni_core.local.skill_library import substeps_from_trajectory
        self._write_run("t_ext1", "r1", True, "2026-10-04T10:00:00+00:00", [
            {"kind": "think", "content": "思考"},
            {"step": 1, "action": {"tool": "read_file", "args": {"path": "D:/data/a.txt"}},
             "result": {"ok": True}},
            {"step": 2, "action": {"tool": "write_file", "args": {"path": "D:/b.txt", "content": "x"}},
             "result": {"ok": True}},
            {"step": 3, "action": {"tool": "write_file", "args": {"path": "D:/c.txt"}},
             "result": {"ok": False, "error": "denied"}},
            {"step": 4, "action": {"tool": "task_done", "args": {"reason": "完成"}},
             "result": {"ok": True}},
        ])
        ss = substeps_from_trajectory("t_ext1")
        assert [s.tool for s in ss] == ["read_file", "write_file"]
        assert ss[0].args == {"path": "D:/data/a.txt"}  # 原始 args（双轨：不归一落盘）

    def test_picks_latest_success_run(self):
        from omni_core.local.skill_library import substeps_from_trajectory
        self._write_run("t_ext2", "r_old", True, "2026-10-04T09:00:00+00:00", [
            {"step": 1, "action": {"tool": "list_dir", "args": {"path": "."}}, "result": {"ok": True}},
            {"step": 2, "action": {"tool": "list_dir", "args": {"path": ".."}}, "result": {"ok": True}},
        ])
        self._write_run("t_ext2", "r_fail", False, "2026-10-04T10:00:00+00:00", [
            {"step": 1, "action": {"tool": "shell_exec", "args": {"command": "bad"}}, "result": {"ok": False}},
        ])
        self._write_run("t_ext2", "r_new", True, "2026-10-04T11:00:00+00:00", [
            {"step": 1, "action": {"tool": "shell_exec", "args": {"command": "echo hi"}}, "result": {"ok": True}},
            {"step": 2, "action": {"tool": "shell_exec", "args": {"command": "echo yo"}}, "result": {"ok": True}},
        ])
        ss = substeps_from_trajectory("t_ext2")
        assert [s.tool for s in ss] == ["shell_exec", "shell_exec"]
        assert ss[0].args == {"command": "echo hi"}

    def test_dual_track_raw_vs_entry_id(self):
        """原始 args 保真；entry_id 由归一形态计算——同构不同文本同身份。"""
        from omni_core.local.skill_library import substeps_from_trajectory, compute_entry_id
        self._write_run("t_ext3", "r1", True, "2026-10-04T10:00:00+00:00", [
            {"step": 1, "action": {"tool": "shell_exec", "args": {"command": "echo alpha"}}, "result": {"ok": True}},
            {"step": 2, "action": {"tool": "shell_exec", "args": {"command": "echo beta"}}, "result": {"ok": True}},
        ])
        ss = substeps_from_trajectory("t_ext3")
        assert ss[0].args == {"command": "echo alpha"}
        other = [SkillSubstep(tool="shell_exec", args={"command": "echo gamma"}),
                 SkillSubstep(tool="shell_exec", args={"command": "echo delta"})]
        assert compute_entry_id(ss) == compute_entry_id(other)  # 归一形态同构


# === 缓存淘汰 =================================================================
class TestEvictStale:
    def _mk(self, lib, name, status="candidate", entry="e1", age_days=0.0,
            confidence=0.0, last_used=None):
        from datetime import datetime, timedelta, timezone as _tz
        from omni_core.local.skill_library import Skill, SkillMetadata
        now = datetime.now(_tz.utc)
        md = SkillMetadata(
            status=status, confidence=confidence,
            created=(now - timedelta(days=age_days)).isoformat(),
            last_used=((now - timedelta(days=age_days)).isoformat() if last_used is None else last_used),
        )
        sk = Skill(name=name, objective_pattern="o", entry_id=entry,
                   substeps=[SkillSubstep(tool="a", args={})], metadata=md)
        lib.save(sk)
        return sk

    def test_candidate_expired_archived(self):
        lib = SkillLibrary("ev_test")
        self._mk(lib, "skill_旧候选", status="candidate", age_days=15)
        res = lib.evict_stale(candidate_ttl_days=14)
        assert res["candidates_archived"] == 1
        assert lib.load("skill_旧候选") is None  # 退出 *_ 视野
        assert (lib._dir() / "_archive").is_dir()  # 降级不删

    def test_candidate_fresh_stays(self):
        lib = SkillLibrary("ev_test2")
        self._mk(lib, "skill_新候选", status="candidate", age_days=1)
        res = lib.evict_stale(candidate_ttl_days=14)
        assert res["archived"] == 0
        assert lib.load("skill_新候选") is not None

    def test_active_low_confidence_idle_archived(self):
        lib = SkillLibrary("ev_test3")
        self._mk(lib, "skill_弱宏", status="active", age_days=31, confidence=0.2)
        res = lib.evict_stale(active_idle_days=30, active_min_confidence=0.5)
        assert res["actives_archived"] == 1

    def test_active_good_confidence_survives_idleness(self):
        lib = SkillLibrary("ev_test4")
        self._mk(lib, "skill_强宏", status="active", age_days=90, confidence=0.9)
        res = lib.evict_stale(active_idle_days=30, active_min_confidence=0.5)
        assert res["archived"] == 0

    def test_handwritten_never_evicted(self):
        from datetime import datetime, timedelta, timezone as _tz
        from omni_core.local.skill_library import Skill, SkillMetadata
        lib = SkillLibrary("ev_test5")
        old = (datetime.now(_tz.utc) - timedelta(days=365)).isoformat()
        sk = Skill(name="手写技能", objective_pattern="o", entry_id="",
                   metadata=SkillMetadata(created=old, status="candidate"))
        lib.save(sk)
        res = lib.evict_stale(candidate_ttl_days=14)
        assert res["archived"] == 0
        assert lib.load("手写技能") is not None


# === 生产者 · 结构化宏提取（skill.auto_distill） ==========
class TestSkillAutoDistill:
    def _write_run(self, task_id, run_id, success, end_ts, step_records):
        d = _RP.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        traj = d / f"2026-10-04_{run_id}.jsonl"
        traj.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in step_records) + "\n",
            encoding="utf-8")
        (d / f"{run_id}.run.json").write_text(json.dumps({
            "run_id": run_id, "success": success, "end_ts": end_ts,
            "trajectory_file": str(traj)}), encoding="utf-8")
        return traj

    _STEPS = [
        {"step": 1, "action": {"tool": "shell_exec", "args": {"command": "rm -rf build"}}, "result": {"ok": True}},
        {"step": 2, "action": {"tool": "shell_exec", "args": {"command": "echo done"}}, "result": {"ok": True}},
    ]

    def _mock_labels(self, monkeypatch, routine="清理产物", description="清理构建产物目录"):
        import omni_core.local.llm_judge as lj
        monkeypatch.setattr(lj, "chat_json",
                            lambda *a, **k: {"routine": routine, "description": description})

    def _distill(self, task_id, success=True, objective="清理构建产物", brain_cfg={"mock": 1}):
        from omni_core.local.skill_library import maybe_distill_skill
        return maybe_distill_skill(task_id, brain_cfg, success, objective)

    def test_no_brain_skipped(self):
        assert self._distill("sk0", brain_cfg=None) == {"action": "skipped", "reason": "no_brain"}

    def test_not_success_skipped(self):
        assert self._distill("sk0b", success=False)["reason"] == "not_success"

    def test_creates_candidate_with_raw_args(self, monkeypatch):
        self._write_run("sk1", "r1", True, "2026-10-04T10:00:00+00:00", self._STEPS)
        self._mock_labels(monkeypatch)
        out = self._distill("sk1")
        assert out["action"] == "created"
        files = list(_RP.project_skills("default").glob("*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "skill_清理产物" in text
        assert "rm -rf build" in text  # 原始 args 双轨落盘（非归一形态）

    def test_too_few_steps_skipped(self, monkeypatch):
        self._write_run("sk2", "r1", True, "2026-10-04T10:00:00+00:00", self._STEPS[:1])
        self._mock_labels(monkeypatch)
        assert self._distill("sk2")["reason"] == "too_few_steps"

    def test_not_replayable_skipped(self, monkeypatch):
        """回放白名单前置把关：步骤含白名单外工具 → 不建候选（宁严勿松）。"""
        steps = [
            {"step": 1, "action": {"tool": "shell_exec", "args": {"command": "echo a"}}, "result": {"ok": True}},
            {"step": 2, "action": {"tool": "advance_tick", "args": {"ticks": 100}}, "result": {"ok": True}},
        ]
        self._write_run("sk_nr", "r1", True, "2026-10-05T10:00:00+00:00", steps)
        self._mock_labels(monkeypatch)
        out = self._distill("sk_nr")
        assert out["action"] == "skipped" and out["reason"] == "not_replayable"

    def test_validate_failure_no_output(self, monkeypatch):
        self._write_run("sk3", "r1", True, "2026-10-04T10:00:00+00:00", self._STEPS)
        self._mock_labels(monkeypatch, routine="这个技能名字实在太长了不合格")
        out = self._distill("sk3")
        assert out["action"] == "skipped" and out["reason"] == "validate_failed"

    def test_repeated_sequence_increments_success_count(self, monkeypatch):
        self._write_run("sk4", "r1", True, "2026-10-04T10:00:00+00:00", self._STEPS)
        self._mock_labels(monkeypatch)
        assert self._distill("sk4")["action"] == "created"
        assert self._distill("sk4")["action"] == "updated"
        sk = SkillLibrary(task_id="sk4").list_all()[0]
        assert sk.metadata.success_count == 2

    def _mk_finish_stub(self, task_id, brain_cfg={"mock": 1}):
        """构造可直接调用 FinishMixin._finish 的最小桩（不跑真循环）。"""
        from omni_core.local.loop.finish import FinishMixin
        from omni_core.local.loop.parts import TaskSpec

        class _Rec:
            run_id = "r9"
            trajectory_file = "t.jsonl"
            def to_dict(self):
                return {}

        class _Store:
            def finish_run(self, **kw):
                return _Rec()

        class _Stub:
            brain = None
            executor = None
            _cfg = {}
            brain_model = "m"
            exec_model = "m"
            _brain_calls = _decision_steps = _action_count = _retry_count = 0
            _failures = _recoveries = 0
            verbose = False
            def _log(self, *a):
                pass

        stub = _Stub()
        stub.brain_cfg = brain_cfg
        return FinishMixin._finish, stub, _Store(), TaskSpec(
            objective="清理构建产物", done_when="", task_id=task_id)

    def _patch_gate(self, monkeypatch, enabled):
        import config as config_mod
        real = config_mod.get_config
        monkeypatch.setattr(config_mod, "get_config",
                            lambda k, d=None: enabled if k == "skill.auto_distill" else real(k, d))

    def test_finish_gate_off_noop(self, monkeypatch):
        """默认态（开关关）：_finish 收尾不产出技能——现状零行为变化。"""
        self._write_run("sk5", "r1", True, "2026-10-04T10:00:00+00:00", self._STEPS)
        self._mock_labels(monkeypatch)
        self._patch_gate(monkeypatch, False)
        fin, stub, store, spec = self._mk_finish_stub("sk5")
        fin(stub, True, "ok", 2, store, spec)
        d = _RP.project_skills("default")
        assert not d.exists() or not list(d.glob("*.md"))

    def test_finish_gate_on_distills(self, monkeypatch):
        """开关开：_finish 收尾触发宏提取并落库（原始 args）。"""
        self._write_run("sk6", "r1", True, "2026-10-04T10:00:00+00:00", self._STEPS)
        self._mock_labels(monkeypatch)
        self._patch_gate(monkeypatch, True)
        fin, stub, store, spec = self._mk_finish_stub("sk6")
        fin(stub, True, "ok", 2, store, spec)
        files = list(_RP.project_skills("default").glob("*.md"))
        assert len(files) == 1
        assert "rm -rf build" in files[0].read_text(encoding="utf-8")
