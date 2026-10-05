"""skill 轴消费侧：search_skill + replay_skill。

覆盖：关键词检索（发现层）、回放白名单硬重放 / 白名单外降参考建议、
回放经 call_tool 走既有门（S0 read_only 拒绝 exec）、命中簿记。
"""
import json

import pytest

import omni_core.local.runtime_paths as _RP
from omni_core.local.runtime_paths import DEFAULT_PROJECT_ID


@pytest.fixture(autouse=True)
def _iso_global(tmp_path, monkeypatch):
    monkeypatch.setattr(_RP, "_GLOBAL", tmp_path / ".omniagent")
    _RP.ensure_global_dirs()


def _mk_skill(lib, name, steps, desc="", tags=None):
    from omni_core.local.skill_library import Skill, SkillSubstep

    sk = Skill(name=name, objective_pattern="目标：" + name, description=desc,
               tags=tags or [], substeps=[SkillSubstep(tool=t, args=dict(a)) for t, a in steps])
    lib.save(sk)
    return sk


@pytest.fixture()
def lib():
    from omni_core.local.skill_library import SkillLibrary

    return SkillLibrary(task_id="t_axis")


# === search_skill：发现层 =====================================================
def test_search_matches_name_desc_tags(lib):
    from omni_core.tools.skill_tool import search_skill

    # 注意：身份=归一化结构哈希。两个单步 shell_exec(<text>) 技能同构 → 同一
    # entry_id 会被 list_all 去重合并——测试数据必须结构不同（工具/键不同）。
    _mk_skill(lib, "skill_清理产物", [("shell_exec", {"command": "rm"}), ("list_dir", {"path": "."})],
              desc="清理构建产物目录", tags=["构建"])
    _mk_skill(lib, "skill_备份", [("read_file", {"path": "a.txt"})], desc="备份文件")

    r = search_skill(query="清理")
    assert r["ok"] is True
    names = [h["name"] for h in r["hits"]]
    assert "skill_清理产物" in names and "skill_备份" not in names
    assert search_skill(query="构建")["total"] >= 1  # 命中标签
    assert search_skill(query="备份文件")["total"] >= 1  # 命中描述


def test_search_summary_has_no_raw_args(lib):
    """发现层只给摘要，不外显动作参数（细节走 load_skill）。"""
    from omni_core.tools.skill_tool import search_skill

    _mk_skill(lib, "skill_私有宏", [("shell_exec", {"command": "secret-cmd"})], desc="做某事")
    r = search_skill(query="宏")
    assert r["hits"] and all("args" not in h for h in r["hits"])


def test_search_empty_query_rejected():
    from omni_core.tools.skill_tool import search_skill

    assert search_skill(query="")["ok"] is False


# === replay_skill：回放执行器 =================================================
def test_replay_whitelist_hard_replays_others_as_reference(lib, monkeypatch):
    """白名单内硬重放（经 call_tool），GUI/未知工具降「参考建议」。"""
    import omni_core.tools.base as base
    from omni_core.tools.skill_tool import replay_skill

    calls = []

    def fake_call_tool(name, args):
        calls.append((name, args))
        return {"ok": True, "output": f"ran {name}"}

    monkeypatch.setattr(base, "call_tool", fake_call_tool)
    _mk_skill(lib, "skill_混合宏", [
        ("shell_exec", {"command": "echo hi"}),
        ("press", {"key": "enter"}),   # GUI 类：不在白名单
    ])
    r = replay_skill(skill_name="skill_混合宏")
    assert r["ok"] is True
    assert r["replayed_steps"] == 1 and r["reference_steps"] == 1
    assert calls == [("shell_exec", {"command": "echo hi"})]
    ref = [s for s in r["steps"] if not s["replayed"]][0]
    assert "press(" in ref["reference"] and "enter" in ref["reference"]


def test_replay_s0_read_only_denies_exec(lib):
    """回放不是免检通道：read_only 模式下白名单内 exec 步被 S0 拒绝。"""
    from omni_core.tools.base import build_plugin_registry
    from omni_core.tools.policy import bind, unbind
    from omni_core.tools.skill_tool import replay_skill

    build_plugin_registry()
    _mk_skill(lib, "skill_跑命令", [("shell_exec", {"command": "echo hi"})])
    bind("t_axis", security_cfg={"mode": "read_only"})
    try:
        r = replay_skill(skill_name="skill_跑命令")
    finally:
        unbind()
    assert r["ok"] is True  # 回放工具本身完成，内部步骤被拒
    step = r["steps"][0]
    assert step["replayed"] is True
    assert step["result"].get("denied") is True
    assert step["result"].get("rule") == "mode_read_only"


def test_replay_records_use(lib, monkeypatch):
    import omni_core.tools.base as base
    from omni_core.tools.skill_tool import replay_skill

    monkeypatch.setattr(base, "call_tool", lambda name, args: {"ok": True})
    sk = _mk_skill(lib, "skill_记账", [("read_file", {"path": "a.txt"}),
                                       ("list_dir", {"path": "."})])
    before = sk.metadata.total_uses
    replay_skill(skill_name="skill_记账")
    after = lib.load("skill_记账").metadata.total_uses
    assert after == before + 1


def test_replay_missing_and_empty_and_disabled(lib):
    from omni_core.local.skill_library import Skill
    from omni_core.tools.skill_tool import replay_skill

    assert replay_skill(skill_name="不存在")["ok"] is False
    _mk_skill(lib, "skill_纯引导", [])
    assert replay_skill(skill_name="skill_纯引导")["ok"] is False
    sk = Skill(name="skill_禁用", disable_model_invocation=True,
               substeps=lib.list_all() and [] )
    sk.substeps = []
    lib.save(sk)
    assert replay_skill(skill_name="skill_禁用").get("disabled") is True
