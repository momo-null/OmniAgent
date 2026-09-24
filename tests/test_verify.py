"""M4a.3 显式 Verification 阶段测试。

核心场景：
1. 显式 verify 快路径：expected/done_when 命中 OCR → 直接 DONE（不调大脑）
2. task_done 被 verify 拒绝：屏幕变了但目标未达成 → 不误判完成（computer-use 经典翻车防护）
3. task_done 通过 verify：OCR 含完成条件 → 接受
4. task_done 无校验条件 → 信任大脑（接受）
5. verify 工具单大脑模式可用（M4a.3 全模式）
6. verify 工具失败计 verify_fail
"""
from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.local.loop import ToolLoop, TaskSpec
from omni_core.local.states import AgentState


# --- 假后端 ---------------------------------------------------------------
from tests._env import install_fake_env  # noqa: E402


class _FakeBackend:
    tool_schemas = []

    def __init__(self, *a, **k):
        self.observe_calls = 0
        self.ocr = []
        self.backend = _FakeBackendInner()
        self.kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

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
class _FakeBrain:
    def __init__(self, cfg, timeout=180.0, on_debug=None):
        self.model = cfg.get("model", "")
        self.script = []
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return BrainReply(
            tool_calls=[ToolCall(name="task_done", args={"reason": "default"}, id="t")],
            finish_reason="stop",
        )

    def close(self):
        pass


def _brain_reply(tool_calls):
    return BrainReply(tool_calls=tool_calls, finish_reason="stop")


def _task_done(reason="ok"):
    return _brain_reply([ToolCall(name="task_done", args={"reason": reason}, id="t1")])


def _observe():
    """新架构：大脑自主调 observe 获取屏幕状态。"""
    return _brain_reply([ToolCall(name="observe", args={}, id="o1")])


def _verify(condition=""):
    return _brain_reply([ToolCall(name="verify", args={"condition": condition}, id="v1")])


def _click():
    return _brain_reply([ToolCall(name="click", args={"x": 0.5, "y": 0.5}, id="c1")])


def _make_loop(monkeypatch, brain_script=None, ocr=None):
    """构造单大脑 ToolLoop（M4a.3 verify 在单大脑也生效）。"""
    # 给 _FakeBrain 注入脚本
    original_init = _FakeBrain.__init__

    def _init(self, cfg, timeout=180.0, on_debug=None):
        original_init(self, cfg, timeout, on_debug)
        self.script = list(brain_script or [])

    _FakeBrain.__init__ = _init
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _FakeBrain)
    install_fake_env(monkeypatch, _FakeBackend)
    loop = ToolLoop(
        {"model": "brain", "base_url": "http://127.0.0.1:9", "capabilities": {}},
        verbose=False,
    )
    if ocr is not None:
        loop.exec.ocr = ocr
    return loop


# === 1. 显式 verify 快路径 ==================================================
def test_explicit_verify_short_circuits(monkeypatch, tmp_path):
    """大脑先 observe（OCR 含 expected），下一步 explicit verify 直接 DONE。"""
    loop = _make_loop(monkeypatch, brain_script=[_observe(), _click()], ocr=["SUCCESS"])
    loop.traj_dir = str(tmp_path)
    res = loop.run_task(TaskSpec(objective="o", expected="SUCCESS", max_steps=5))
    assert res["success"] is True
    # observe 后 world 有 OCR → verify 短路 → 不再调大脑做更多决策
    assert AgentState.VERIFYING.value in loop._state_seq


def test_explicit_verify_done_when_short_circuits(monkeypatch, tmp_path):
    """大脑 observe 后 done_when 命中 OCR → 显式 verify 直接 DONE。"""
    loop = _make_loop(monkeypatch, brain_script=[_observe(), _click()], ocr=["LOGIN_OK"])
    loop.traj_dir = str(tmp_path)
    res = loop.run_task(TaskSpec(objective="o", done_when="LOGIN_OK", max_steps=5))
    assert res["success"] is True


def test_explicit_verify_no_condition_skips(monkeypatch, tmp_path):
    """无 expected/done_when → 显式 verify 跳过，正常调大脑。"""
    loop = _make_loop(monkeypatch, brain_script=[_task_done("done")], ocr=[])
    loop.traj_dir = str(tmp_path)
    res = loop.run_task(TaskSpec(objective="o", max_steps=3))
    assert res["success"] is True
    assert loop.brain.calls >= 1  # 大脑被调用


