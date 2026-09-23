"""F4.1b — 纪律文件注入位置修订：尾部重插 → system prompt（run 内锁定）。

验收（对应 F4.1b 计划 1~6）：
1. 两层 AGENTS.md 有内容 → instructions 含纪律块与来源标注；均不存在 → 逐字节不变；
2. run 级快照锁定：run 中途改写文件 → 后续请求不含新内容，且有告警事件落轨迹；
3. 覆盖语义（官方规则）：纪律在 system，用户当轮消息仍排在最后（不被纪律"压住"）；
4. `inject_instructions: false` → 零注入；超上限 → 截断标注；
5. request_fingerprint 的 system hash 含纪律块 → 跨 run 随文件变、run 内不变；
6. memory 仍走尾部（F4.2 未被本次迁移影响）。
"""
import json
import types

import config as config_mod
from omni_core.brain import sdk_loop as sl
from omni_core.brain.sdk_loop import run_subtask_sdk
from omni_core.local import knowledge_inject as ki
from omni_core.local.knowledge_inject import load_agents_snapshot
from omni_core.local.tool_loop import ToolLoop, _request_fingerprint


class _Rec:
    """轨迹存储替身：只记录 log_think。"""

    def __init__(self):
        self.thinks = []

    def log_think(self, content, role="", model=""):
        self.thinks.append({"content": content, "role": role, "model": model})

    def kinds(self):
        out = []
        for t in self.thinks:
            try:
                out.append(json.loads(t["content"]).get("kind"))
            except Exception:
                out.append(None)
        return out

    def record(self, kind):
        for t in self.thinks:
            try:
                d = json.loads(t["content"])
            except Exception:
                continue
            if d.get("kind") == kind:
                return d
        return None


def _loop():
    return ToolLoop({"model": "m", "base_url": "http://x", "api_key": "k"}, verbose=False)


def _spec(task_id="f41b", max_steps=None):
    return types.SimpleNamespace(
        objective="o", done_when="", expected=None, task_id=task_id,
        project_id=None, max_steps=max_steps, history=[], corrections=[],
        task_mode="oneshot",
    )


def _cfg(monkeypatch, **over):
    """固定 runtime.long_task.* 配置（其余键透传缺省）。"""
    table = {
        "runtime.long_task.inject_instructions": True,
        "runtime.long_task.instructions_limit": 8192,
        "runtime.long_task.memory_in_user": True,
    }
    table.update(over)
    monkeypatch.setattr(config_mod, "get_config", lambda p, d=None: table.get(p, d))


def _pin_agents(monkeypatch, global_path, task_path=None):
    """把两层纪律文件路径钉到 tmp 路径（tool_loop 内是函数级导入，运行期替换生效）。"""
    import omni_core.local.runtime_paths as rp

    monkeypatch.setattr(rp, "global_agents_file", lambda: global_path)
    monkeypatch.setattr(
        rp, "task_agents_file", lambda tid: task_path or (global_path.parent / "nope.md"))


# --- 1. 组装 + 零注入 ---------------------------------------------------------
def test_snapshot_merges_two_layers_with_source_labels(tmp_path):
    g = tmp_path / "g.md"
    g.write_text("GLOBAL-RULE", encoding="utf-8")
    t = tmp_path / "t.md"
    t.write_text("TASK-RULE", encoding="utf-8")

    snap = load_agents_snapshot([("global", g), ("task", t)])
    assert snap.injected is True
    assert snap.labels == ["global", "task"]
    assert "任务纪律" in snap.block and "AGENTS.md" in snap.block
    assert "[来源: global]" in snap.block and "[来源: task]" in snap.block
    assert "GLOBAL-RULE" in snap.block and "TASK-RULE" in snap.block
    assert snap.block.index("GLOBAL-RULE") < snap.block.index("TASK-RULE"), "task 层更近，排后"


def test_snapshot_empty_when_files_missing_and_merge_is_byte_identical(tmp_path):
    snap = load_agents_snapshot([("global", tmp_path / "none.md"),
                                 ("task", tmp_path / "none2.md")])
    assert snap.injected is False
    assert snap.block == ""
    assert snap.labels == []

    base = "SYS-PROMPT-AS-IS\nline2"
    assert ToolLoop._merge_instructions(base, snap) == base, "零注入时 instructions 逐字节不变"
    assert ToolLoop._merge_instructions(base, None) == base


