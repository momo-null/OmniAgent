"""记忆注入形态 — placement 不变量的回归锁（2026-10-07 立，2026-11-02 P1 改版）。

P1（方案 C，2026-11-02）：**L1 检索结果永不自动注入**——lead-in 通道整段删除，
模型经 memory_recall 工具自取；system 常驻 <memory-usage-rules>（必查场景 + 每轮 ≤3 次）。
本文件锁死四条不变量：

1. 稳定段（<memory-usage-rules> + <scene-navigation> + <self-persona>）进 system，
   且在指纹计算之前合并；
2. run 起始**没有** <relevant-memories>，inject_text_brain 不被自动调用；
   检索结果只经工具返回进上下文一次；
3. L2 场景全文**只**经 read_scene 工具按需进上下文（渐进披露），任何注入路径不含全文；
4. 子任务（is_sub）零记忆注入语义不变。

L2 执行器机制级测试在 test_scene_executor.py；本文件只测注入 placement。
"""
import json
import types

import config as config_mod
from omni_core import memory_brain as mb
from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import run_subtask_sdk
from omni_core.local import knowledge_inject as ki
from omni_core.local import scene_executor
from omni_core.local.loop import ToolLoop


class _Rec:
    """轨迹存储替身：只记录 log_think。"""

    def __init__(self):
        self.thinks = []

    def log_think(self, content, role="", model=""):
        self.thinks.append({"content": content, "role": role, "model": model})

    def records(self, kind):
        out = []
        for t in self.thinks:
            try:
                d = json.loads(t["content"])
            except Exception:
                continue
            if d.get("kind") == kind:
                out.append(d)
        return out


def _loop():
    return ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}, verbose=False)


def _spec(task_id="align"):
    return types.SimpleNamespace(
        objective="o", done_when="", expected=None, task_id=task_id,
        project_id=None, max_steps=None, history=[], corrections=[],
        task_mode="oneshot",
    )


def _cfg(monkeypatch, **over):
    table = {
        "runtime.long_task.inject_instructions": True,
        "runtime.long_task.instructions_limit": 8192,
    }
    table.update(over)
    monkeypatch.setattr(config_mod, "get_config", lambda p, d=None: table.get(p, d))


def _fake_sdk(captured):
    def _sdk(*a, **k):
        captured.update(k)
        return {"success": True, "reason": "ok", "steps": 1, "escalated": False,
                "escalate_reason": "", "provider_error": False, "dispatch_plan": []}
    return _sdk


# --- 1. 稳定段进 system -------------------------------------------------------
def test_stable_block_scene_nav_and_profile_in_system(monkeypatch):
    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True
    monkeypatch.setattr(scene_executor, "load_scene_nav",
                        lambda pid, **k: [{"filename": "windows-环境事实.md",
                                           "summary": "SCENE-SUM", "heat": 3,
                                           "updated": "2026-11-01T00:00:00+00:00"},
                                          {"filename": "diary-2026-10-31.md",
                                           "summary": "DIARY-SUM", "heat": 0,
                                           "updated": "2026-11-01T00:00:00+00:00"}])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "PROFILE-TEXT")

    block, layers = loop._memory_stable_block(False, task_id="tid")
    assert "<scene-navigation>" in block and "</scene-navigation>" in block
    assert "windows-环境事实.md" in block
    # P6（2026-11-02）：对齐原仓库 Path + 热度 + Summary 三行索引；M-1 零摘要已废
    assert "SCENE-SUM" in block and "热度 3" in block
    # NP-2（INV-11 / N-3）：稳定段取消日记/月记
    # 固定配额——固定日记条目不进导航（叙事经 narrative_search 按需检索）
    assert "DIARY-SUM" not in block and "diary-2026-10-31" not in block
    assert "read_scene" in block, "导航必须指引按需读取（渐进披露）"
    assert "<self-persona>" in block and "PROFILE-TEXT" in block
    assert layers == ["memory_rules", "scene_nav", "self_profile"]
    assert "<memory-usage-rules>" in block, "P1：使用规则常驻 system（检索结果永不自动注入）"
    # NP-2：叙事能力声明（零内容）+ 不得转述规则 + 七工具配额口径
    assert "narrative_search" in block and "narrative_read" in block
    assert "不得把日记/月记/对人的印象直接转述到对外消息" in block
    assert "不要反复换关键词硬查" in block
    assert "SCENE-CONTENT" not in block, "全文不得进稳定段（只准摘要 + read_scene）"


def test_stable_block_empty_when_memory_and_profile_gates_off():
    loop = _loop()
    loop.knowledge_cfg["memory"] = False
    loop.knowledge_cfg["profile"] = False
    assert loop._memory_stable_block(False, task_id="tid") == ("", [])