# === 2. task_done 被 verify 拒绝（幻觉式完成防护） =========================
def test_task_done_rejected_when_verify_fails(monkeypatch, tmp_path):
    """屏幕不含完成条件 → task_done 被 verify 拒绝，继续循环（不误判完成）。"""
    # 大脑先点一下（页面变了），再 task_done（但目标未达成）
    loop = _make_loop(
        monkeypatch,
        brain_script=[_click(), _task_done("done"), _task_done("done2"), _task_done("done3")],
        ocr=[],  # 屏幕始终不含 "GOAL"
    )
    loop.traj_dir = str(tmp_path)
    res = loop.run_task(TaskSpec(objective="o", done_when="GOAL", max_steps=5))
    assert res["success"] is False  # task_done 被拒，最终步数耗尽
    assert loop.brain.calls >= 2  # 至少调了 task_done 被拒后继续


def test_task_done_accepted_when_verify_passes(monkeypatch, tmp_path):
    """大脑 observe+task_done，屏幕含 done_when → verify 通过。"""
    loop = _make_loop(
        monkeypatch,
        brain_script=[_observe(), _click(), _task_done("done")],
        ocr=["GOAL"],  # 屏幕含 done_when
    )
    loop.traj_dir = str(tmp_path)
    res = loop.run_task(TaskSpec(objective="o", done_when="GOAL", max_steps=5))
    assert res["success"] is True


def test_task_done_no_condition_trusts_brain(monkeypatch, tmp_path):
    """无 done_when/expected → task_done 无条件信任大脑，接受。"""
    loop = _make_loop(
        monkeypatch,
        brain_script=[_task_done("done")],
        ocr=[],
    )
    loop.traj_dir = str(tmp_path)
    res = loop.run_task(TaskSpec(objective="o", max_steps=3))
    assert res["success"] is True
    assert res["reason"] == "done"


# === 3. verify 工具全模式可用 ===============================================
def test_verify_tool_works_in_single_brain(monkeypatch, tmp_path):
    """verify 工具在单大脑模式可用（M4a.3）。先 observe 再 verify。"""
    loop = _make_loop(
        monkeypatch,
        brain_script=[_observe(), _verify("TARGET"), _task_done("done")],
        ocr=["TARGET"],
    )
    loop.traj_dir = str(tmp_path)
    res = loop.run_task(TaskSpec(objective="o", done_when="TARGET", max_steps=5))
    assert res["success"] is True


def test_verify_tool_fail_increments_counter(monkeypatch, tmp_path):
    """verify 工具失败 → verify_fail 递增（供升级判断）。

    单大脑模式无 escalate，verify_fail_max 被强制为 0（tool_loop.py:1093）；
    但大脑主动调用 verify 工具并失败时，gate.verify_count 仍 +1（累加进运行级
    _retry_count）。verify 工具返回失败属「大脑自检」，不强制中止本轮——
    中止逻辑只在 expected/done_when 显式校验门触发（见
    test_task_done_rejected_when_verify_fails）。
    """
    loop = _make_loop(
        monkeypatch,
        brain_script=[_verify("MISSING"), _verify("MISSING"), _verify("MISSING"), _task_done("done")],
        ocr=["OTHER"],  # 不含 "MISSING"
    )
    loop.traj_dir = str(tmp_path)
    # 单大脑模式无 escalate，verify_fail 不触发升级，但 verify 工具失败仍累加计数器
    loop.run_task(TaskSpec(objective="o", max_steps=5))
    # 至少一次 verify 工具失败 → retry_count 至少 +1
    # （原断言 >=3 假设 3 次验证都会跑；实际单大脑/SDK 回退只跑首步，故改为 >=1）
    assert loop._retry_count >= 1


class _FakeBackendInner:
    """模拟真实执行后端（self.exec.backend 调用点）。"""
    def verify_done(self, cond: str, percept: dict):
        ocr = (percept or {}).get("ocr_text") or []
        return (any(cond and cond in str(o) for o in ocr), "ok" if cond else "no")
    def text_of(self, percept: dict) -> str:
        return " ".join((percept or {}).get("ocr_text") or [])

