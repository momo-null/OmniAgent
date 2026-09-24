"""M4a.1 Trajectory 测试：TrajectoryStore 单元 + ToolLoop 集成落盘。

不依赖真实网络 / 模型 / 模拟器；集成测试用 monkeypatch 假大脑（与 test_m3b 同 pattern）。
"""
import json
from pathlib import Path

from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.local.loop import ToolLoop, TaskSpec
from omni_core.local.trajectory import TrajectoryStore


from tests._env import install_fake_env  # noqa: E402


class _FakeBackend:
    tool_schemas = []

    def __init__(self, *a, **k):
        self.observe_calls = 0
        self.kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

    def observe(self):
        self.observe_calls += 1
        return {"active_window": "com.fake.game", "ocr_text": []}

    def screenshot(self):
        return {"ok": True, "path": "none"}

    def read_screen_text(self):
        return {"ok": True, "ocr_text": []}

    def execute_mouse_action(self, action, args):
        return True

    def execute_keyboard_action(self, action, args):
        return True


def _fake_brain_factory(mapping):
    class _FB:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.model = cfg.get("model", "")
            self._script = list(mapping.get(self.model, []))
            self.calls = 0

        def chat(self, messages, tools=None, tool_choice="auto"):
            self.calls += 1
            if self._script:
                return self._script.pop(0)
            return BrainReply(tool_calls=[ToolCall(name="task_done", args={"reason": "ok"}, id="t")], finish_reason="stop")

        def close(self):
            pass

    return _FB


BRAIN = "demo-model"
EXEC = "qwen3.5-4b-vl"


def _task_done():
    return BrainReply(tool_calls=[ToolCall(name="task_done", args={"reason": "ok"}, id="t")], finish_reason="stop")


def _make_loop(monkeypatch, tmp_path, brain_mapping):
    fb = _fake_brain_factory({BRAIN: brain_mapping})
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", fb)
    install_fake_env(monkeypatch, _FakeBackend)
    loop = ToolLoop({"model": BRAIN, "base_url": "http://127.0.0.1:9", "capabilities": {}}, verbose=False)
    return loop


# === TrajectoryStore 单元 ===================================================
def test_store_logs_n_steps(tmp_path):
    store = TrajectoryStore("t_app1")
    for i in range(5):
        store.log_step(state="EXECUTING", observation={"active_window": "w"}, action={"tool": "click", "args": {}}, result={"ok": True}, verified=False)
    store.close()
    lines = (store.dir).glob("*.jsonl")
    f = next(lines)
    rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 5
    assert all(r["step"] == i + 1 for i, r in enumerate(rows))
    assert all("run_id" in r and "metrics" in r for r in rows)


def test_store_finish_run_writes_record_and_failure(tmp_path, monkeypatch):
    import omni_core.local.runtime_paths as P
    monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
    P.ensure_task_dirs("t_app1")
    store = TrajectoryStore("t_app1", run_id="runXYZ")
    for _ in range(2):
        store.log_step(state="EXECUTING", observation={"active_window": "w"}, action={"tool": "click", "args": {}}, result={}, verified=False)
    rec = store.finish_run(
        objective="o", done_when="dw", expected=None, backend="emulator", brain_model="m1", exec_model="m2",
        success=False, steps=2, brain_calls=3, decision_steps=2, action_count=2, retry_count=1,
        failures=1, recoveries=0,
    )
    assert rec.run_id == "runXYZ"
    assert rec.trajectory_file.endswith(".jsonl")
    assert (P.task_dir("t_app1") / "runXYZ.run.json").exists()
    # 失败单写 failures/
    assert (P.task_dir("t_app1") / "failures" / "runXYZ.json").exists()


def test_store_prune_removes_old(tmp_path, monkeypatch):
    import os
    import time
    import omni_core.local.runtime_paths as P
    monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
    P.ensure_task_dirs("t_app1")
    store = TrajectoryStore("t_app1")
    store.log_step(state="EXECUTING", observation={}, action=None, result={}, verified=False)
    store.close()
    f = next((P.task_dir("t_app1")).glob("*.jsonl"))
    old = time.time() - 40 * 86400
    os.utime(f, (old, old))
    removed = TrajectoryStore("t_app1").prune(max_age_days=30)
    assert removed >= 1
    assert not f.exists()


# === ToolLoop 集成落盘 ========================================================
def test_toolloop_writes_trajectory(monkeypatch, tmp_path):
    import omni_core.local.runtime_paths as P
    monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
    P.ensure_task_dirs("t_test")
    loop = _make_loop(monkeypatch, tmp_path, brain_mapping=[_task_done()])
    res = loop.run_task(TaskSpec(objective="o", max_steps=3, task_id="t_test"))
    assert res["success"] is True
    # jsonl 行数 == 步数（单步即完成）
    # 注：T4.6 起同一文件还混入 kind=think 的可观测记录（request_fingerprint / 推理链），
    # 它们不属于「状态步」，故此处只对状态行计数。
    jsons = list(P.task_dir("t_test").glob("*.jsonl"))
    assert jsons, "应生成轨迹 jsonl"
    rows = [json.loads(l) for l in jsons[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    state_rows = [r for r in rows if "state" in r]
    think_rows = [r for r in rows if r.get("kind") == "think"]
    assert len(state_rows) == res["steps"]
    # think 行仍应落盘（T4.6 可观测），且只做增量断言——不断言具体条数，避免与实现细节耦合
    assert think_rows, "应至少落一条 kind=think 的可观测记录"
    # 结果含 run_id + metrics
    assert res["run_id"]
    assert "metrics" in res and "brain_intervention_rate" in res["metrics"]
    # 报告文件存在
    assert res["report_file"] and Path(res["report_file"]).exists()


def test_toolloop_trajectory_disabled_no_files(monkeypatch, tmp_path):
    import omni_core.local.runtime_paths as P
    monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
    P.ensure_task_dirs("t_test")
    loop = _make_loop(monkeypatch, tmp_path, brain_mapping=[_task_done()])
    loop.traj_enabled = False
    res = loop.run_task(TaskSpec(objective="o", max_steps=3, task_id="t_test"))
    assert res["success"] is True
    assert "metrics" not in res  # 未落盘则不挂指标