def test_stable_block_rules_survive_empty_scene_and_profile(monkeypatch):
    """scene/画像都空时，使用规则仍常驻——L1 自取语义靠它立住。"""
    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True
    monkeypatch.setattr(scene_executor, "load_scene_nav", lambda pid, **k: [])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "  ")
    block, layers = loop._memory_stable_block(False, task_id="tid")
    assert "<memory-usage-rules>" in block and layers == ["memory_rules"]


def test_bridge_merges_stable_block_into_instructions_before_fingerprint(monkeypatch):
    """稳定段并入 system 且早于指纹计算（Model-visible ⟺ logged）。"""
    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True
    _cfg(monkeypatch)
    monkeypatch.setattr(scene_executor, "load_scene_nav",
                        lambda pid, **k: [{"filename": "a.md", "summary": "SCENE-SUM", "heat": 1}])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "PROFILE-TEXT")

    captured = {}
    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_sdk(captured))
    loop._run_via_sdk(_spec(), {"model": "m"}, "SYS", types.SimpleNamespace(),
                      traj=None, user_input="hi")

    instr = captured["instructions"] or ""
    assert "<scene-navigation>" in instr and "<self-persona>" in instr
    assert loop._fp_system_prompt == instr, "指纹输入必须是拼接稳定段后的 system"


# --- 2. P1：检索结果永不自动注入；system 常驻使用规则 --------------------------
def test_search_results_never_auto_injected_rules_in_system(monkeypatch):
    calls = {"inject": 0}

    def _spy_inject(p, q):
        calls["inject"] += 1
        return "<relevant-memories>MEMO</relevant-memories>", {"hits": 1}

    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True
    _cfg(monkeypatch)
    monkeypatch.setattr(scene_executor, "load_scene_nav",
                        lambda pid, **k: [{"filename": "a.md", "summary": "S", "heat": 1}])
    monkeypatch.setattr(mb, "inject_text_brain", _spy_inject)
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "PROFILE-TEXT")

    captured = {}
    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_sdk(captured))
    traj = _Rec()
    loop._run_via_sdk(_spec(), {"model": "m"}, "SYS", types.SimpleNamespace(),
                      traj=traj, user_input="hi")

    instr = captured["instructions"] or ""
    assert "<relevant-memories>" not in instr and calls["inject"] == 0, \
        "L1 检索结果不得进 system，也不得被 run 起始自动检索"
    assert "lead_in_message" not in captured, "lead-in 通道已删除"
    assert "<memory-usage-rules>" in instr and "memory_recall" in instr
    assert "不要反复换关键词硬查" in instr
    placements = {r["placement"]: r for r in traj.records("memory_injected")}
    assert set(placements) == {"system"}
    assert placements["system"]["layers"] == ["memory_rules", "scene_nav", "self_profile"]
    assert "<current-context>" not in instr, "空信号零注入"


def test_context_signal_moved_out_of_system(monkeypatch):
    """2026-11-11 钉前缀：逐 run 变化的 <current-context> 不再进 system。

    静态知识块（rules / scene_nav / profile）留在 system 跨 run 字节一致；
    处境信号改由当轮 user 输入携带，单独落 context_signal_injected 轨迹。
    """
    from omni_core.local.loop import context_signal as cs

    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True
    _cfg(monkeypatch)
    monkeypatch.setattr(scene_executor, "load_scene_nav", lambda pid, **k: [])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "")
    monkeypatch.setattr(cs, "build_run", lambda **k: dict(cs._CURRENT))
    monkeypatch.setattr(cs, "render",
                        lambda *a, **k: "<current-context>\ncontext=group:7\n"
                                        "</current-context>")
    captured = {}
    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_sdk(captured))
    traj = _Rec()
    loop._run_via_sdk(_spec(), {"model": "m"}, "SYS", types.SimpleNamespace(),
                      traj=traj, user_input="hi")
    instr = captured["instructions"] or ""
    assert "<current-context>" not in instr, "动态信号不得进 system（否则前缀逐 run 失效）"
    assert "context=group:7" in str(captured.get("user_input") or ""), \
        "处境信号随当轮输入进上下文"
    placements = {r["placement"]: r for r in traj.records("memory_injected")}
    assert set(placements) == {"system"}
    assert "context_signal" not in placements["system"]["layers"]
    assert traj.records("context_signal_injected"), "新 kind 单独落轨迹"


