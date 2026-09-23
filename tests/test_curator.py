"""M4b.3 Curator 测试：prune / refine / review_skills / flag_low_quality。

不依赖真实网络 / 模型 / 模拟器；全部用 tmp_path 临时目录。
"""
import json
import os
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _iso_global(tmp_path, monkeypatch):
    """隔离 ~/.omniagent 到临时目录，测试后自动恢复。"""
    monkeypatch.setattr(_RP, "_GLOBAL", tmp_path / ".omniagent")
    _RP.ensure_global_dirs()


from omni_core.local.curator import Curator, CuratorReport, DEFAULTS
from omni_core.local.trajectory import TrajectoryStore
from omni_core.local.world_model import WorldModel
from omni_core.local.skill_library import SkillLibrary, Skill, SkillSubstep, SkillMetadata
import omni_core.local.runtime_paths as _RP


# === 辅助：构造 RunRecord dict ============================================

def _make_run_record(
    run_id: str = "test_run_001",
    success: bool = True,
    steps: int = 5,
    retry_count: int = 0,
    brain_calls: int = 1,
    decision_steps: int = 5,
    objective: str = "点击开始按钮",
    steps_data: list = None,
    reason: str = "",
) -> dict:
    return {
        "run_id": run_id,
        "objective": objective,
        "done_when": "看到主界面",
        "expected": None,
        "backend": "emulator",
        "brain_model": "demo-model",
        "exec_model": "qwen3.5-4b",
        "success": success,
        "steps": steps,
        "brain_calls": brain_calls,
        "decision_steps": decision_steps,
        "action_count": steps,
        "retry_count": retry_count,
        "failures": 0 if success else 1,
        "recoveries": 0,
        "reason": reason,
        "steps_data": steps_data or [
            {"tool": "observe", "args": {}},
            {"tool": "click", "args": {"x": 0.5, "y": 0.3}},
            {"tool": "verify", "args": {}},
        ],
    }


