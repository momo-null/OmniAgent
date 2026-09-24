"""F1.1（P0）— skill 埋点计数修复。

契约：
- `build_skill_catalog` 返回结构化目录（每条目带 source 标签 task/global）；
- `_run_via_sdk` 先取 catalog 再格式化，埋点 skills=len(catalog)、追加 names/sources；
- 无 skill 时 skills=0 且不注入（skill_catalog=None）。

验收：
- 2 task + 1 global → 埋点 skills=3、chars>0、names/sources 正确；
- 无 skill → skills=0 且不注入。
"""
import json
import types

import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.local.knowledge_inject import build_skill_catalog
from omni_core.local.runtime_paths import global_skills, task_skills
from omni_core.local.loop import ToolLoop


class _Rec:
    """轨迹存储替身：记录 log_step / log_think 调用。"""

    def __init__(self):
        self.steps = []
        self.thinks = []

    def log_step(self, **kw):
        self.steps.append(kw)

    def log_think(self, content, role="", model=""):
        self.thinks.append({"content": content, "role": role, "model": model})


def _write(dirpath, filename, name, desc, body="技能正文"):
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / filename).write_text(
        "---\n"
        f"name: {name}\n"
        f"objective_pattern: {desc}\n"
        f"description: {desc}\n"
        "tags_json: []\n"
        "substeps_json: []\n"
        "metadata_json: {}\n"
        "---\n\n" + body,
        encoding="utf-8",
    )


def _loop():
    return ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}, verbose=False)


def _fake_run_subtask_sdk(captured):
    def _impl(*a, **k):
        captured["skill_catalog"] = k.get("skill_catalog")
        captured["kwargs"] = k
        return {
            "success": True, "reason": "ok", "steps": 1,
            "escalated": False, "escalate_reason": "",
            "provider_error": False, "dispatch_plan": [],
        }
    return _impl


def _spec(task_id):
    return types.SimpleNamespace(
        objective="o", done_when="", expected=None, task_id=task_id,
        project_id=None, max_steps=None, history=[], corrections=[],
    )


def test_catalog_returns_source_tags():
    """build_skill_catalog 每条目带 source 标签，供埋点统计来源。"""
    tid = "f11_src"
    _write(task_skills(tid), "a.md", "a", "任务A")
    _write(task_skills(tid), "b.md", "b", "任务B")
    _write(global_skills(), "c.md", "c", "全局C")
    cat = build_skill_catalog(tid)
    names = [s["name"] for s in cat]
    assert names == ["a", "b", "c"], names
    sources = {
        "task": sum(1 for s in cat if s.get("source") == "task"),
        "global": sum(1 for s in cat if s.get("source") == "global"),
    }
    assert sources == {"task": 2, "global": 1}, sources


def test_skill_probe_logs_names_and_sources(monkeypatch, tmp_path):
    """2 task + 1 global → 埋点 skills=3、chars>0、names/sources 正确。"""
    tid = "f11_probe"
    _write(task_skills(tid), "a.md", "a", "任务A")
    _write(task_skills(tid), "b.md", "b", "任务B")
    _write(global_skills(), "c.md", "c", "全局C")

    captured = {}
    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_run_subtask_sdk(captured))

    traj = _Rec()
    loop = _loop()
    loop._run_via_sdk(
        _spec(tid), {"model": "m"}, "sys", types.SimpleNamespace(),
        traj=traj, allow_dispatch=False, user_input="hi",
    )

    assert captured.get("skill_catalog"), "技能目录应被注入"
    probes = [json.loads(t["content"]) for t in traj.thinks
              if "skill_catalog" in str(t["content"])]
    assert probes, "应有一条 skill_catalog 埋点"
    probe = probes[0]
    assert probe["kind"] == "skill_catalog"
    assert probe["skills"] == 3, probe
    assert probe["chars"] > 0, probe
    assert set(probe["names"]) == {"a", "b", "c"}, probe["names"]
    assert probe["sources"] == {"task": 2, "global": 1}, probe["sources"]


def test_no_skill_no_injection(monkeypatch):
    """无技能文件 → skill_catalog=None 且不写埋点。"""
    captured = {}
    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_run_subtask_sdk(captured))

    traj = _Rec()
    loop = _loop()
    loop._run_via_sdk(
        _spec("f11_none"), {"model": "m"}, "sys", types.SimpleNamespace(),
        traj=traj, allow_dispatch=False, user_input="hi",
    )
    # 无技能文件 → skill_catalog 为空串（falsy），SDK 侧 `if skill_catalog:` 判定不注入
    assert not captured["kwargs"].get("skill_catalog"), "无技能时不应注入目录"
    probes = [t for t in traj.thinks if "skill_catalog" in str(t["content"])]
    assert not probes, "无技能时不应有埋点"
