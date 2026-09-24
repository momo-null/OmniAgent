"""B2（2026-09-22）：Run 登记异常、run_id 双源分裂修复验证。

两处独立缺陷：
1) run_id 双源生成（TrajectoryStore 自生成 vs world.run_id）-> 数据不一致 / 悬空；
   修复：TrajectoryStore 构造函数已支持可选 run_id，_make_store 透传统一 run_id。
2) runs 数组覆盖写入（runs=[world.run_id]）-> 历史运行记录丢失；
   修复：改用 TaskStore.add_run 追加（自带去重），多轮 run 有序沉淀、无覆盖。

验证：
- 单源：_make_store 透传的 run_id == TrajectoryStore.run_id（B2 修复点 1）。
- 追加：同一任务连续两轮 run，task.json.runs 有序包含两轮 run_id，无覆盖 / 无遗漏。
- 一致：collected.json 的 run_id（=world.run_id）与 task.json.runs 末条对齐。
"""
import json
import types
import uuid

from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.local import runtime_paths  # noqa: F401  (确保路径模块已导入)
from omni_core.local.runtime_paths import task_collected
from omni_core.local.task_store import TaskStore
from omni_core.local.loop import ToolLoop, TaskSpec


# --- 假后端 / 假大脑（最小可执行，不依赖真实模型 / 网络） -------------------
from tests._env import install_fake_env  # noqa: E402


class _FakeBackend:
    kind = "host"  # ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

    def __init__(self, *a, **k):
        self.ocr = []
        self.backend = _FakeBackendInner()  # 模拟真实执行后端（self.exec.backend 调用点）

    def observe(self):
        return {"active_window": "com.fake.game", "ocr_text": list(self.ocr)}

    def screenshot(self):
        return {"ok": True, "path": "none"}

    def read_screen_text(self):
        return {"ok": True, "ocr_text": list(self.ocr)}

    def execute_mouse_action(self, action, args):
        return True

    def execute_keyboard_action(self, action, args):
        return True


class _FakeBackendInner:
    """模拟真实执行后端（self.exec.backend 调用点）。"""

    def verify_done(self, cond: str, percept: dict):
        ocr = (percept or {}).get("ocr_text") or []
        return (any(cond and cond in str(o) for o in ocr), "ok" if cond else "no")

    def text_of(self, percept: dict) -> str:
        return " ".join((percept or {}).get("ocr_text") or [])


def _observe():
    return BrainReply(tool_calls=[ToolCall(name="observe", args={}, id="o1")], finish_reason="stop")


def _task_done(reason="ok"):
    return BrainReply(tool_calls=[ToolCall(name="task_done", args={"reason": reason}, id="t1")],
                      finish_reason="stop")


def _make_loop(monkeypatch):
    """单 agent（executor 兼任主模型），大脑先 observe 再 task_done，保证可完成。"""
    mapping = {"brain": [_observe(), _task_done("s1 ok")]}

    class _FakeBrain:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.model = cfg.get("model", "")
            self._script = list(mapping.get(self.model, []))
            self.calls = 0

        def chat(self, messages, tools=None, tool_choice="auto"):
            self.calls += 1
            if self._script:
                if callable(self._script[0]):
                    return self._script[0](messages)
                return self._script.pop(0)
            return _task_done("default")

        def close(self):
            pass

    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _FakeBrain)
    install_fake_env(monkeypatch, _FakeBackend)
    loop = ToolLoop(
        {"model": "brain", "base_url": "http://127.0.0.1:9", "capabilities": {}},
        verbose=False,
        executor_cfg={"enabled": False},
    )
    loop.exec.ocr = ["ALL"]  # verify 门控：屏幕含 done_when="ALL"
    return loop


# --- 1. 单源：_make_store 透传统一 run_id -----------------------------------
def test_make_store_unified_run_id_passthrough():
    """B2 修复点 1：TrajectoryStore 收到的 run_id 必须等于调用方透传的统一 run_id。"""
    loop = ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}, verbose=False)
    store = loop._make_store("t_b2_unit", "fixed_run_xyz")
    assert store is not None
    assert store.run_id == "fixed_run_xyz", "TrajectoryStore 应复用统一 run_id，而非自生成"
    # 向后兼容：不传 run_id 时仍自生成非空 ID
    store2 = loop._make_store("t_b2_unit2")
    assert store2 is not None and store2.run_id and store2.run_id != "fixed_run_xyz"


