"""M3b / M7 多 agent 执行编排测试：双客户端 / 派发扇出 / 升级阈值。

M7 起不再是「planner 规划 + worker 执行」的固定两层：
- 主 agent（BRAIN 模型）默认自己把任务做完；
- 需要并行时主 agent 调 `dispatch` 产出派发计划，子 agent（EXEC 模型）并发执行。

全部用 monkeypatch 替换 LLMClient（按 model 分发脚本回复）与 ExecutionModule，
不依赖真实网络 / 模型 / 模拟器。
"""

# 随手搓循环 `_run_inner` 退役删除（Phase 0）：M3b.5 worker 历史按轮裁剪相关用例
# （test_m3b5_worker_history_keep_three_rounds / test_m3b5_no_keep_accumulates /
# test_brain_not_three_round_when_long_task_disabled）及其辅助函数，SDK 路径无对应语义。

import json

from omni_core.brain.llm import BrainReply, ToolCall
from omni_core.local.loop import ToolLoop, TaskSpec


# --- 假后端（ExecutionModule 替身） -----------------------------------------
from tests._env import install_fake_env  # noqa: E402


class _FakeBackend:
    # 注：M4 起后端不再声明 tool_schemas（能力清单由 tool 插件层提供）

    def __init__(self, *a, **k):
        self.observe_calls = 0
        self.ocr = []
        self.backend = _FakeBackendInner()  # 可配置 OCR 文本（M4a.3 verify 门控需要屏幕含 done_when）
        self.kind = "host"  # 阶段 0.5：ExecutionModule 契约字段（host 模式，不暴露 Android 工具）

    def observe(self):
        self.observe_calls += 1
        return {"active_window": "com.fake.game", "ocr_text": list(self.ocr)}

    def screenshot(self):
        return {"ok": True, "path": "none"}

    def read_screen_text(self):
        return {"ok": True, "ocr_text": list(self.ocr)}

    def execute_mouse_action(self, action, args):
        return True

    def execute_keyboard_action(self, action, args):
        return True


# --- 假大脑工厂（按 model 分发脚本回复） ------------------------------------
def _fake_brain_factory(mapping: dict):
    class _FakeBrain:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.model = cfg.get("model", "")
            self._script = list(mapping.get(self.model, []))
            self.calls = 0

        def chat(self, messages, tools=None, tool_choice="auto"):
            self.calls += 1
            if self._script:
                # 可调用项 = 常驻策略（按对话内容决定回复），不被消费；
                # 普通项 = 一次性脚本，按顺序消费。
                if callable(self._script[0]):
                    return self._script[0](messages)
                return self._script.pop(0)
            return BrainReply(
                tool_calls=[ToolCall(name="task_done", args={"reason": "default"}, id="c")],
                finish_reason="stop",
            )

        def close(self):
            pass

    return _FakeBrain


# --- 回复构造助手 -----------------------------------------------------------
def _dispatch(items):
    """M7：主 agent 派发子任务（items 走 JSON 串，与真实模型序列化习惯一致）。"""
    return BrainReply(
        tool_calls=[ToolCall(name="dispatch", args={"items": json.dumps(items, ensure_ascii=False)}, id="d1")],
        finish_reason="stop",
    )


def _task_done(reason="ok"):
    return BrainReply(
        tool_calls=[ToolCall(name="task_done", args={"reason": reason}, id="t1")],
        finish_reason="stop",
    )


def _observe():
    """调 observe 获取屏幕状态（新架构：大脑自主选择感知工具）。"""
    return BrainReply(
        tool_calls=[ToolCall(name="observe", args={}, id="o1")],
        finish_reason="stop",
    )


def _auto_observe_then_done(reason="ok"):
    """内容驱动的假执行器：还没观测过就 observe，观测过就 task_done。

    子 agent 是并发的、共用同一个假大脑实例，线性脚本会被交错取用导致测试不稳定；
    改为按「本轮对话里有没有工具结果」决定，与并发顺序无关。
    """
    def _decide(messages):
        for m in reversed(messages or []):
            if m.get("role") == "tool":
                return _task_done(reason)
        return _observe()

    return _decide


def _ocr_screenshot():
    return BrainReply(
        tool_calls=[ToolCall(name="ocr_screenshot", args={}, id="o2")],
        finish_reason="stop",
    )


def _escalate(reason="stuck"):
    return BrainReply(
        tool_calls=[ToolCall(name="escalate", args={"reason": reason}, id="e1")],
        finish_reason="stop",
    )


