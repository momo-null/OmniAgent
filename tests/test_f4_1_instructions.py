"""F4.1（D1）— 纪律文件写保护 + 注入块组装原语。

注：注入**位置**的验收已随 F4.1b 迁移——AGENTS.md 由「尾部重插」改为
:「run 起始读一次 → 并入 system prompt（run 内锁定）」，相应断言在
``tests/test_f41b_instructions_system.py``；本文件保留与位置无关的契约：

1. 注入块组装原语：来源标注、零注入、上限截断并标注；
2. 尾部重插包装（现由 F4.2 memory 使用）只改请求副本，空块零干预；
3. 纪律文件对 agent 只读：AGENTS.md 写入/编辑被拒（record/memory 类写工具不得指向该名）。
"""
import asyncio
from pathlib import Path

import pytest
from agents import Model

from omni_core.brain.sdk_loop import _TailInjectModel
from omni_core.local.knowledge_inject import compose_injection_block
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


class _Inner(Model):
    """记录收到的 input，返回占位结果。"""

    def __init__(self):
        self.seen = None

    async def get_response(self, *args, **kwargs):
        self.seen = kwargs.get("input", args[1] if len(args) >= 2 else None)
        return "resp"

    async def stream_response(self, *args, **kwargs):
        self.seen = kwargs.get("input", args[1] if len(args) >= 2 else None)
        yield "chunk"


# --- 1. 组装 & 零注入 ---------------------------------------------------------
def test_compose_block_labels_and_empty():
    block, labels = compose_injection_block([("global", "G1"), ("task", "T1")])
    assert labels == ["global", "task"]
    assert "[来源: global]" in block and "[来源: task]" in block
    assert "G1" in block and "T1" in block
    assert compose_injection_block([]) == ("", [])
    assert compose_injection_block([("global", "   ")]) == ("", [])


def test_compose_block_title_override():
    """纪律块用专属标题（默认标题供 memory 尾部块使用）。"""
    block, _ = compose_injection_block([("global", "G")], title="# 任务纪律（AGENTS.md）")
    assert block.startswith("# 任务纪律（AGENTS.md）")


# --- 2. 上限截断 --------------------------------------------------------------
def test_compose_block_truncates_with_annotation():
    block, labels = compose_injection_block([("global", "x" * 400)], limit=80)
    assert labels == ["global"]
    assert "截断" in block
    assert len(block) <= 80 + 40, "截断后仅多出简短标注"


# --- 3. 尾部重插：只改请求副本（现承载 F4.2 memory） ---------------------------
def test_tail_inject_appends_to_request_copy_only():
    inner = _Inner()
    m = _TailInjectModel(inner, "BLOCK")
    original = [{"role": "user", "content": "hi"}]
    asyncio.run(m.get_response(input=original))
    assert inner.seen == [{"role": "user", "content": "hi"},
                          {"role": "user", "content": "BLOCK"}]
    # 原列表不被修改（会话历史 items 不因注入增长）
    assert original == [{"role": "user", "content": "hi"}]


def test_tail_inject_noop_when_empty():
    inner = _Inner()
    m = _TailInjectModel(inner, "")
    original = [{"role": "user", "content": "hi"}]
    asyncio.run(m.get_response(input=original))
    assert inner.seen == original


def test_tail_inject_reports_once():
    seen = []
    inner = _Inner()
    m = _TailInjectModel(inner, "BLOCK", layers=["memory"],
                         on_inject=lambda c, ls: seen.append((c, ls)))
    asyncio.run(m.get_response(input=[{"role": "user", "content": "a"}]))
    asyncio.run(m.get_response(input=[{"role": "user", "content": "a"}]))
    assert len(seen) == 1, "注入指标只上报一次"
    assert seen[0][0] == len("BLOCK")
    assert seen[0][1] == ["memory"]


# --- 4. 纪律文件写保护（F4.1b 维持现状，不做改动） ------------------------------
def test_agents_md_write_blocked(tmp_path, fs):
    p = tmp_path / "AGENTS.md"
    r = fs.write_file(str(p), "nope")
    assert r["ok"] is False and "只读" in r["error"]
    assert not p.exists()
    # 编辑同样被拒
    p.write_text("orig", encoding="utf-8")
    e = fs.edit_file(str(p), "orig", "changed")
    assert e["ok"] is False and "只读" in e["error"]
    assert p.read_text(encoding="utf-8") == "orig"
    # 普通文件不受影响
    ok = fs.write_file(str(tmp_path / "ok.md"), "yes")
    assert ok["ok"] is True


def test_agents_md_write_guard_covers_nested_name(tmp_path, fs):
    """嵌套同名文件同样受保护（大小写不敏感）。"""
    nested = Path(tmp_path) / "sub" / "agents.md"
    r = fs.write_file(str(nested), "nope")
    assert r["ok"] is False and "只读" in r["error"]
    assert not nested.exists()