def test_run_level_load_upgrades_to_merged_prompt(monkeypatch, tmp_path):
    """run 起始装配：两层文件存在时 instructions 尾部带纪律块。"""
    g = tmp_path / "g.md"
    g.write_text("GLOBAL-RULE", encoding="utf-8")
    t = tmp_path / "t.md"
    t.write_text("TASK-RULE", encoding="utf-8")

    loop = _loop()
    _cfg(monkeypatch)
    _pin_agents(monkeypatch, g, t)

    snap = loop._load_instructions_snapshot(_spec("tid"))
    merged = loop._merge_instructions("SYS", snap)
    assert merged.startswith("SYS")
    assert "GLOBAL-RULE" in merged and "TASK-RULE" in merged
    assert snap.labels == ["global", "task"]


def test_run_level_zero_injection_when_no_files(monkeypatch, tmp_path):
    loop = _loop()
    _cfg(monkeypatch)
    _pin_agents(monkeypatch, tmp_path / "absent.md")

    snap = loop._load_instructions_snapshot(_spec("tid"))
    assert snap.injected is False
    assert loop._merge_instructions("SYS", snap) == "SYS"


# --- 4. 开关 / 上限 -----------------------------------------------------------
def test_inject_instructions_false_is_zero_injection(monkeypatch, tmp_path):
    g = tmp_path / "g.md"
    g.write_text("GLOBAL-RULE", encoding="utf-8")

    loop = _loop()
    _cfg(monkeypatch, **{"runtime.long_task.inject_instructions": False})
    _pin_agents(monkeypatch, g)

    snap = loop._load_instructions_snapshot(_spec("tid"))
    assert snap.injected is False
    assert loop._merge_instructions("SYS", snap) == "SYS"


def test_snapshot_over_limit_truncated_with_annotation(tmp_path):
    g = tmp_path / "g.md"
    g.write_text("x" * 500, encoding="utf-8")
    t = tmp_path / "t.md"
    t.write_text("y" * 500, encoding="utf-8")

    snap = load_agents_snapshot([("global", g), ("task", t)], limit=120)
    assert "截断" in snap.block and "120" in snap.block
    assert snap.block.startswith("# 任务纪律")


def test_run_level_limit_comes_from_config(monkeypatch, tmp_path):
    g = tmp_path / "g.md"
    g.write_text("z" * 800, encoding="utf-8")

    loop = _loop()
    _cfg(monkeypatch, **{"runtime.long_task.instructions_limit": 100})
    _pin_agents(monkeypatch, g)

    snap = loop._load_instructions_snapshot(_spec("tid"))
    assert "截断" in snap.block and "100" in snap.block


# --- 2. run 内快照锁定 --------------------------------------------------------
def test_snapshot_block_frozen_after_file_change(tmp_path):
    """run 中途改文件：块内容仍是起始快照，但 check_drift 能发现改动。"""
    g = tmp_path / "g.md"
    g.write_text("OLD-RULE", encoding="utf-8")
    snap = load_agents_snapshot([("global", g)])
    assert "OLD-RULE" in snap.block
    assert snap.check_drift() == []

    g.write_text("NEW-RULE", encoding="utf-8")
    assert "NEW-RULE" not in snap.block, "run 内不重读，快照内容保持起始状态"
    assert "OLD-RULE" in snap.block
    assert snap.check_drift() == ["global"], "改动应被只读比对发现"


def test_snapshot_drift_detects_late_creation(tmp_path):
    """起始不存在、中途新建 → 同样算改动（缺失层也参与比对）。"""
    g = tmp_path / "g.md"
    snap = load_agents_snapshot([("global", g)])
    assert snap.injected is False and snap.check_drift() == []
    g.write_text("LATE", encoding="utf-8")
    assert snap.check_drift() == ["global"]


# --- 2/3. run_subtask_sdk 集成：逐块漂移检查 + 位置断言 ------------------------
class _FakeRes:
    def __init__(self, items):
        self._items = items

    def to_input_list(self):
        return self._items


class _MockRunner:
    """记录每轮 agent.instructions 与 items；首轮结束时执行 mutate（模拟中途改文件）。"""

    def __init__(self, mutate=None):
        self.mutate = mutate
        self.calls = []
        self.instructions = []

    def run(self, agent, items, max_turns=None, hooks=None, run_config=None):
        self.calls.append(list(items))
        self.instructions.append(getattr(agent, "instructions", None))
        if self.mutate is not None and len(self.calls) == 1:
            self.mutate()

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