def _verify(cond=""):
    return BrainReply(
        tool_calls=[ToolCall(name="verify", args={"condition": cond}, id="v1")],
        finish_reason="stop",
    )


def _click():
    return BrainReply(
        tool_calls=[ToolCall(name="click", args={"x": 0.5, "y": 0.5}, id="c1")],
        finish_reason="stop",
    )


BRAIN = "demo-model"
EXEC = "qwen3.5-4b-vl"


def _make_loop(monkeypatch, brain_mapping, executor_mapping, *, executor_cfg=None, escalation_cfg=None, ocr=None):
    fb = _fake_brain_factory({BRAIN: brain_mapping, EXEC: executor_mapping})
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", fb)
    install_fake_env(monkeypatch, _FakeBackend)
    ecfg = executor_cfg if executor_cfg is not None else {
        "enabled": True, "model": EXEC, "base_url": "http://127.0.0.1:9", "capabilities": {"vision": True},
    }
    loop = ToolLoop(
        {"model": BRAIN, "base_url": "http://127.0.0.1:9", "capabilities": {}},
        verbose=False,
        executor_cfg=ecfg,
        escalation_cfg=escalation_cfg,
    )
    if ocr is not None:
        loop.exec.ocr = ocr
    return loop


# === M3b.1 第二路客户端 =====================================================
def test_m3b1_executor_constructed_when_enabled(monkeypatch):
    loop = _make_loop(monkeypatch, brain_mapping=[], executor_mapping=[])
    assert loop.executor is not None
    assert loop.executor_capabilities.get("vision") is True


def test_m3b1_no_executor_when_disabled(monkeypatch):
    """未单独配置子 agent 模型时由主模型兼任。

    设计变更：双 agent 架构不退化，executor 总非 None，只是指向 brain。
    """
    loop = _make_loop(monkeypatch, brain_mapping=[], executor_mapping=[], executor_cfg={"enabled": False})
    assert loop.executor is not None
    assert loop.executor is loop.brain
    assert loop.executor_is_planner is True


def test_m3b1_two_layer_executor_is_brain_when_disabled(monkeypatch):
    """executor disabled 时编排仍可跑（子 agent 由主模型兼任）。

    M7：主 agent 默认自己做完整任务，不派发 → 单 agent 路径。
    """
    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_observe(), _task_done("s1 ok")],
        executor_mapping=[],
        executor_cfg={"enabled": False},
        ocr=["ALL"],
    )
    res = loop.run_task(TaskSpec(objective="o", done_when="ALL"))
    assert res["success"] is True


# === M7 派发计划解析（内核零领域假设：只认 desc / done_when） ================
def test_m7_dispatch_items_normalization():
    from omni_core.brain.sdk_loop import parse_dispatch_items

    # 标准 JSON 数组字符串
    assert parse_dispatch_items('[{"desc": "a", "done_when": "A"}]') == [
        {"desc": "a", "done_when": "A"}]
    # 单个对象 / 纯文本（不同模型的序列化习惯）
    assert parse_dispatch_items({"desc": "b"}) == [{"desc": "b", "done_when": ""}]
    assert parse_dispatch_items("纯文本描述") == [{"desc": "纯文本描述", "done_when": ""}]
    assert parse_dispatch_items(None) == []


# === M7 多 agent 编排 =======================================================
def test_m7_happy_path(monkeypatch):
    """主 agent 派发 2 项 → 子 agent 各 observe+task_done → 主 agent 收尾成功。"""
    loop = _make_loop(
        monkeypatch,
        brain_mapping=[
            _dispatch([{"desc": "s1", "done_when": "S1"}, {"desc": "s2", "done_when": "S2"}]),
            _task_done("all ok"),
        ],
        executor_mapping=[_auto_observe_then_done("sub ok")],
        ocr=["S1", "S2", "ALL"],  # verify 门控：屏幕文本需含各自的 done_when
    )
    # 必须显式给步数预算：缺省 max_steps=None 在内核语义上是「不限」（见 TaskSpec），
    # 子 agent 策略若感知不到 role=="tool" 就会无界 observe → 按序全量时 CPU 满核卡死。
    res = loop.run_task(TaskSpec(objective="o", done_when="ALL", max_steps=20))
    assert res["success"] is True
    assert len(res["subtask_results"]) == 2
    assert all(r["success"] for r in res["subtask_results"]), res["subtask_results"]