def test_memory_gate_off_zero_injection(monkeypatch):
    loop = _loop()
    loop.knowledge_cfg["memory"] = False
    loop.knowledge_cfg["profile"] = False
    _cfg(monkeypatch)
    monkeypatch.setattr(scene_executor, "load_scene_nav",
                        lambda pid, **k: [{"filename": "a.md", "summary": "S", "heat": 1}])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "PROFILE-TEXT")

    captured = {}
    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_sdk(captured))
    loop._run_via_sdk(_spec(), {"model": "m"}, "SYS", types.SimpleNamespace(),
                      traj=None, user_input="hi")

    assert "<scene-navigation>" not in (captured["instructions"] or "")
    assert "PROFILE-TEXT" not in (captured["instructions"] or "")
    assert "<memory-usage-rules>" not in (captured["instructions"] or "")


# --- 2b. sdk_loop 起始 items 骨架（后续意图门测试复用 _MockRunner/_gate） -------
class _FakeRes:
    def __init__(self, items):
        self._items = items

    def to_input_list(self):
        return self._items


class _MockRunner:
    def __init__(self):
        self.calls = []

    def run(self, agent, items, max_turns=None, hooks=None, run_config=None):
        self.calls.append(list(items))

        async def _coro():
            return _FakeRes(list(items))

        return _coro()


def _gate():
    return types.SimpleNamespace(
        verify_done=lambda: (False, "未完成"),
        peek=lambda: (False, "未完成"),
        verify_count=0,
        has_condition=True,
    )


# --- 3. read_scene：全文只走按需读取 ------------------------------------------
def test_read_scene_tool_registered_and_reads_named_block(monkeypatch):
    from omni_core.tools.base import TOOL_REGISTRY, call_tool
    from omni_core.tools.loader import RESERVED_TOOL_NAMES
    from omni_core.tools.scene_tool import set_scene_task_context

    assert TOOL_REGISTRY.get("read_scene") is not None, "scene_tool import 即注册"
    assert "read_scene" in RESERVED_TOOL_NAMES, "保留名：插件不得顶替"

    monkeypatch.setattr(scene_executor, "read_scene_block", lambda pid, name: "SCENE-CONTENT")
    set_scene_task_context("tid")
    r = call_tool("read_scene", {"scene_name": "a.md"})
    assert r["ok"] is True and r["scene"] == "SCENE-CONTENT"


def test_read_scene_empty_name_lists_blocks(monkeypatch):
    from omni_core.tools.base import call_tool
    from omni_core.tools.scene_tool import set_scene_task_context

    monkeypatch.setattr(scene_executor, "list_scene_blocks",
                        lambda pid: [{"filename": "a.md", "summary": "S", "heat": 1}])
    set_scene_task_context("tid")
    r = call_tool("read_scene", {})
    assert r["ok"] is True and r["scenes"][0]["filename"] == "a.md"


def test_read_scene_missing_returns_not_ok(monkeypatch):
    from omni_core.tools.base import call_tool
    from omni_core.tools.scene_tool import set_scene_task_context

    monkeypatch.setattr(scene_executor, "read_scene_block", lambda pid, name: "")
    set_scene_task_context("tid")
    r = call_tool("read_scene", {"scene_name": "缺失"})
    assert r["ok"] is False and "未找到场景块" in r["error"]


# --- 4. 子任务语义不变 --------------------------------------------------------
def test_subtask_gets_no_memory_injection(monkeypatch):
    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True
    monkeypatch.setattr(scene_executor, "load_scene_nav",
                        lambda pid, **k: [{"filename": "a.md", "summary": "S", "heat": 1}])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "PROFILE-TEXT")

    assert loop._memory_stable_block(True, task_id="t") == ("", [])


# --- 5. 收尾意图门（判断全 LLM,零格式字面量）-----------------------------------
def test_intent_judge_built_only_with_brain_cfg(monkeypatch):
    loop = _loop()
    traj = _Rec()
    loop.brain_cfg = None
    assert loop._plain_finish_judge(traj) is None, "无 brain_cfg 不注入 judge"

    loop.brain_cfg = {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}
    judge = loop._plain_finish_judge(traj)
    assert callable(judge)
    # 空文本直接放行（不发 LLM）
    assert judge("   ") is None

    from omni_core.local import llm_judge
    monkeypatch.setattr(llm_judge, "chat_json",
                        lambda cfg, s, u, timeout=25.0: {"fragments": ["<伪标签包裹的调用>"]})
    verdict = judge("D盘总使用约299GB...<某未派发的调用文本>")
    assert verdict is not None and "tool_calls" in verdict
    rec = traj.records("finish_intent_check")[0]
    assert rec["fragments"] == 1 and rec["verdict"] == "attempted_tool_call"

    monkeypatch.setattr(llm_judge, "chat_json",
                        lambda cfg, s, u, timeout=25.0: {"fragments": []})
    assert judge("D盘总使用约299GB") is None

    monkeypatch.setattr(llm_judge, "chat_json",
                        lambda cfg, s, u, timeout=25.0: (_ for _ in ()).throw(RuntimeError("x")))
    assert judge("任意") is None, "judge 异常 fail-open"