def test_run_keeps_snapshot_and_warns_once_on_mid_run_change(monkeypatch, tmp_path):
    """run 中途改文件 → 后续请求不含新内容 + 告警一次（沿用快照）。"""
    g = tmp_path / "g.md"
    g.write_text("OLD-RULE", encoding="utf-8")
    snap = load_agents_snapshot([("global", g)])
    merged = ToolLoop._merge_instructions("SYS", snap)

    runner = _MockRunner(mutate=lambda: g.write_text("NEW-RULE", encoding="utf-8"))
    monkeypatch.setattr(sl, "Runner", runner)
    warns = []

    res = run_subtask_sdk(
        {"model": "m", "base_url": "http://x", "api_key": "k"},
        instructions=merged,
        user_input="执行任务",
        tools=[],
        gate=_gate(),
        max_steps=3,
        chunk_turns=1,
        should_stop=lambda: False,
        instructions_snapshot=snap,
        on_instructions_drift=lambda labels: warns.append(list(labels)),
    )

    assert len(runner.calls) >= 2, "应跨多块运行才能观察到中途改动"
    assert warns == [["global"]], "改动只告警一次"
    # 后续请求不含新内容，且 system 逐字节稳定（前缀缓存友好）
    assert len(set(runner.instructions)) == 1
    assert "NEW-RULE" not in (runner.instructions[-1] or "")
    assert "OLD-RULE" in (runner.instructions[-1] or "")
    assert res["steps"] >= 1


def test_no_drift_no_warning(monkeypatch, tmp_path):
    g = tmp_path / "g.md"
    g.write_text("STABLE", encoding="utf-8")
    snap = load_agents_snapshot([("global", g)])

    runner = _MockRunner()
    monkeypatch.setattr(sl, "Runner", runner)
    warns = []
    run_subtask_sdk(
        {"model": "m", "base_url": "http://x", "api_key": "k"},
        instructions=ToolLoop._merge_instructions("SYS", snap),
        user_input="执行任务", tools=[], gate=_gate(),
        max_steps=2, chunk_turns=1, should_stop=lambda: False,
        instructions_snapshot=snap,
        on_instructions_drift=lambda labels: warns.append(list(labels)),
    )
    assert warns == []


def test_discipline_block_in_system_while_user_turn_stays_last(monkeypatch, tmp_path):
    """覆盖语义（官方规则测试化）：纪律进 system，用户当轮消息仍排最后。"""
    g = tmp_path / "g.md"
    g.write_text("禁止使用 X", encoding="utf-8")
    snap = load_agents_snapshot([("global", g)])
    merged = ToolLoop._merge_instructions("SYS", snap)

    runner = _MockRunner()
    monkeypatch.setattr(sl, "Runner", runner)
    run_subtask_sdk(
        {"model": "m", "base_url": "http://x", "api_key": "k"},
        instructions=merged,
        user_input="本轮明确允许使用 X",
        tools=[], gate=_gate(), max_steps=1, chunk_turns=1, should_stop=lambda: False,
        instructions_snapshot=snap,
    )

    # 位置断言：纪律在 system（instructions），不出现在任何会话消息里
    assert "禁止使用 X" in runner.instructions[0]
    assert all("禁止使用 X" not in str(m.get("content", ""))
               for m in runner.calls[0] if isinstance(m, dict))
    # 用户当轮消息排在最后 → 未被纪律块"压住"
    assert runner.calls[0][-1] == {"role": "user", "content": "本轮明确允许使用 X"}


# --- 5. RH-1：fingerprint 含拼接后 system -------------------------------------
def test_fp_system_prompt_carries_discipline_block(monkeypatch, tmp_path):
    """O5+ 不变式：指纹输入 = 拼接纪律块之后的 system（Model-visible ⟺ logged）。"""
    g = tmp_path / "g.md"
    g.write_text("FP-RULE", encoding="utf-8")

    loop = _loop()
    _cfg(monkeypatch)
    _pin_agents(monkeypatch, g)

    captured = {}

    def _fake_sdk(*a, **k):
        captured.update(k)
        captured["instructions"] = k.get("instructions")
        return {"success": True, "reason": "ok", "steps": 1, "escalated": False,
                "escalate_reason": "", "provider_error": False, "dispatch_plan": []}

    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_sdk)
    loop._run_via_sdk(_spec("tid"), {"model": "m"}, "SYS", types.SimpleNamespace(),
                      traj=None, user_input="hi")

    assert "FP-RULE" in (captured.get("instructions") or ""), "纪律块应进 instructions"
    assert "FP-RULE" in loop._fp_system_prompt, "指纹输入应为拼接后的 system"
    assert getattr(captured.get("instructions_snapshot"), "injected", False) is True
    assert callable(captured.get("on_instructions_drift"))