def test_m7_sub_escalate_main_gives_up(monkeypatch):
    """子 agent 升级 → 主 agent 收到失败结果后仍无法达成 → 整体失败。"""
    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("give up")],
        executor_mapping=[_escalate("unknown ui")],
        ocr=[],
    )
    # 主链（allow_escalate=False）无 verify_fail 升级闸门，终止靠步数预算——
    # 必须显式传 max_steps（生产 /chat 恒回退 runtime.default_max_steps=40），
    # 否则 done_when 不可满足时内层循环无上界。
    res = loop.run_task(TaskSpec(objective="o", done_when="NEVER", max_steps=6))
    assert res["success"] is False
    assert res["subtask_results"][0]["success"] is False


def test_m7_main_can_redispatch_after_failure(monkeypatch):
    """子任务失败 → 主 agent 自行决定是否重派发（重规划权在主 agent，不在编排层）。"""
    loop = _make_loop(
        monkeypatch,
        brain_mapping=[
            _dispatch([{"desc": "s1", "done_when": "S1"}]),
            _dispatch([{"desc": "n1", "done_when": "N1"}]),
            _task_done("all done"),
        ],
        executor_mapping=[_escalate("stuck"), _observe(), _task_done("n1")],
        ocr=["N1", "ALL"],  # S1 不在屏幕 → 首轮失败；N1 在 → 重派发后成功
    )
    res = loop.run_task(TaskSpec(objective="o", done_when="ALL"))
    assert res["success"] is True
    assert res["rounds"] == 3


# === M3b.4 升级阈值 =========================================================
def test_m3b4_verify_fail_max(monkeypatch):
    """子 agent 连续 verify 失败达阈值 -> 升级 -> 子任务失败。"""
    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("abort")],
        executor_mapping=[_verify(), _verify(), _verify()],
        escalation_cfg={"verify_fail_max": 3, "inner_step_max": 20, "wallclock_sec": 9999,
                        "no_confidence_hard": True, "context_near_limit": True},
    )
    # 同 test_m7_sub_escalate_main_gives_up：主链终止靠步数预算，需显式 max_steps。
    res = loop.run_task(TaskSpec(objective="o", done_when="NEVER", max_steps=6))
    assert res["success"] is False
    assert res["subtask_results"][0]["success"] is False


def test_m3b4_inner_step_max(monkeypatch):
    """子 agent 打转（始终 click 无进展）耗尽分到的预算 -> budget_exhausted。

    设计变更：inner_step_max 已移除，改用 spec.max_steps 作为顶层总预算，
    由编排层在主 agent / 各子 agent 之间分配。
    """
    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("abort")],
        executor_mapping=[_click(), _click(), _click(), _click(), _click()],
        escalation_cfg={"verify_fail_max": 99, "wallclock_sec": 9999,
                        "no_confidence_hard": True},
    )
    res = loop.run_task(TaskSpec(objective="o", done_when="NEVER", max_steps=3))
    assert res["success"] is False
    assert any("budget_exhausted" in r["reason"] for r in res["subtask_results"])


def test_m3b4_no_confidence_hard(monkeypatch, tmp_path):
    """工具结果标记 no_confidence -> 立即升级。"""
    # 真实走一遍「无置信」：template_match 在这张截图里找不到模板 -> no_confidence
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(3)
    base_p = tmp_path / "base.png"
    templ_p = tmp_path / "templ.png"
    Image.fromarray(rng.integers(0, 255, (120, 120, 3), dtype=np.uint8)).save(base_p)
    Image.fromarray(rng.integers(0, 255, (20, 20, 3), dtype=np.uint8)).save(templ_p)

    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("abort")],
        executor_mapping=[BrainReply(tool_calls=[
            ToolCall(name="template_match",
                     args={"template_path": str(templ_p), "threshold": 0.8}, id="m1"),
        ], finish_reason="stop")],
        escalation_cfg={"verify_fail_max": 99, "inner_step_max": 20, "wallclock_sec": 9999,
                        "no_confidence_hard": True, "context_near_limit": True},
    )
    loop.exec.screenshot = lambda save_path=None: {"ok": True, "path": str(base_p)}
    # 主链终止靠步数预算（见 test_m7_sub_escalate_main_gives_up）。
    res = loop.run_task(TaskSpec(objective="o", done_when="NEVER", max_steps=6))
    assert res["success"] is False


def test_m3b4_wallclock(monkeypatch):
    """子 agent 墙钟超时（阈值设为负，首步即触发）-> 升级。"""
    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("abort")],
        executor_mapping=[_click()],
        escalation_cfg={"verify_fail_max": 99, "inner_step_max": 20, "wallclock_sec": -1,
                        "no_confidence_hard": True, "context_near_limit": True},
    )
    # 主链终止靠步数预算（见 test_m7_sub_escalate_main_gives_up）。
    res = loop.run_task(TaskSpec(objective="o", done_when="NEVER", max_steps=6))
    assert res["success"] is False


