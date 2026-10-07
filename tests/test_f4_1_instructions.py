"""F4.1（D1）— 纪律文件写保护 + 注入块组装原语。

注：注入**位置**的验收已随 F4.1b 迁移——AGENTS.md 由「尾部重插」改为
:「run 起始读一次 → 并入 system prompt（run 内锁定）」，相应断言在
``tests/test_f41b_instructions_system.py``；本文件保留与位置无关的契约：

1. 注入块组装原语：来源标注、零注入、上限截断并标注；
2. 纪律文件对 agent 只读：AGENTS.md 写入/编辑被拒（record/memory 类写工具不得指向该名）。

注：原第 2 组「尾部重插包装」单测随 2026-10-07 TAM 对齐移除——尾部每步重发
机制（_TailInjectModel）已删除，现行注入形态见 test_memory_alignment.py。
"""
from pathlib import Path

import pytest

from omni_core.local.knowledge_inject import compose_injection_block
from omni_core.tools.base import call_tool
from omni_core.tools.loader import load_plugins, plugin_module


@pytest.fixture
def fs():
    """filesystem 已迁为官方插件（plugins/filesystem）：用例内取模块句柄。

    P2 把 `omni_core/tools/filesystem_tool.py` 整体迁到 `plugins/filesystem/plugin.py`
    且**不留兼容 re-export**，故这里改为「先幂等装载插件、再取模块」。
    """
    load_plugins({})
    module = plugin_module("filesystem")
    assert module is not None, "filesystem 插件未装载"
    return module


# --- 1. 组装 & 零注入 ---------------------------------------------------------
def test_compose_block_labels_and_empty():
    block, labels = compose_injection_block([("global", "G1"), ("task", "T1")])
    assert labels == ["global", "task"]
    assert "[来源: global]" in block and "[来源: task]" in block
    assert "G1" in block and "T1" in block
    assert compose_injection_block([]) == ("", [])
    assert compose_injection_block([("global", "   ")]) == ("", [])


def test_compose_block_title_override():
    """纪律块用专属标题（默认标题供辅助知识注入使用）。"""
    block, _ = compose_injection_block([("global", "G")], title="# 任务纪律（AGENTS.md）")
    assert block.startswith("# 任务纪律（AGENTS.md）")


# --- 2. 上限截断 --------------------------------------------------------------
def test_compose_block_truncates_with_annotation():
    block, labels = compose_injection_block([("global", "x" * 400)], limit=80)
    assert labels == ["global"]
    assert "截断" in block
    assert len(block) <= 80 + 40, "截断后仅多出简短标注"


# --- 3. 纪律文件写护已撤销（F4.1b 原 S1 载体规则） -------------------------
def test_agents_md_write_now_flows_through_s2(tmp_path, fs, approve_all_sink):
    """纪律文件写护已撤销：AGENTS.md 不再被静默拒，改走 S2（approve_all 批准）。"""
    p = tmp_path / "AGENTS.md"
    r = call_tool("write_file", {"path": str(p), "content": "nope"})
    assert r["ok"] is True, r
    assert p.read_text(encoding="utf-8") == "nope"
    # 编辑同样放行
    e = call_tool("edit_file", {"path": str(p), "old_string": "nope", "new_string": "changed"})
    assert e["ok"] is True, e
    assert p.read_text(encoding="utf-8") == "changed"


def test_agents_md_nested_write_allowed(tmp_path, fs, approve_all_sink):
    """嵌套同名文件同样不再受保护（大小写不敏感的名称规则已撤）。"""
    nested = Path(tmp_path) / "sub" / "agents.md"
    r = call_tool("write_file", {"path": str(nested), "content": "nope"})
    assert r["ok"] is True, r
    assert nested.read_text(encoding="utf-8") == "nope"
