"""S2 审批门用例（sandbox-permission-design.md §9.3–§9.6）。

覆盖：前置门（exec/actuate/network）、AutoDeny 兜底、批准后执行、
任务级类记忆（remember）、拒绝 / 超时语义、read_only 档、full_access 语义、
审计落盘。统一走 call_tool 直调（与 SDK Runner 同一 on_invoke_tool 路径）。
"""
import json
import os
from pathlib import Path

import pytest

from omni_core.local import runtime_paths as RP
from omni_core.tools import policy
from omni_core.tools.base import call_tool
from omni_core.tools.workspace import set_task

REPO_ROOT = Path(__file__).resolve().parents[1]
_SHELL = "cmd" if os.name == "nt" else "bash"


@pytest.fixture(scope="module", autouse=True)
def _load_fs_plugin():
    """filesystem 为插件（非 core）：直调用例需先装载注册（生产由 ToolLoop 装载）。"""
    from omni_core.tools.loader import load_plugins
    load_plugins({})


class ScriptedSink:
    """脚本化 sink：按队列吐 Decision，记录全部卡片请求。"""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.requests = []
        self.memory = set()

    def request(self, card):
        self.requests.append(card)
        if not self.decisions:
            return policy.Decision(approved=False, rule="user_deny")
        d = self.decisions.pop(0)
        if d.remember:
            self.memory.add(("x",))
        return d

    def remembered(self, task_id, key):
        return key in self.memory

    def remember(self, task_id, key):
        self.memory.add(key)


# --- 前置门兜底（AutoDeny）---------------------------------------------------

def test_exec_autodenied_without_context_or_sink():
    """未绑定 run / 未注册 sink（脚本直调）→ auto_deny（fail-closed，不挂测试）。"""
    res = call_tool("shell_exec", {"command": "echo hi", "shell": _SHELL})
    assert res.get("ok") is False and res.get("denied") is True
    assert res.get("policy") == "S2" and res.get("rule") == "auto_deny"


# --- 批准 / 拒绝 / 记忆 ------------------------------------------------------

def test_exec_approved_then_executes():
    policy.bind(task_id="t_ap1")
    sink = ScriptedSink([policy.Decision(approved=True)])
    policy.set_sink(sink)
    try:
        res = call_tool("shell_exec", {"command": "echo approved_run", "shell": _SHELL})
        assert res.get("ok") is True, res
        assert len(sink.requests) == 1
        card = sink.requests[0]
        assert card.risk == "exec" and card.task_id == "t_ap1"
        assert card.arguments.get("command") == "echo approved_run"
    finally:
        policy.set_sink(None)
        policy.unbind()


def test_exec_denied_returns_structured_refusal():
    policy.bind(task_id="t_ap2")
    policy.set_sink(ScriptedSink([policy.Decision(approved=False, rule="user_deny")]))
    try:
        res = call_tool("shell_exec", {"command": "echo no", "shell": _SHELL})
        assert res.get("denied") is True and res.get("rule") == "user_deny"
        assert res.get("hint"), "拒绝结果必须带引导（换方向而非重试）"
    finally:
        policy.set_sink(None)
        policy.unbind()


def test_timeout_rule_surfaces():
    policy.bind(task_id="t_ap3")
    policy.set_sink(ScriptedSink([policy.Decision(approved=False, rule="timeout")]))
    try:
        res = call_tool("shell_exec", {"command": "echo t", "shell": _SHELL})
        assert res.get("denied") is True and res.get("rule") == "timeout"
    finally:
        policy.set_sink(None)
        policy.unbind()


def test_task_level_remember_skips_second_card():
    """remember 后同任务同类操作不再弹卡（类级键：一次同意覆盖该类全部工具）。"""
    policy.bind(task_id="t_ap4")
    sink = ScriptedSink([policy.Decision(approved=True, remember=True),
                         policy.Decision(approved=True)])
    policy.set_sink(sink)
    try:
        r1 = call_tool("shell_exec", {"command": "echo once", "shell": _SHELL})
        assert r1.get("ok") is True, r1
        r2 = call_tool("shell_exec", {"command": "echo twice", "shell": _SHELL})
        assert r2.get("ok") is True, r2
        assert len(sink.requests) == 1, "第二次调用不应再产生卡片"
    finally:
        policy.set_sink(None)
        policy.unbind()


# --- S0 档位 -----------------------------------------------------------------

