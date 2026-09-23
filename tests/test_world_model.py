"""M4b.1 WorldModel 持久化 + checkpoint 测试。

核心场景：
1. save/load 往返（facts 恢复）
2. checkpoint 写盘 + load_checkpoint 恢复
3. merge_progress 累积事实（防 JPEG 效应）
4. add_fact 去重
5. 两层编排中 world_model 生命周期（plan/reflect 注入 summary）
"""
import json
import uuid
from pathlib import Path

import pytest

from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.local.tool_loop import ToolLoop, TaskSpec
from omni_core.local.world_model import WorldModel
import omni_core.local.runtime_paths as _RP

def _redirect(tmp_path):
    """把所有 task 资产重定向到临时目录（不污染真实 ~/.omniagent）。"""
    _RP._GLOBAL = tmp_path / ".omniagent"
    _RP.ensure_global_dirs()
    return _RP._GLOBAL



# --- 假后端 ---------------------------------------------------------------
class _FakeBackendInner:
    """模拟真实执行后端（self.exec.backend 调用点）。"""

    def verify_done(self, cond: str, percept: dict):
        ocr = (percept or {}).get("ocr_text") or []
        ok = any(cond and cond in str(o) for o in ocr)
        return ok, "ok" if ok else "no"

    def text_of(self, percept: dict) -> str:
        return " ".join((percept or {}).get("ocr_text") or [])


class _FakeBackend:
    tool_schemas = []

    def __init__(self, *a, **k):
        self.observe_calls = 0
        self.ocr = []
        self.backend = _FakeBackendInner()
        self.backend_kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

    def observe(self):
        self.observe_calls += 1
        return {"active_window": "com.fake", "ocr_text": list(self.ocr)}

    def screenshot(self):
        return {"ok": True, "path": "none"}

    def read_screen_text(self):
        return {"ok": True, "ocr_text": list(self.ocr)}

    def execute_mouse_action(self, action, args):
        return True

    def execute_keyboard_action(self, action, args):
        return True


# --- 假大脑 ----------------------------------------------------------------
def _fake_brain_factory(mapping):
    class _FB:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.model = cfg.get("model", "")
            self._script = list(mapping.get(self.model, []))
            self.calls = 0

        def chat(self, messages, tools=None, tool_choice="auto"):
            self.calls += 1
            if self._script:
                # 可调用项 = 常驻策略（按对话内容决定），不被消费
                if callable(self._script[0]):
                    return self._script[0](messages)
                return self._script.pop(0)
            return BrainReply(
                tool_calls=[ToolCall(name="task_done", args={"reason": "default"}, id="t")],
                finish_reason="stop",
            )

        def close(self):
            pass

    return _FB


def _dispatch(items):
    """M7：主 agent 派发子任务。"""
    return BrainReply(
        tool_calls=[ToolCall(name="dispatch", args={"items": json.dumps(items)}, id="d")],
        finish_reason="stop",
    )


def _task_done(reason="ok"):
    return BrainReply(
        tool_calls=[ToolCall(name="task_done", args={"reason": reason}, id="t")],
        finish_reason="stop",
    )


def _observe():
    return BrainReply(
        tool_calls=[ToolCall(name="observe", args={}, id="o1")],
        finish_reason="stop",
    )


def _auto_observe_then_done(reason="ok"):
    """内容驱动的假执行器（子 agent 并发共用实例，线性脚本会交错）：
    本轮还没工具结果 → observe；已有 → task_done。"""
    def _decide(messages):
        for m in reversed(messages or []):
            if m.get("role") == "tool":
                return _task_done(reason)
        return _observe()

    return _decide


BRAIN = "demo-model"
EXEC = "qwen3.5-4b-vl"


