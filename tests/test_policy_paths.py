"""S1 PathPolicy 路径围栏用例（sandbox-permission-design.md §9.1 / §9.2）。

覆盖：绝对拒绝区（读写全拒 + `..` 规范化穿透）、任务 tmp 例外、
允许根、载体文件全盘写拒（config*.yaml / mcp.json / AGENTS.md）、
根外写入的审批/拒绝路径（AutoDeny 兜底）。
"""
import os
from pathlib import Path

import pytest

from omni_core.local import runtime_paths as RP
from omni_core.tools import policy
from omni_core.tools.base import call_tool
from omni_core.tools.workspace import resolve_path, set_task

REPO_ROOT = Path(__file__).resolve().parents[1]
_SHELL = "cmd" if os.name == "nt" else "bash"


def _omni_home() -> Path:
    """受保护根（测试内取隔离后的全局根；生产 = ~/.omniagent）。"""
    return RP.global_omni()


@pytest.fixture(scope="module", autouse=True)
def _load_fs_plugin():
    """filesystem 为插件（非 core）：直调用例需先装载注册（生产由 ToolLoop 装载）。"""
    from omni_core.tools.loader import load_plugins
    load_plugins({})


# --- 绝对拒绝区 --------------------------------------------------------------

def test_guard_denies_omniagent_home_read():
    set_task("t_s1")
    RP.ensure_task_dirs("t_s1")
    try:
        with pytest.raises(policy.PolicyRefusal) as ei:
            resolve_path(str(_omni_home() / "config.yaml"))
        assert ei.value.policy == "S1" and ei.value.rule == "self_carrier"
        # 读 / 列目录 / 检索都经同一漏斗（read 侧同样拒）
        with pytest.raises(policy.PolicyRefusal):
            resolve_path(str(_omni_home() / "memory"))
    finally:
        set_task(None)


def test_guard_normalizes_dotdot_traversal():
    """`..` 词法穿透必须被 resolve 后的规范路径挡住（§5.2 安全要点）。"""
    tricky = str(_omni_home() / "tasks" / "t_x" / "tmp" / ".." / ".." / ".." / "config.yaml")
    with pytest.raises(policy.PolicyRefusal) as ei:
        resolve_path(tricky)
    assert ei.value.rule == "self_carrier"


def test_task_tmp_is_the_only_exception():
    """当前任务 tmp 是唯一例外区：可解析、可写；tmp 之外的 tasks/ 仍拒。"""
    set_task("t_s2")
    RP.ensure_task_dirs("t_s2")
    try:
        p = resolve_path("scratch.py")
        assert RP.task_tmp("t_s2") in (p, p.parent)
        # 解析放行，但 tmp 内的 config.yaml 命中载体写拒（例外区不让位给载体规则）
        res = call_tool("write_file", {"path": "config.yaml", "content": "x"})
        assert res.get("ok") is False and res.get("denied") is True
        assert res.get("rule") == "carrier_write"
    finally:
        set_task(None)


# --- 载体文件全盘写拒 --------------------------------------------------------

def test_carrier_write_denied_anywhere():
    set_task("t_s3")
    RP.ensure_task_dirs("t_s3")
    try:
        for name, rule in [("mcp.json", "carrier_write"), ("AGENTS.md", "discipline_file")]:
            res = call_tool("write_file", {"path": name, "content": "x"})
            assert res.get("ok") is False and res.get("denied") is True, (name, res)
            assert res.get("rule") == rule, (name, res)
        # AGENTS.md 仍可读（F4.1 语义：只读纪律文件）
        tmp = RP.task_tmp("t_s3")
        (tmp / "AGENTS.md").write_text("# discipline", encoding="utf-8")
        res = call_tool("read_file", {"path": str(tmp / "AGENTS.md")})
        assert res.get("ok") is True, res
    finally:
        set_task(None)


# --- 允许根 / 根外写入 -------------------------------------------------------

def test_write_outside_root_autodenied_without_sink():
    """未注册 sink（直调兜底）时，根外写入 fail-closed 拒绝（rule=auto_deny）。"""
    set_task("t_s4")
    RP.ensure_task_dirs("t_s4")
    try:
        res = call_tool("write_file", {"path": str(REPO_ROOT / "x_s1_probe.txt"),
                                       "content": "x"})
        assert res.get("ok") is False and res.get("denied") is True
        assert res.get("rule") == "auto_deny"
        assert not (REPO_ROOT / "x_s1_probe.txt").exists()
    finally:
        set_task(None)


def test_write_inside_root_passes_without_sink():
    """根内（任务 tmp）写入静默放行：无需审批、不产生卡的干预。"""
    set_task("t_s5")
    RP.ensure_task_dirs("t_s5")
    try:
        res = call_tool("write_file", {"path": "out.txt", "content": "hello"})
        assert res.get("ok") is True, res
        assert (RP.task_tmp("t_s5") / "out.txt").read_text(encoding="utf-8") == "hello"
    finally:
        set_task(None)


def test_allow_roots_config_extends_write_zone(tmp_path):
    """security.allow_write_roots 扩展写入区：根内放行、根外仍拒。

    允许根面向用户工作区（保护根之外；guard 绝对优先于允许根——保护根内
    除当前任务 tmp 外永远拒绝，允许根不能豁免它）。
    """
    extra_root = tmp_path / "workspace_root"
    extra_root.mkdir(parents=True, exist_ok=True)
    policy.bind(task_id="t_s6", full_access=False,
                security_cfg={"allow_write_roots": [str(extra_root)]})
    set_task("t_s6")
    try:
        target = extra_root / "note.txt"
        res = call_tool("write_file", {"path": str(target), "content": "ok"})
        assert res.get("ok") is True, res
        assert target.read_text(encoding="utf-8") == "ok"
        # 允许根本身落在保护根内时不可豁免（guard 先行）
        inside = RP.global_omni() / "evil.txt"
        res2 = call_tool("write_file", {"path": str(inside), "content": "x"})
        assert res2.get("denied") is True and res2.get("rule") == "self_carrier"
    finally:
        set_task(None)
        policy.unbind()


# --- 结构化拒绝契约 ----------------------------------------------------------

def test_refusal_shape_contract():
    """拒绝结果形状（S1/S2 共用契约，§5.2）：denied/policy/rule/error/hint 齐全。"""
    set_task("t_s7")
    try:
        res = call_tool("read_file", {"path": str(_omni_home() / "config.yaml")})
        assert res.get("ok") is False
        assert res.get("denied") is True
        assert res.get("policy") == "S1"
        assert res.get("rule") == "self_carrier"
        assert res.get("error") and res.get("hint")
    finally:
        set_task(None)


def test_shell_exec_still_tripwire_not_path_gated():
    """S1 不拦 shell（命令字符串不可判，§5.1）：echo 重定向到受保护路径不会被
    路径围栏拦截——由 S2 整条审批兜住（本用例只验证围栏不越权到 shell）。"""
    set_task("t_s8")
    RP.ensure_task_dirs("t_s8")
    try:
        # 无 sink + 未绑定安全上下文 → S2 前置门 auto_deny（而非 S1 路径错误）
        res = call_tool("shell_exec", {"command": f"echo x > {_omni_home() / 'config.yaml'}",
                                       "shell": _SHELL})
        assert res.get("denied") is True
        assert res.get("rule") == "auto_deny"
        assert res.get("policy") == "S2"
    finally:
        set_task(None)