def test_fingerprint_changes_across_runs_and_is_stable_within_run(tmp_path):
    g = tmp_path / "g.md"
    g.write_text("RULE-A", encoding="utf-8")

    snap_a = load_agents_snapshot([("global", g)])
    sys_a = ToolLoop._merge_instructions("SYS", snap_a)
    fp_a = _request_fingerprint(sys_a, ["t"], 0)
    assert fp_a != _request_fingerprint("SYS", ["t"], 0), "含纪律块 → 指纹必须变化"

    # 跨 run：文件变了 → 拼接后 system 变 → 指纹变
    g.write_text("RULE-B", encoding="utf-8")
    snap_b = load_agents_snapshot([("global", g)])
    sys_b = ToolLoop._merge_instructions("SYS", snap_b)
    assert _request_fingerprint(sys_b, ["t"], 0) != fp_a

    # run 内：旧快照的 system 全程不变（即便文件已被改）→ 指纹前缀稳定
    assert snap_a.check_drift() == ["global"]
    assert ToolLoop._merge_instructions("SYS", snap_a) == sys_a
    assert _request_fingerprint(ToolLoop._merge_instructions("SYS", snap_a), ["t"], 0) == fp_a

    # 还原内容 → 指纹与首轮一致（内容决定，而非时间）
    g.write_text("RULE-A", encoding="utf-8")
    assert _request_fingerprint(
        ToolLoop._merge_instructions("SYS", load_agents_snapshot([("global", g)])), ["t"], 0) == fp_a


def test_instructions_injected_and_drift_logged_to_trajectory(monkeypatch, tmp_path):
    """埋点：instructions_injected（placement=system, locked）与 instructions_drift 落轨迹。"""
    g = tmp_path / "g.md"
    g.write_text("TRACE-RULE", encoding="utf-8")

    loop = _loop()
    _cfg(monkeypatch)
    _pin_agents(monkeypatch, g)

    captured = {}

    def _fake_sdk(*a, **k):
        captured.update(k)
        return {"success": True, "reason": "ok", "steps": 1, "escalated": False,
                "escalate_reason": "", "provider_error": False, "dispatch_plan": []}

    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_sdk)
    traj = _Rec()
    loop._run_via_sdk(_spec("tid"), {"model": "m"}, "SYS", types.SimpleNamespace(),
                      traj=traj, user_input="hi")

    rec = traj.record("instructions_injected")
    assert rec is not None, traj.kinds()
    assert rec["placement"] == "system" and rec["locked"] is True
    assert rec["layers"] == ["global"] and rec["chars"] == len(captured["instructions"]) - len("SYS") - 2

    # 漂移告警：模拟 sdk_loop 逐块检测到改动后回调
    captured["on_instructions_drift"](["global"])
    drift = traj.record("instructions_drift")
    assert drift is not None and drift["layers"] == ["global"]


# --- 6. memory 仍走尾部（F4.2 未被迁移影响） -----------------------------------
def test_memory_still_in_tail_not_system(monkeypatch, tmp_path):
    g = tmp_path / "g.md"
    g.write_text("DISCIPLINE", encoding="utf-8")

    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    _cfg(monkeypatch)
    _pin_agents(monkeypatch, g)
    monkeypatch.setattr(ki, "load_memory_text", lambda: "MEM-TEXT")

    captured = {}

    def _fake_sdk(*a, **k):
        captured.update(k)
        return {"success": True, "reason": "ok", "steps": 1, "escalated": False,
                "escalate_reason": "", "provider_error": False, "dispatch_plan": []}

    monkeypatch.setattr(sl, "run_subtask_sdk", _fake_sdk)
    loop._run_via_sdk(_spec("tid"), {"model": "m"}, "SYS", types.SimpleNamespace(),
                      traj=None, user_input="hi")

    assert "DISCIPLINE" in captured["instructions"]          # 纪律 → system
    assert "MEM-TEXT" not in captured["instructions"]        # 记忆不进 system
    assert "MEM-TEXT" in captured["tail_inject_block"]       # 记忆 → 尾部重插
    assert captured["tail_inject_layers"] == ["memory"]


def test_memory_in_system_fallback_when_memory_in_user_false(monkeypatch, tmp_path):
    """一键回退：memory_in_user=false → 尾部块不含记忆（由 system 路径承载）。"""
    loop = _loop()
    loop.knowledge_cfg["memory"] = True
    _cfg(monkeypatch, **{"runtime.long_task.memory_in_user": False})

    block, layers = loop._build_memory_injection(False)
    assert block == "" and layers == []
    assert loop._memory_in_system() is True