# === WorldModel 单元测试 =====================================================
class TestWorldModel:
    def test_save_and_load_roundtrip(self, tmp_path):
        """保存后加载——facts 恢复正确。"""
        wm = WorldModel(task_id="test_app")
        wm.run_id = "rid001"
        wm.objective = "测试目标"
        wm.add_fact("观察1")
        wm.add_fact("观察2")
        wm.update({"active_window": "test", "ocr_text": ["hello"]})
        wm.save()

        wm2 = WorldModel(task_id="test_app")
        ok = wm2.load()
        assert ok
        assert "观察1" in wm2.facts
        assert "观察2" in wm2.facts

    def test_load_nonexistent(self, tmp_path):
        """文件不存在时 load 返回 False。"""
        wm = WorldModel(task_id="nonexistent")
        assert wm.load() is False

    def test_checkpoint_write_and_load(self, tmp_path):
        """checkpoint 写盘后 load_checkpoint 可恢复。"""
        _redirect(tmp_path)
        wm = WorldModel(task_id="ckpt_test")
        wm.run_id = "r99"
        wm.objective = "目标"
        wm.add_fact("fact1")
        wm.update({"active_window": "x", "ocr_text": ["a"]})
        wm.log_action({"name": "click", "args": {"x": 0.5}}, {"ok": True})

        path = wm.checkpoint({"desc": "清房间1", "done_when": "ROOM_CLEAR"})
        assert Path(path).exists()

        ck = WorldModel.load_checkpoint("ckpt_test", "r99")
        assert ck is not None
        assert ck["subgoal"]["desc"] == "清房间1"
        assert "fact1" in ck["world_snapshot"]["facts"]

    def test_load_checkpoint_nonexistent(self, tmp_path):
        """不存在 run_id 时返回 None。"""
        assert WorldModel.load_checkpoint("no", "fake") is None

    def test_add_fact_dedup(self):
        """同一事实不重复累积。"""
        wm = WorldModel()
        wm.add_fact("fact_a")
        wm.add_fact("fact_a")
        wm.add_fact("fact_b")
        assert len(wm.facts) == 2
        assert wm.facts == ["fact_a", "fact_b"]

    def test_merge_progress(self):
        """压缩关键进展写回 facts，避免 JPEG 效应。"""
        wm = WorldModel()
        wm.merge_progress(["已完成步骤A", "已完成步骤B", "  "])
        assert "已完成步骤A" in wm.facts
        assert "已完成步骤B" in wm.facts
        assert len(wm.facts) == 2  # 空行被过滤

    def test_snapshot_includes_facts(self):
        """snapshot() 含 facts + state_text + recent_actions（去场景化，不暴露感知字段）。"""
        wm = WorldModel()
        wm.add_fact("f1")
        wm.update({"active_window": "w", "ocr_text": ["t"]})
        wm.log_action({"name": "click"}, "ok")
        snap = wm.snapshot()
        assert "state_text" in snap
        assert "f1" in snap["facts"]
        assert len(snap["recent_actions"]) == 1

    def test_collected_roundtrip_via_json(self, tmp_path):
        """采集条目存盘(collected.json)→加载后 text 字段不丢。

        固化上次「去 level 清理」遗留回归点：load 必须按 {"text"} 读取，
        而非旧 {"name","level"} schema，否则采集数据落盘再读会全丢。
        """
        wm = WorldModel(task_id="coll_test")
        assert wm.add_collected("苹果") is True
        assert wm.add_collected("香蕉") is True
        assert wm.add_collected("苹果") is False  # 同内容去重
        assert len(wm.collected) == 2
        wm.save()

        wm2 = WorldModel(task_id="coll_test")
        assert wm2.load()
        assert wm2.collected == [{"text": "苹果"}, {"text": "香蕉"}]
        # summary 能正确渲染（不依赖旧 name/level 字段）
        assert "苹果" in wm2.summary()

    def test_collected_markdown_fallback_parse(self, tmp_path):
        """collected.json 缺失时，从 world_model.md 的 # 采集记录 段解析为 {text}。

        确保 markdown 兜底路径也用新 {"text"} schema，不会回退到旧 {"name","level"}。
        """
        wm = WorldModel(task_id="coll_md")
        wm.add_collected("西瓜")
        wm.add_collected("橙子")
        wm.save()

        cj = _RP.tasks_root() / "coll_md" / "collected.json"
        assert cj.exists()
        cj.unlink()  # 模拟 collected.json 丢失，强制走 markdown 兜底解析

        wm2 = WorldModel(task_id="coll_md")
        assert wm2.load()
        assert wm2.collected == [{"text": "西瓜"}, {"text": "橙子"}]


# === M7 多 agent 编排中 world_model 生命周期 ================================
def test_two_layer_world_model_persistence(monkeypatch, tmp_path):
    """主 agent 派发 → 子 agent 执行 → world_model 落盘 + 子任务 checkpoint。"""
    fb = _fake_brain_factory({
        BRAIN: [_dispatch([{"desc": "s1", "done_when": "S1"}, {"desc": "s2", "done_when": "S2"}]),
                _task_done("all ok")],
        # 子 agent 并发共用同一个假大脑实例，用内容驱动的假执行器（与并发顺序无关）
        EXEC: [_auto_observe_then_done("ok")],
    })
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", fb)
    monkeypatch.setattr("omni_core.local.loop.core.ExecutionModule", _FakeBackend)

    loop = ToolLoop(
        {"model": BRAIN, "base_url": "http://x", "capabilities": {}},
        verbose=False,
        executor_cfg={"enabled": True, "model": EXEC, "base_url": "http://y", "capabilities": {"vision": True}},
    )
    loop.exec.ocr = ["S1", "S2", "ALL"]  # M4a.3 verify 门控需要（含主 agent 的 done_when）
    _redirect(tmp_path)

    res = loop.run_task(
        TaskSpec(objective="测试", done_when="ALL", task_id="m4b1_test")
    )
    assert res["success"] is True

    # world_model.md 已生成
    md = _RP.tasks_root() / "m4b1_test" / "world_model.md"
    assert md.exists()

    # checkpoints 已生成
    ckpt_dir = _RP.tasks_root() / "m4b1_test" / "checkpoints"
    ckpt_run = sorted(ckpt_dir.glob("*/*.json"))
    assert len(ckpt_run) == 2  # 两个子任务各一个 checkpoint


def test_two_layer_world_model_early_save_on_abort(monkeypatch, tmp_path):
    """编排异常提前返回——early return 前仍 save。"""
    fb = _fake_brain_factory({
        BRAIN: [
            _dispatch([{"desc": "s1", "done_when": "S1"}]),
            _task_done("abort"),
        ],
        EXEC: [_observe()],  # observe → world 有 OCR → verify 通过 → success
    })
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", fb)
    monkeypatch.setattr("omni_core.local.loop.core.ExecutionModule", _FakeBackend)

    loop = ToolLoop(
        {"model": BRAIN, "base_url": "http://x", "capabilities": {}},
        verbose=False,
        executor_cfg={"enabled": True, "model": EXEC, "base_url": "http://y", "capabilities": {"vision": True}},
    )
    loop.exec.ocr = ["S1"]  # verify 通过
    _redirect(tmp_path)

    res = loop.run_task(
        TaskSpec(objective="测试", done_when="S1", task_id="abort_test")
    )
    assert res["success"] is True  # 子任务完成, 没有 escalate

    # 无论成功/失败，world_model 都应 save
    md = _RP.tasks_root() / "abort_test" / "world_model.md"
    assert md.exists()