# --- 2. 追加：收尾落盘走 add_run（追加），而非 update(runs=[...]) 覆盖 ----------
def test_finish_appends_run_via_add_run(monkeypatch):
    """B2 修复点 2：收尾落盘必须走 add_run 追加，而非 TaskStore.update(runs=[...]) 覆盖。

    用单次 run_task + monkeypatch 断言生产代码的真实调用点：
    - add_run 必须被调用（追加本轮 run_id）；
    - TaskStore.update 不得带 runs= 形参（旧覆盖式写法会丢历史运行 / 产生悬空）。
    """
    loop = _make_loop(monkeypatch)
    add_calls = []
    update_calls = []
    orig_update = TaskStore.update
    orig_add = TaskStore.add_run

    def _upd(task_id, **kwargs):
        update_calls.append((task_id, dict(kwargs)))
        return orig_update(task_id, **kwargs)

    def _add(task_id, run_id):
        add_calls.append((task_id, run_id))
        return orig_add(task_id, run_id)

    monkeypatch.setattr(TaskStore, "update", _upd)
    monkeypatch.setattr(TaskStore, "add_run", _add)

    # 前置：生产环境任务在「提交」时已由 TaskStore.create 建好，_finish 的
    # update/add_run 依赖其存在（否则 update 会抛 KeyError 被吞，跳过 add_run）。
    created = TaskStore.create("o", done_when="ALL")
    task_id = created["task_id"]
    # max_steps 显式给上：本用例只验 add_run，不该依赖「预算兜底」——
    # 缺了它，一旦 verify 不过就会无界循环（生产 /chat 恒回退 default_max_steps=40）。
    spec = TaskSpec(objective="o", done_when="ALL", task_id=task_id, max_steps=5)
    res = loop.run_task(spec)
    assert res["success"] is True, res

    # B2 核心：runs 用 add_run 追加，绝不能用 update(runs=[...]) 覆盖历史运行
    my_adds = [(tid, rid) for (tid, rid) in add_calls if tid == task_id]
    assert my_adds, f"收尾必须调用 add_run 追加 run_id，实际 add_calls={add_calls}"
    assert not any("runs" in kw for (_tid, kw) in update_calls if _tid == task_id), \
        f"收尾不得用 update(runs=...) 覆盖历史运行，实际 update_calls={update_calls}"

    # 落地验证：task.json.runs 含本轮 run_id，且来自 add_run 透传
    meta = TaskStore.get(task_id)
    assert meta is not None and meta["runs"], "task.json.runs 应已落盘且非空"
    assert meta["runs"][-1] == my_adds[-1][1], \
        f"task.json.runs 末条应与 add_run 透传的 run_id 一致: {meta['runs']} vs {my_adds}"
    # 一致：collected.json 的 run_id（=world.run_id）与 task.json.runs 末条对齐
    cj = json.loads(task_collected(task_id).read_text(encoding="utf-8"))
    assert cj["run_id"] == meta["runs"][-1], \
        f"world.run_id 应与末轮 runs 对齐: collected={cj['run_id']} runs={meta['runs'][-1]}"


def test_task_store_add_run_append_semantics():
    """B2 修复点 2 的底层保证：add_run 追加 + 去重；旧的 update(runs=[...]) 会覆盖（反例）。"""
    created = TaskStore.create("o", done_when="")
    tid = created["task_id"]
    TaskStore.add_run(tid, "r1")
    TaskStore.add_run(tid, "r2")
    runs = TaskStore.get(tid)["runs"]
    assert runs == ["r1", "r2"], f"应追加为 2 条: {runs}"
    # 重复 run_id 去重，不产生悬空 / 重复
    TaskStore.add_run(tid, "r1")
    assert TaskStore.get(tid)["runs"] == ["r1", "r2"], f"重复 run_id 应去重: {TaskStore.get(tid)['runs']}"
    # 反例：若用旧覆盖式 update(runs=[...])，历史运行会丢失（证明必须改用 add_run）
    TaskStore.update(tid, runs=["r3"])
    assert TaskStore.get(tid)["runs"] == ["r3"], "旧覆盖式仅留 1 条（证明必须改用 add_run 追加）"