def test_read_only_auto_denies_exec_and_network():
    """read_only：exec 自动拒绝（不弹卡）；network 同拒（§6.5 半档裁决的拒侧）。"""
    policy.bind(task_id="t_ro", security_cfg={"mode": "read_only"})
    try:
        res = call_tool("shell_exec", {"command": "echo ro", "shell": _SHELL})
        assert res.get("denied") is True and res.get("rule") == "mode_read_only"
        # network 类：standard 放行 / read_only 拒（直接测前置门判定）
        refusal = policy._pre_gate("network", "web_fetch", "web", {"url": "https://x"}, policy.current())
        assert refusal is not None and refusal.rule == "mode_read_only"
    finally:
        policy.unbind()


def test_network_passes_in_standard_mode():
    """network 类 standard 放行（web 插件开关=常驻同意；§6.5）。"""
    policy.bind(task_id="t_net")
    try:
        assert policy._pre_gate("network", "web_fetch", "web", {}, policy.current()) is None
    finally:
        policy.unbind()


def test_full_access_skips_gate_but_not_absolute_deny():
    """full_access：exec 不弹卡直接执行；~/.omniagent 写仍拒（自提权路径不可用）。"""
    sink = ScriptedSink([])
    policy.set_sink(sink)
    policy.bind(task_id="t_fa", full_access=True)
    try:
        res = call_tool("shell_exec", {"command": f"echo {123}", "shell": _SHELL})
        assert res.get("ok") is True, res
        assert len(sink.requests) == 0, "full_access 不应产生卡片"
        set_task("t_fa")
        RP.ensure_task_dirs("t_fa")
        res2 = call_tool("write_file", {"path": str(RP.global_omni() / "config.yaml"),
                                        "content": "evil"})
        assert res2.get("denied") is True and res2.get("rule") == "self_carrier"
    finally:
        set_task(None)
        policy.unbind()
        policy.set_sink(None)


# --- 审计 --------------------------------------------------------------------

def test_audit_records_gate_interventions():
    """门的干预落审计（approved / user_deny / denied_s1 / mode_read_only）。"""
    policy.bind(task_id="t_aud", security_cfg={})
    policy.set_sink(ScriptedSink([policy.Decision(approved=True),
                                  policy.Decision(approved=False, rule="user_deny")]))
    try:
        call_tool("shell_exec", {"command": "echo a1", "shell": _SHELL})
        call_tool("shell_exec", {"command": "echo a2", "shell": _SHELL})
        call_tool("read_file", {"path": str(RP.global_omni() / "config.yaml")})
        d = policy.audit_dir()
        files = sorted(d.glob("*.jsonl"))
        assert files, "审计文件应已生成"
        entries = [json.loads(x) for x in
                   files[-1].read_text(encoding="utf-8").splitlines() if x.strip()]
        decisions = {e["decision"] for e in entries}
        assert {"approved", "user_deny", "denied_s1"} <= decisions
        for e in entries:
            assert e["task_id"] == "t_aud"
            assert {"ts", "tool", "risk", "arguments", "rule", "cwd"} <= set(e)
    finally:
        policy.set_sink(None)
        policy.unbind()


def test_write_approval_via_sink_approved(tmp_path):
    """根外写入经 sink 批准后放行（S1→S2 联动；ensure_writable 阻塞式）。"""
    policy.bind(task_id="t_wr")
    policy.set_sink(ScriptedSink([policy.Decision(approved=True)]))
    set_task("t_wr")
    RP.ensure_task_dirs("t_wr")
    try:
        target = tmp_path / "outside_root.txt"
        res = call_tool("write_file", {"path": str(target), "content": "granted"})
        assert res.get("ok") is True, res
        assert target.read_text(encoding="utf-8") == "granted"
    finally:
        set_task(None)
        policy.set_sink(None)
        policy.unbind()


def test_read_only_blocks_outside_root_write():
    """read_only：根外写入自动拒绝（不进审批）。"""
    policy.bind(task_id="t_ro2", security_cfg={"mode": "read_only"})
    set_task("t_ro2")
    RP.ensure_task_dirs("t_ro2")
    try:
        res = call_tool("write_file", {"path": str(REPO_ROOT / "x_ro.txt"), "content": "x"})
        assert res.get("denied") is True and res.get("rule") == "mode_read_only"
    finally:
        set_task(None)
        policy.unbind()