# === 单大脑 run_task 回归（重构不应改变行为） ==============================
def test_single_brain_run_task_backward_compat(monkeypatch):
    """单大脑先 observe 再 task_done -> 成功，且 observe 路由到后端。"""
    fb = _fake_brain_factory({BRAIN: [_observe(), _task_done("ok")]})
    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", fb)
    install_fake_env(monkeypatch, _FakeBackend)
    loop = ToolLoop({"model": BRAIN, "base_url": "http://127.0.0.1:9", "capabilities": {}}, verbose=False)
    res = loop.run_task(TaskSpec(objective="o", max_steps=3))
    assert res["steps"] >= 1
    assert res["reason"] == "ok"
    assert loop.exec.observe_calls >= 1


# === M3b 健壮性：worker 内 brain.chat 异常应转 escalate（而非崩溃） =========
def test_m3b_worker_brain_chat_exception_escalates(monkeypatch):
    """本地执行器 brain.chat 抛异常（如 ctx 溢出 HTTP 400）-> 升级回在线大脑。"""
    def _boom(*a, **k):
        raise RuntimeError("ctx overflow 8748 > 8192")

    loop = _make_loop(
        monkeypatch,
        brain_mapping=[_dispatch([{"desc": "s1", "done_when": "S1"}]), _task_done("abort")],
        executor_mapping=[],
    )
    loop.executor.chat = _boom  # 让子 agent 的 chat 直接抛异常
    # 主链终止靠步数预算（见 test_m7_sub_escalate_main_gives_up）。
    res = loop.run_task(TaskSpec(objective="o", done_when="NEVER", max_steps=6))
    assert res["success"] is False
    # 框架态：Runner 内的模型调用异常被 L2 捕获并升级（不再手搓 brain.chat）
    sub_reasons = " ".join(r["reason"] for r in res["subtask_results"])
    assert "异常" in sub_reasons
    assert "ctx overflow" in sub_reasons




# === 大脑长任务自动压缩（对照 worker 的 3 轮硬截断） ======================
def test_brain_long_task_runs_to_completion(monkeypatch):
    """单大脑长任务：多步推进后仍能正常收尾（历史由框架 Runner 托管）。

    框架态变更：历史不再由内核逐轮拼装，而是由 SDK Runner 通过
    `to_input_list()` 托管；L2 只在检查点（verify/task_done）之间做压缩。
    因此这里验证「长任务能跑完且步数被正确记账」。
    """
    captured = []

    class _RecBrain:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.model = cfg.get("model", "")
            self.n = 0

        def chat(self, messages, tools=None, tool_choice="auto"):
            captured.append(messages)
            self.n += 1
            # 中途调一次 verify：制造 L2 检查点（框架态下压缩发生在检查点之间）
            if self.n == 4:
                return BrainReply(tool_calls=[ToolCall(name="verify", args={}, id="v")], finish_reason="stop")
            if self.n <= 8:
                return BrainReply(tool_calls=[ToolCall(name="screenshot", args={}, id="s")], finish_reason="stop")
            return BrainReply(tool_calls=[ToolCall(name="task_done", args={"reason": "done"}, id="t")], finish_reason="stop")

        def close(self):
            pass

    monkeypatch.setattr("omni_core.local.loop.core.LLMClient", _RecBrain)
    install_fake_env(monkeypatch, _FakeBackend)
    loop = ToolLoop(
        {"model": "brain", "base_url": "http://127.0.0.1:9", "capabilities": {},
         "long_task": {"enabled": True, "max_turns": 2, "compress": True}},
        verbose=False,
    )
    res = loop.run_task(TaskSpec(objective="o", max_steps=20))
    assert res["success"] is True
    assert res["steps"] >= 4, f"长任务步数记账异常: {res}"
    # 历史确实在累积（框架托管）：后续调用带上更早的 assistant/tool 轮次
    assert any(
        len([m for m in msgs if m.get("role") in ("assistant", "tool")]) >= 2
        for msgs in captured
    )




class _FakeBackendInner:
    """模拟真实执行后端（self.exec.backend 调用点）。"""
    def verify_done(self, cond: str, percept: dict):
        ocr = (percept or {}).get("ocr_text") or []
        return (any(cond and cond in str(o) for o in ocr), "ok" if cond else "no")
    def text_of(self, percept: dict) -> str:
        return " ".join((percept or {}).get("ocr_text") or [])