def _make_old_file(path: Path, age_days: int, content: str = "{}") -> None:
    """创建一个文件并将其 mtime 设为 age_days 天前。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    old_ts = time.time() - age_days * 86400
    os.utime(path, (old_ts, old_ts))


# === 1. prune_trajectories ==================================================

class TestPruneTrajectories:
    def test_prune_old_jsonl(self, tmp_path):
        """超过 30 天的 jsonl 应被删除。"""
        app_dir = _RP.tasks_root() / "testapp"
        app_dir.mkdir(parents=True, exist_ok=True)
        _make_old_file(app_dir / "old_001.jsonl", age_days=35, content='{"step":1}\n')
        _make_old_file(app_dir / "old_001.run.json", age_days=35, content='{"run_id":"old_001"}')
        (app_dir / "new_002.jsonl").write_text('{"step":1}\n', encoding="utf-8")

        curator = Curator(task_id="testapp")
        removed = curator.prune_trajectories()

        assert removed >= 2  # old jsonl + run.json
        assert (app_dir / "new_002.jsonl").exists()
        assert not (app_dir / "old_001.jsonl").exists()

    def test_prune_failures_14_days(self, tmp_path):
        """failures/ 下超过 14 天的应被删除。"""
        app_dir = _RP.tasks_root() / "testapp"
        app_dir.mkdir(parents=True, exist_ok=True)
        fdir = app_dir / "failures"
        _make_old_file(fdir / "old_fail.json", age_days=20, content='{"run_id":"old_fail"}')
        (fdir / "new_fail.json").write_text('{"run_id":"new_fail"}', encoding="utf-8")

        curator = Curator(task_id="testapp")
        removed = curator.prune_trajectories()

        assert removed >= 1
        assert not (fdir / "old_fail.json").exists()
        assert (fdir / "new_fail.json").exists()

    def test_prune_empty_dir(self, tmp_path):
        """目录不存在时不报错。"""
        curator = Curator(task_id="nonexistent")
        removed = curator.prune_trajectories()
        assert removed == 0


# === 2. refine_world_model ==================================================

class TestRefineWorldModel:
    def test_remove_duplicate_facts(self, tmp_path, monkeypatch):
        """磁盘上重复 facts 应被去重（模拟历史累积）。"""
        wm = WorldModel(task_id="testapp")
        # 直接 append 绕过 add_fact 去重，模拟磁盘上已有重复
        wm.facts = ["界面有开始按钮", "界面有开始按钮", "电量充足", "界面有开始按钮", ""]
        wm.save()

        curator = Curator(task_id="testapp")
        removed = curator.refine_world_model()

        # 原始 5 条 → load 过滤空 → 4 条（3 重复 + 1 独特）→ 去重后 2 条 → 移除 2
        assert removed == 2
        wm2 = WorldModel(task_id="testapp")
        wm2.load()
        assert len(wm2.facts) == 2

    def test_no_facts_no_change(self, tmp_path, monkeypatch):
        """无 facts 时不改动。"""
        wm = WorldModel(task_id="testapp")
        wm.save()

        curator = Curator(task_id="testapp")
        removed = curator.refine_world_model()
        assert removed == 0

    def test_no_file_returns_zero(self, tmp_path):
        """current.md 不存在时返回 0。"""
        curator = Curator(task_id="nonexistent")
        removed = curator.refine_world_model()
        assert removed == 0


# === 3. review_candidate_skills =============================================

class TestReviewCandidateSkills:
    def test_success_run_creates_candidate(self, tmp_path, monkeypatch):
        """成功 RunRecord 应提取 candidate skill。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(success=True, steps=5)

        result = curator.review_candidate_skills(rec)

        assert result["reviewed"] == 1
        assert result["created"] == 1
        assert result["promoted"] == 0

        lib = SkillLibrary(task_id="testapp")
        skills = lib.list_all()
        assert len(skills) >= 1

    def test_failed_run_no_skill(self, tmp_path, monkeypatch):
        """失败 RunRecord 不提取 skill。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(success=False)

        result = curator.review_candidate_skills(rec)
        assert result["reviewed"] == 0
        assert result["created"] == 0

    def test_short_run_no_skill(self, tmp_path, monkeypatch):
        """步数太少（< min_steps_for_skill）不提取。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(success=True, steps=1)

        result = curator.review_candidate_skills(rec)
        assert result["reviewed"] == 0

    def test_n3_promotion(self, tmp_path, monkeypatch):
        """连续 3 次成功 → 晋升 active。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(success=True, objective="点击开始按钮")

        # 第一次 → created
        r1 = curator.review_candidate_skills(rec)
        assert r1["created"] == 1 and r1["promoted"] == 0

        # 第二次 → updated（未晋升）
        r2 = curator.review_candidate_skills(rec)
        assert r2["promoted"] == 0

        # 第三次 → promoted
        r3 = curator.review_candidate_skills(rec)
        assert r3["promoted"] == 1

        lib = SkillLibrary(task_id="testapp")
        skills = lib.list_all()
        assert any(s.metadata.status == "active" for s in skills)


# === 4. flag_low_quality ====================================================

class TestFlagLowQuality:
    def test_high_retry_flagged(self, tmp_path):
        """retry_count >= 阈值 → 标 low_quality。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(run_id="high_retry_run", success=True, retry_count=5)

        flagged = curator.flag_low_quality(rec)

        assert "high_retry_run" in flagged
        marker_path = _RP.tasks_root() / "testapp" / "excluded" / "high_retry_run.json"
        assert marker_path.exists()
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        assert "high_retry=5" in marker["reasons"]

    def test_high_intervention_flagged(self, tmp_path):
        """brain_intervention_rate >= 阈值 → 标 low_quality。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(
            run_id="high_intv_run",
            success=True,
            brain_calls=8,
            decision_steps=10,  # 80% intervention
        )

        flagged = curator.flag_low_quality(rec)
        assert "high_intv_run" in flagged

    def test_failed_run_flagged(self, tmp_path):
        """失败 run → 标 low_quality。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(run_id="failed_run", success=False)

        flagged = curator.flag_low_quality(rec)
        assert "failed_run" in flagged

    def test_good_run_not_flagged(self, tmp_path):
        """正常成功 run → 不标记。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(
            run_id="good_run",
            success=True,
            retry_count=0,
            brain_calls=1,
            decision_steps=10,  # 10% intervention
        )

        flagged = curator.flag_low_quality(rec)
        assert flagged == []
        excl_dir = _RP.tasks_root() / "testapp" / "excluded"
        assert not excl_dir.exists() or not any(excl_dir.iterdir())

    def test_no_run_id_not_flagged(self, tmp_path):
        """无 run_id → 不标记。"""
        curator = Curator(task_id="testapp")
        rec = _make_run_record(run_id="", success=False)
        flagged = curator.flag_low_quality(rec)
        assert flagged == []


# === 5. run_once 集成 =======================================================

class TestRunOnce:
    def test_full_run_once_success(self, tmp_path, monkeypatch):
        """成功 run → run_once 执行四件维护、返回 report。"""

        curator = Curator(
            task_id="testapp",
        )
        rec = _make_run_record(
            run_id="good_001",
            success=True,
            steps=5,
            retry_count=0,
            brain_calls=1,
            decision_steps=5,
        )

        report = curator.run_once(rec)

        assert isinstance(report, CuratorReport)
        assert report.task_id == "testapp"
        assert report.skills_created == 1
        assert len(report.flagged_low_quality) == 0
        assert len(report.errors) == 0

    def test_full_run_once_low_quality(self, tmp_path, monkeypatch):
        """高 retry 失败 run → run_once 标 low_quality。"""

        curator = Curator(
            task_id="testapp",
        )
        rec = _make_run_record(
            run_id="bad_001",
            success=False,
            retry_count=4,
        )

        report = curator.run_once(rec)

        assert "bad_001" in report.flagged_low_quality
        assert report.skills_created == 0  # 失败不提取 skill

    def test_run_once_no_run_record(self, tmp_path, monkeypatch):
        """无 run_record → 只跑 prune + refine，不跑 skill/flag。"""

        curator = Curator(
            task_id="testapp",
        )
        report = curator.run_once(None)

        assert report.skills_reviewed == 0
        assert report.flagged_low_quality == []
        assert len(report.errors) == 0

    def test_run_once_prune_works(self, tmp_path, monkeypatch):
        """run_once 含 prune，过期文件被清理。"""

        app_dir = _RP.tasks_root() / "testapp"
        app_dir.mkdir(parents=True, exist_ok=True)
        _make_old_file(app_dir / "old.jsonl", age_days=40)

        curator = Curator(
            task_id="testapp",
        )
        report = curator.run_once(None)

        assert report.pruned_files >= 1
        assert not (app_dir / "old.jsonl").exists()


# === 6. scan_all_runs / flag_all ============================================

class TestBatchScan:
    def test_scan_all_runs(self, tmp_path):
        """scan_all_runs 读出所有 *.run.json。"""
        app_dir = _RP.tasks_root() / "testapp"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "run1.run.json").write_text('{"run_id":"run1","success":true}', encoding="utf-8")
        (app_dir / "run2.run.json").write_text('{"run_id":"run2","success":false}', encoding="utf-8")

        curator = Curator(task_id="testapp")
        runs = curator.scan_all_runs()

        assert len(runs) == 2
        assert {r["run_id"] for r in runs} == {"run1", "run2"}

    def test_flag_all_low_quality(self, tmp_path):
        """批量扫描 + 标记。"""
        app_dir = _RP.tasks_root() / "testapp"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "good.run.json").write_text(
            json.dumps(_make_run_record(run_id="good", success=True, retry_count=0,
                                        brain_calls=1, decision_steps=10)),
            encoding="utf-8",
        )
        (app_dir / "bad.run.json").write_text(
            json.dumps(_make_run_record(run_id="bad", success=False, retry_count=5)),
            encoding="utf-8",
        )

        curator = Curator(task_id="testapp")
        flagged = curator.flag_all_low_quality()

        assert "bad" in flagged
        assert "good" not in flagged


# === 7. distill_task_memory / maybe_merge_rollouts（§3.3 第五件维护） =========

class TestDistillAndMerge:
    def test_short_steps_distill_short_circuits(self, tmp_path):
        """步数低于阈值 → 直接返回 None（短路）。"""
        curator = Curator(task_id="distill_short")
        rec = _make_run_record(success=True, steps=1)
        assert curator.distill_task_memory(rec) is None

    def test_success_distill_facts_from_world_model(self, tmp_path):
        """成功任务读取磁盘 world_model facts → rollout 含 ## facts。"""
        task_id = "distill_ok"
        wm = WorldModel(task_id=task_id)
        wm.add_fact("界面顶部有开始按钮")
        wm.add_fact("登录态已保持")
        wm.save()
        curator = Curator(task_id=task_id)
        rec = _make_run_record(success=True, steps=5, objective="启动应用")
        path = curator.distill_task_memory(rec)
        assert path is not None
        text = path.read_text(encoding="utf-8")
        assert "## facts" in text
        assert "界面顶部有开始按钮" in text
        assert "登录态已保持" in text
        assert "## lessons" in text

    def test_failed_distill_lessons(self, tmp_path):
        """失败任务 → rollout 含带归因的 [失败] lessons。"""
        curator = Curator(task_id="distill_fail")
        rec = _make_run_record(success=False, steps=5, reason="按钮点击后无响应", retry_count=1)
        path = curator.distill_task_memory(rec)
        assert path is not None
        text = path.read_text(encoding="utf-8")
        assert "[失败]" in text
        assert "按钮点击后无响应" in text

    def test_high_retry_generates_lesson(self, tmp_path):
        """高重试（即便成功） → 生成 [高重试(n)] 经验。"""
        curator = Curator(task_id="distill_retry")
        rec = _make_run_record(success=True, steps=5, retry_count=5, reason="首次坐标偏移")
        path = curator.distill_task_memory(rec)
        text = path.read_text(encoding="utf-8")
        assert "[高重试(5)]" in text

    def test_merge_below_threshold_noop(self, tmp_path):
        """未合并数 < 阈值 → 不生成 MEMORY.md，返回 0。"""
        curator = Curator(task_id="m1")
        for i in range(2):
            p = _RP.memory_rollout_file(f"m1_{i}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"# rollout: m1_{i}\n- task_id: m1_{i}\n\n## facts\n- fact{i}\n\n## lessons\n- (暂无)\n",
                encoding="utf-8",
            )
        assert curator.maybe_merge_rollouts() == 0
        assert not _RP.memory_master().exists()

    def test_merge_appends_and_dedups(self, tmp_path):
        """≥ 阈值 → 合并进 MEMORY.md，事实去重、唯一项保留；再生 summary。"""
        curator = Curator(task_id="m2")
        for i in range(5):
            p = _RP.memory_rollout_file(f"m2_{i}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"# rollout: m2_{i}\n- task_id: m2_{i}\n\n## facts\n- 共同事实\n- 唯一事实{i}\n\n## lessons\n- (暂无)\n",
                encoding="utf-8",
            )
        merged = curator.maybe_merge_rollouts()
        # 5 个 rollout：共同事实重复 + 5 个唯一 → 去重追加 1+5 = 6
        assert merged == 6
        master = _RP.memory_master().read_text(encoding="utf-8")
        assert "## 长期事实" in master
        assert master.count("共同事实") == 1
        assert master.count("唯一事实0") == 1
        assert _RP.memory_summary().exists()

    def test_merge_idempotent_second_run_zero(self, tmp_path):
        """二次合并已合并项 → 返回 0，merged.json 记录 5 个 id。"""
        curator = Curator(task_id="m3")
        for i in range(5):
            p = _RP.memory_rollout_file(f"m3_{i}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                f"# rollout: m2_{i}\n- task_id: m3_{i}\n\n## facts\n- f{i}\n\n## lessons\n- (暂无)\n",
                encoding="utf-8",
            )
        assert curator.maybe_merge_rollouts() > 0
        assert curator.maybe_merge_rollouts() == 0
        merged = json.loads((_RP.global_memory() / "merged.json").read_text(encoding="utf-8"))
        assert len(merged["merged_task_ids"]) == 5

    def test_run_once_distills_rollout(self, tmp_path):
        """run_once 在成功任务后触发蒸馏，report.rollouts_distilled == 1。"""
        task_id = "ro1"
        wm = WorldModel(task_id=task_id)
        wm.add_fact("已验证首页布局")
        wm.save()
        curator = Curator(task_id=task_id)
        rec = _make_run_record(success=True, steps=5, objective="打开首页")
        report = curator.run_once(rec)
        assert report.rollouts_distilled == 1
        assert _RP.memory_rollout_file(task_id).exists()
