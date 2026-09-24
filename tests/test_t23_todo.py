"""T2.3 (TD-1): todo_write 任务元工具验收。

直接驱动内核元工具构造器 build_meta_tools，用真实 function_tool 的 on_invoke_tool
入口验证 todo_write 的写入/校验/持久化语义与「子 agent 不暴露」约束。
"""
import asyncio
import json
import os

import pytest

from omni_core.brain.sdk_loop import build_meta_tools, SubtaskState


class _MemStore:
    """复用与 _TodoStore 相同磁盘契约的本地 store（指向临时文件）。"""

    def __init__(self, path):
        self.path = str(path)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save(self, data):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _make_store(path):
    from omni_core.local.loop import _TodoStore

    return _TodoStore(str(path))


def _make_tools(allow_dispatch=True, todo_store=None):
    state = SubtaskState()
    gate = type(
        "G",
        (),
        {
            "verify_done": lambda: (False, ""),
            "verify": lambda c: (False, ""),
            "note": lambda t: {"ok": True},
            "verify_count": 0,
        },
    )()
    return build_meta_tools(state, gate, None, allow_dispatch, todo_store)


def _invoke(tool, **kwargs):
    from types import SimpleNamespace

    ctx = SimpleNamespace(tool_name=tool.name)
    return asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))


def _get(tools, name):
    return next((t for t in tools if t.name == name), None)


def test_todo_write_present_only_for_main_agent():
    main = _make_tools(allow_dispatch=True, todo_store=_MemStore("/tmp/x.json"))
    sub = _make_tools(allow_dispatch=False)
    assert _get(main, "todo_write") is not None
    assert _get(sub, "todo_write") is None
    # 主 agent 但无 store 也不暴露
    assert _get(_make_tools(allow_dispatch=True, todo_store=None), "todo_write") is None


def test_todo_write_roundtrip_and_persist(tmp_path):
    store = _make_store(tmp_path / "todo.json")
    tools = _make_tools(allow_dispatch=True, todo_store=store)
    tw = _get(tools, "todo_write")
    data = [
        {"content": "收集需求", "status": "done"},
        {"content": "编写代码", "status": "in_progress"},
        {"content": "回归测试", "status": "pending"},
    ]
    res = _invoke(tw, items_json=json.dumps(data))
    assert res["ok"] is True
    assert res["total"] == 3
    assert res["done"] == 1
    # 落盘后另一实例可读取（跨 ToolLoop 实例等价）
    reader = _make_store(tmp_path / "todo.json")
    assert reader.load() == data


def test_todo_write_rejects_invalid_json():
    store = _make_store("/tmp/x.json")
    tw = _get(_make_tools(allow_dispatch=True, todo_store=store), "todo_write")
    res = _invoke(tw, items_json="{not valid json")
    assert res["ok"] is False
    assert "JSON" in res["error"]


def test_todo_write_rejects_non_array():
    store = _make_store("/tmp/x.json")
    tw = _get(_make_tools(allow_dispatch=True, todo_store=store), "todo_write")
    res = _invoke(tw, items_json=json.dumps({"content": "x"}))
    assert res["ok"] is False
    assert "数组" in res["error"]


def test_todo_write_rejects_bad_status():
    store = _make_store("/tmp/x.json")
    tw = _get(_make_tools(allow_dispatch=True, todo_store=store), "todo_write")
    res = _invoke(
        tw,
        items_json=json.dumps([{"content": "x", "status": "weird"}]),
    )
    assert res["ok"] is False
    assert "status" in res["error"]


def test_todo_write_rejects_missing_content():
    store = _make_store("/tmp/x.json")
    tw = _get(_make_tools(allow_dispatch=True, todo_store=store), "todo_write")
    res = _invoke(tw, items_json=json.dumps([{"status": "pending"}]))
    assert res["ok"] is False
    assert "content" in res["error"]
