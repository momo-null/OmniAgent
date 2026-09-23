"""F4.2（KJ-1）— memory 注入迁会话流（尾部重插），开关可一键回退 system。

注：F4.1b 起尾部重插只承载 **memory**（纪律文件 AGENTS.md 已迁 system prompt），
本文件的断言随之下移到 ``_build_memory_injection``。

覆盖：
1. memory_in_user=true（缺省）→ 记忆并入尾部块（带 memory 来源标注）；system 注入关闭。
2. memory_in_user=false → 尾部块不含记忆（回退 system 注入路径）。
3. 子 agent（is_sub=True）不入记忆（保持既有「记忆仅主链」语义）。
"""
import config as config_mod
from omni_core.local.tool_loop import ToolLoop


def _loop():
    loop = ToolLoop({"model": "m", "base_url": "http://x", "api_key": "k"}, verbose=False)
    loop.knowledge_cfg["memory"] = True     # 打开 memory 能力
    return loop


def _patch(monkeypatch, memory_in_user):
    import omni_core.local.knowledge_inject as ki
    monkeypatch.setattr(ki, "load_memory_text", lambda: "MEMTEXT-42")
    cfg = {
        "runtime.long_task.memory_in_user": memory_in_user,
        "runtime.long_task.instructions_limit": 8192,
    }
    monkeypatch.setattr(config_mod, "get_config", lambda p, d=None: cfg.get(p, d))


def test_memory_in_user_true_puts_memory_in_tail(monkeypatch):
    _patch(monkeypatch, True)
    loop = _loop()
    block, layers = loop._build_memory_injection(False)
    assert "MEMTEXT-42" in block
    assert layers == ["memory"]
    # system 注入关闭 → 由尾部承载
    assert loop._memory_in_system() is False


def test_memory_in_user_false_reverts_to_system(monkeypatch):
    _patch(monkeypatch, False)
    loop = _loop()
    block, layers = loop._build_memory_injection(False)
    assert "MEMTEXT-42" not in block
    assert layers == []
    # 回退：system 注入启用
    assert loop._memory_in_system() is True


def test_sub_agent_never_injects_memory(monkeypatch):
    _patch(monkeypatch, True)
    loop = _loop()
    block, layers = loop._build_memory_injection(True)
    assert "MEMTEXT-42" not in block
    assert layers == []


def test_tail_block_carries_no_discipline_file(monkeypatch, tmp_path):
    """F4.1b 回归：AGENTS.md 不再出现在尾部块（已迁 system）。"""
    import omni_core.local.runtime_paths as rp

    g = tmp_path / "g.md"
    g.write_text("DISCIPLINE-RULE", encoding="utf-8")
    monkeypatch.setattr(rp, "global_agents_file", lambda: g)

    _patch(monkeypatch, True)
    loop = _loop()
    block, layers = loop._build_memory_injection(False)
    assert "DISCIPLINE-RULE" not in block
    assert "global" not in layers