def test_intent_judge_config_off(monkeypatch):
    loop = _loop()
    loop.brain_cfg = {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}
    monkeypatch.setattr(config_mod, "get_config",
                        lambda k, d=None: False if k == "runtime.long_task.finish_intent_check" else d)
    assert loop._plain_finish_judge(_Rec()) is None


# --- 5b. sdk_loop 级：判 B → 不判完成继续跑,限 2 次 -----------------------------
def _gate_accept():
    """信任大脑路径:无校验条件 + verify_done 放行。"""
    return types.SimpleNamespace(
        verify_done=lambda: (True, "无校验条件，信任大脑"),
        peek=lambda: (True, "无校验条件"),
        verify_count=0,
        has_condition=False,
    )


def test_intent_gate_nudges_then_accepts(monkeypatch):
    runner = _MockRunner()
    monkeypatch.setattr(sl, "Runner", runner)
    calls = {"n": 0}

    def judge(text):
        calls["n"] += 1
        if calls["n"] <= 2:
            return "你上一条输出中似乎包含未真正派发的工具调用，请用结构化 tool_calls 重发。"
        return None

    res = run_subtask_sdk(
        {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
        instructions="SYS", user_input="执行任务", tools=[], gate=_gate_accept(),
        max_steps=6, chunk_turns=1, should_stop=lambda: False,
        plain_finish_judge=judge,
    )
    assert len(runner.calls) >= 3, "判 B 应继续跑而非立即判成功"
    assert calls["n"] >= 2
    # 纠正消息出现在后续块的 items 里
    second = [m for m in runner.calls[1] if isinstance(m, dict)]
    assert any("未真正派发的工具调用" in str(m.get("content")) for m in second)
    assert res["success"] is True, "超限/放行后按原语义判成功"


def test_intent_gate_fail_open_and_condition_path(monkeypatch):
    runner = _MockRunner()
    monkeypatch.setattr(sl, "Runner", runner)

    def boom(text):
        raise RuntimeError("judge down")

    res = run_subtask_sdk(
        {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
        instructions="SYS", user_input="执行任务", tools=[], gate=_gate_accept(),
        max_steps=1, chunk_turns=1, should_stop=lambda: False,
        plain_finish_judge=boom,
    )
    assert res["success"] is True and len(runner.calls) == 1, "judge 异常 → fail-open 判成功"

    # 有校验条件且字面校验通过 → 不走意图门(判成功不调 judge)
    ran = {"n": 0}

    def spy(text):
        ran["n"] += 1
        return None

    runner2 = _MockRunner()
    monkeypatch.setattr(sl, "Runner", runner2)
    run_subtask_sdk(
        {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"},
        instructions="SYS", user_input="执行任务", tools=[], gate=_gate(),
        max_steps=1, chunk_turns=1, should_stop=lambda: False,
        plain_finish_judge=spy,
    )
    assert ran["n"] == 0, "有条件路径不应触发意图门"


# ---- P6-2（H-6 §16.3）：表达节奏开关缺省关，关闭时稳定段逐字节不变 ----

def test_we_memory_rhythm_switch_default_off(monkeypatch):
    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    loop.knowledge_cfg["profile"] = True
    monkeypatch.setattr(scene_executor, "load_scene_nav", lambda pid, **k: [])
    monkeypatch.setattr(ki, "load_self_text", lambda *a, **k: "")
    _cfg(monkeypatch)
    base, _ = loop._memory_stable_block(False, task_id="tid")
    assert "把话递回去" not in base                      # 缺省关：行为与现状一致

    # 开关开启（enabled + rhythm）→ 多一句「自我披露 / 把话递回去」分寸
    _cfg(monkeypatch, **{"knowledge.narrative.we_memory.enabled": True,
                         "knowledge.narrative.we_memory.rhythm": True})
    on, _ = loop._memory_stable_block(False, task_id="tid")
    added = ("- 聊到你们共同经历过的事时，可以用自己的口气自然提起，也可以把"
             "话递回去问问对方当时的感觉；不硬塞回忆、不为提起而提起。\n")
    assert on == base.replace("不要主动提及。\n</memory-usage-rules>",
                              "不要主动提及。\n" + added + "</memory-usage-rules>")

    # enabled 但 rhythm 关 → 仍不变
    _cfg(monkeypatch, **{"knowledge.narrative.we_memory.enabled": True,
                         "knowledge.narrative.we_memory.rhythm": False})
    off2, _ = loop._memory_stable_block(False, task_id="tid")
    assert off2 == base
