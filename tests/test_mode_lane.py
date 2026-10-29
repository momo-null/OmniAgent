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

def test_same_lane_is_serial_and_other_lane_parallel():
    """对话 lane 串行；工作 lane 不受对话影响——这就是「干活期间聊天不被 409」。"""
    m = RuntimeManager()
    assert m.try_start("t_chat", lane="chat") is True
    assert m.try_start("t_chat2", lane="chat") is False, "对话 lane 内必须串行"
    assert m.try_start("t_work", lane="work") is True, "工作 lane 不受对话 lane 阻塞"
    assert m.is_any_running("chat") is True
    assert m.is_any_running("work") is True
    assert m.is_any_running(None) is True

    m.finish("t_chat")
    assert m.is_any_running("chat") is False
    assert m.is_any_running("work") is True


def test_same_task_never_runs_twice_across_lanes():
    """同一个 task 无论如何不允许并发 run（会话/轨迹按 task 落盘，必须串行）。"""
    m = RuntimeManager()
    assert m.try_start("t_same", lane="work") is True
    assert m.try_start("t_same", lane="chat") is False


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
