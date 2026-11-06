"""单链路参数化（mode）与 lane / outbox 多播 / 注意力预算单测（plan §3.1 / §3.4 / §4）。

不拉起真实模型：只验证**契约层**——mode 决定哪一档参数、lane 决定谁能并发、
outbox 能否多订阅、任务细节能否按需拉取。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.api.routers import chat_runtime as cr  # noqa: E402
from backend.api.routers import helpers as hp  # noqa: E402
from backend.api.runtime_manager import RuntimeManager  # noqa: E402


# ---------------------------------------------------------------- lane（§4）

def test_same_lane_concurrent_tasks_and_lane_stats():
    """并发化（2026-11-05）：同 lane 内不同 task 可并行；lane 降级为诊断元数据，
    is_any_running(lane) 仍按 lane 统计。"""
    m = RuntimeManager()
    assert m.try_start("t_chat", lane="chat") is True
    assert m.try_start("t_chat2", lane="chat") is True, "并发化：同 lane 不同 task 可并行"
    assert m.try_start("t_work", lane="work") is True, "工作 lane 同样可跑"
    assert m.is_any_running("chat") is True
    assert m.is_any_running("work") is True
    assert m.is_any_running(None) is True
    assert sorted(m.running_task_ids("chat")) == ["t_chat", "t_chat2"]
    assert m.running_task_ids("work") == ["t_work"]

    m.finish("t_chat")
    assert m.is_any_running("chat") is True        # t_chat2 仍在跑
    assert m.is_any_running("work") is True


def test_same_task_never_runs_twice_across_lanes():
    """唯一互斥：同一个 task 无论如何不允许并发 run（会话/轨迹按 task 落盘，必须串行）。"""
    m = RuntimeManager()
    assert m.try_start("t_same", lane="work") is True
    assert m.try_start("t_same", lane="chat") is False
    assert m.try_start("t_same", lane="work") is False
    assert m.try_start("t_same2", lane="chat") is True, "别的 task 不受影响"


# ------------------------------------------------------- did_work（§3.3 门）

def test_did_work_ignores_meta_tools():
    steps = [{"type": "tool_call", "name": "dispatch"},
             {"type": "tool_call", "name": "memory_remember"},
             {"type": "tool_call", "name": "verify"}]
    assert cr._did_work(steps, {}) is False, "派活/记忆/校验不算产出事实"


def test_did_work_true_on_real_tool_or_handoff():
    assert cr._did_work([{"type": "tool_call", "name": "shell_exec"}], {}) is True
    assert cr._did_work([], {"dispatch_plan": [{"objective": "x"}]}) is True
    assert cr._did_work([{"type": "thinking"}], {}) is False


# --------------------------------------------------- outbox 多播（M3 前置）

def test_outbox_multicast_primary_and_side_subscriber():
    """主订阅者消费即清空；旁路订阅者按游标取新增，两者互不抢事件。"""
    tid = "t_multicast_unit"
    hp.outbox_reset_cursor(tid, "voice")

    hp.push_chat("user", "第一句", task_id=tid)
    hp.push_chat("agent", "第一答", task_id=tid)

    side = hp.outbox_drain(tid, "voice")
    assert [e["text"] for e in side["chat"]] == ["第一句", "第一答"]

    main = hp.outbox_drain(tid)
    assert [e["text"] for e in main["chat"]] == ["第一句", "第一答"]
    assert hp.outbox_drain(tid)["chat"] == [], "主订阅者是消费即清空"

    hp.push_chat("agent", "第二答", task_id=tid)
    side2 = hp.outbox_drain(tid, "voice")
    assert [e["text"] for e in side2["chat"]] == ["第二答"], "旁路只拿新增"
    assert hp.outbox_drain(tid, "voice")["chat"] == [], "游标推进后不重复投递"


def test_outbox_stream_chunks_get_new_seq():
    """流式块每次 upsert 换新 seq：旁路订阅者能持续拿到最新全文。"""
    tid = "t_multicast_stream"
    hp.outbox_reset_cursor(tid, "voice")
    hp.push_message_stream("brain", "m", f"b-{tid}", "你", task_id=tid)
    first = hp.outbox_drain(tid, "voice")["message"]
    hp.push_message_stream("brain", "m", f"b-{tid}", "你好", task_id=tid)
    second = hp.outbox_drain(tid, "voice")["message"]
    assert [e["content"] for e in first] == ["你"]
    assert [e["content"] for e in second] == ["你好"], "同一 block 的更新也要再投一次"


def test_outbox_side_subscriber_reconnect_starts_at_turn_floor():
    """断线重连的旁路订阅从**本轮派发点**起算：不重放上一轮，也不丢本轮。

    回归：手机端每轮新开一条 SSE（sub 同名），旧实现无游标即从 0 取，把上一轮的
    message/chat 全量重放 → 发第二条消息时上一条回复重复出现。
    """
    tid = "t_multicast_reconnect"
    hp.outbox_reset_cursor(tid, "phone")
    hp.push_chat("user", "上一轮问", task_id=tid)
    hp.push_chat("agent", "上一轮答", task_id=tid)
    hp.outbox_reset_cursor(tid, "phone")          # 上一轮的订阅断开

    hp.mark_turn_start(tid)                        # 新一轮派发点
    hp.push_chat("user", "这一轮问", task_id=tid)
    hp.push_message_stream("brain", "m", f"b-{tid}", "这一轮口播", task_id=tid)

    got = hp.outbox_drain(tid, "phone")
    assert [e["text"] for e in got["chat"]] == ["这一轮问"], "上一轮不重放"
    assert [e["content"] for e in got["message"]] == ["这一轮口播"], "本轮事件不丢"


# ------------------------------------------- 注意力预算：任务卡 + 按需拉取

def test_task_card_line_is_bounded():
    steps = [{"type": "tool_call", "name": n} for n in
             ("shell_exec", "write_file", "read_file", "web_fetch")]
    line = hp._task_card_line(steps, "结论很长" * 30)
    assert line.startswith("【任务卡】执行 4 步")
    assert len(line) <= hp._TASK_CARD_MAX


def test_session_records_collapse_to_cards():
    """跨轮不再回灌完整 steps，只给结论 + 一行任务卡。"""
    records = [
        {"role": "user", "content": "跑一下脚本"},
        {"role": "assistant", "content": "跑完了",
         "extra": {"steps": [{"type": "tool_call", "name": "shell_exec",
                              "arguments": "{}", "result": "x" * 500}]}},
    ]
    out = hp._session_records_to_model_messages(records)
    assert [m["role"] for m in out] == ["user", "assistant"]
    assert "tool_calls" not in out[1], "tools 细节不回灌对话上下文"
    assert "【任务卡】执行 1 步（shell_exec）" in out[1]["content"]


def test_session_records_fold_old_cards():
    """卡片超过上限时最旧的折叠成一行提示（滚动合并，不无限膨胀）。"""
    records = []
    for i in range(hp._TASK_CARD_KEEP + 3):
        records.append({"role": "assistant", "content": f"结论{i}",
                        "extra": {"steps": [{"type": "tool_call", "name": "shell_exec"}]}})
    out = hp._session_records_to_model_messages(records)
    folded = [m for m in out if "已折叠" in m["content"]]
    assert len(folded) == 1 and "更早的 3 轮" in folded[0]["content"]


def test_read_task_digest_returns_card_and_steps():
    """read_task_digest：目标/状态/结论 + 最近工具调用（纯读、有硬上限）。"""
    from omni_core.local.runtime_paths import task_trajectory
    from omni_core.local.task_store import TaskStore
    from omni_core.tools.task_digest_tool import read_task_digest, task_card

    meta = TaskStore.create(objective="统计昨天的成交额")
    tid = meta["task_id"]
    TaskStore.update(tid, state="done")

    d = task_trajectory(tid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "t.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"action": {"tool": "shell_exec", "args": {"cmd": "ls"}}, "result": "a.txt"},
        {"thinking": "无关条目应被忽略"},
    ]), encoding="utf-8")

    card = task_card(tid)
    assert "统计昨天的成交额" in card["card"] and card["state"] == "done"

    got = read_task_digest(tid)
    assert got["ok"] is True
    assert got["task_id"] == tid
    assert got["steps"] and got["steps"][0]["tool"] == "shell_exec"


def test_read_task_digest_unknown_task():
    from omni_core.tools.task_digest_tool import read_task_digest

    assert read_task_digest("t_not_exist_zzz")["ok"] is False


# ------------------------------------------------- mode 参数（§3.1）

def test_voice_prompt_is_dialog_oriented():
    """chat 版的 system 是人格/对话取向，不含任务框架纪律。"""
    from omni_core.brain.prompt import build_system_prompt, build_voice_prompt

    voice = build_voice_prompt({"vision": True})
    assert "对话" in voice and "dispatch" in voice
    assert "task_done" in voice, "要明确禁止把闲聊当任务收尾"
    assert "核心纪律" not in voice, "不该带工作台那套 12 条纪律"

    work = build_system_prompt({"vision": True}, [])
    assert "核心纪律" in work and "【任务目标】" not in work


def test_chat_mode_keeps_full_toolset(monkeypatch, enable_plugin):
    """2026-10-29 mode 收窄：mode 不再裁剪工具面——chat 与 auto 工具集完全一致，
    重工具同样暴露（可用性归插件 enabled 与 tools_enabled 通道门）。"""
    from omni_core.local.loop import core as tl
    from omni_core.tools.loader import load_plugins
    from tests._env import install_fake_env

    class _FakeExec:
        kind = "host"

        def text_of(self, percept):
            return ""

        def verify_done(self, condition, percept):
            return (False, "noop")

    install_fake_env(monkeypatch, _FakeExec)
    enable_plugin("vision")
    monkeypatch.setattr(tl.config, "load_config", lambda: {})
    load_plugins({})
    cfg = {"model": "m", "base_url": "http://localhost:9", "api_key": "test"}
    auto = tl.ToolLoop(brain_cfg=cfg, mode="auto")
    chat = tl.ToolLoop(brain_cfg=cfg, mode="chat")
    assert auto.tool_schemas == chat.tool_schemas

    def _name(s):
        fn = s.get("function") if isinstance(s, dict) else None
        return (fn or s).get("name") if isinstance(fn or s, dict) else ""
    names = {_name(s) for s in chat.tool_schemas}
    assert "vision_describe" in names, "重工具不再被 mode 档裁剪"


def test_mode_brain_cfg_removed():
    """2026-10-29 mode 收窄：mode 不再选模型槽（chat 的 voice 槽偏好已删），
    显式 model 选择与主槽路径由 _make_brain_cfg 承担。"""
    assert not hasattr(hp, "_mode_brain_cfg")
    assert hasattr(hp, "_make_brain_cfg")


# ---------------------------------- 收尾意图门按取向收口（2026-11-04 手机 chat 尾巴）

def test_chat_mode_skips_finish_intent_gate(monkeypatch):
    """对话取向**不装**收尾意图门。

    门防的是「oneshot 任务把没派发的畸形调用当结论、静默假成功」；chat 的纯文本收尾就是
    交付物本身，没有可被误判的「任务成功」。而门自身是一次不计入 brain_calls 的模型往返
    （timeout 90s），压在用户已经看完答案之后——本机同一句闲聊实测：口播完成 → run 结束
    门内 34.29s、关门 0.04s。task / auto 取向语义不变，仍然装门。
    """
    from omni_core.local.loop import core as tl
    from tests._env import install_fake_env

    class _FakeExec:
        kind = "host"

        def text_of(self, percept):
            return ""

        def verify_done(self, condition, percept):
            return (False, "noop")

    install_fake_env(monkeypatch, _FakeExec)
    monkeypatch.setattr(tl.config, "load_config", lambda: {})
    cfg = {"model": "m", "base_url": "http://localhost:9", "api_key": "test"}
    assert tl.ToolLoop(brain_cfg=cfg, mode="chat")._plain_finish_judge(None) is None, \
        "对话取向不付收尾意图门"
    assert callable(tl.ToolLoop(brain_cfg=cfg, mode="task")._plain_finish_judge(None)), \
        "task 取向仍装门（oneshot 假成功守门人不动）"
    assert callable(tl.ToolLoop(brain_cfg=cfg, mode="auto")._plain_finish_judge(None))


# --------------------------------------------- mode 持久化（2026-10-31 与通道解绑）

def test_mode_persists_in_store_and_whitelisted():
    """mode 进 task.json meta 白名单：/chat 派发与 /state 更新都能落盘，列表原样透传。"""
    from omni_core.local.task_store import TaskStore

    meta = TaskStore.create(project_id="qq", objective="测试取向持久化")
    tid = meta["task_id"]
    TaskStore.update(tid, state="running", mode="chat")
    assert TaskStore.get(tid)["mode"] == "chat"
    TaskStore.update(tid, mode="task")
    assert TaskStore.get(tid)["mode"] == "task"
    assert any(r["task_id"] == tid and r.get("mode") == "task"
               for r in TaskStore.list())          # tasks 列表透传 → 前端回填数据源


def test_state_update_schema_validates_mode():
    """/state 端点 schema：mode 只收 auto/chat/task，其余 422。"""
    from backend.api.schemas_runtime import UpdateTaskRequest
    from pydantic import ValidationError

    assert UpdateTaskRequest.model_validate({"mode": "chat"}).mode == "chat"
    assert UpdateTaskRequest.model_validate({"mode": "auto"}).mode == "auto"
    with pytest.raises(ValidationError):
        UpdateTaskRequest.model_validate({"mode": "bogus"})
