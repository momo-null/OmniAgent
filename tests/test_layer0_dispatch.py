"""Layer 0 派发底座（2026-09-30，doc/plans/team-mode-layer0-design.md §5 验证清单）。

覆盖：
1. `agent` 字段存活：parse_dispatch_items 白名单透传（终稿修订记录 #10 新增断言）。
2. dispatch 元工具 docstring 动态列出执行单元注册表槽名（§3.2）。
3. allow_dispatch 门控解绑固定双层，只剩 enabled（§3.5）。
4. _run_subtask 按 target_slot 路由 + 缺槽回退默认槽（§3.4）。
5. core 执行单元注册表：executors / default_executor_slot / 向后兼容别名（§3.1）。

全部为无网络单测（LLM 调用不触达，仅验证字段流与结构）。
"""
import asyncio
import inspect
import json
import types

from omni_core.brain.sdk_loop import (
    build_meta_tools,
    parse_dispatch_items,
)


class _State:
    def __init__(self):
        self.dispatch_plan = None
        self.steps = 0
        self.done = False
        self.escalated = False


def _gate():
    return types.SimpleNamespace(verify_done=lambda: (True, ""), peek=lambda: (False, ""),
                                 verify_count=0, has_condition=False)


def _invoke(tool, **kwargs):
    ctx = types.SimpleNamespace(
        tool_name=getattr(tool, "name", "tool"), _function_tool_arguments=None
    )
    return asyncio.run(tool.on_invoke_tool(ctx, json.dumps(kwargs)))


def _dispatch_tool(available_slots=None):
    tools = build_meta_tools(_State(), _gate(), None, allow_dispatch=True,
                             available_slots=available_slots)
    for t in tools:
        if getattr(t, "name", "") == "dispatch":
            return t
    raise AssertionError("dispatch 元工具未暴露")


# --- 1. parse_dispatch_items：agent 字段存活（§3.3 / 修订记录 #10）-----------
def test_parse_keeps_agent():
    items = parse_dispatch_items(
        json.dumps([{"desc": "a", "done_when": "b", "agent": "researcher"}])
    )
    assert items == [{"desc": "a", "done_when": "b", "agent": "researcher"}]


def test_parse_drops_missing_agent():
    items = parse_dispatch_items(json.dumps([{"desc": "a", "done_when": "b"}]))
    assert "agent" not in items[0], "agent 缺省不应注入空键"


def test_parse_drops_blank_agent():
    items = parse_dispatch_items(
        json.dumps([{"desc": "a", "done_when": "b", "agent": "   "}])
    )
    assert "agent" not in items[0], "agent 空白串视为未指定"


def test_parse_coerces_nonstring_agent():
    # 实现语义：str(agent).strip()，非空即保留——数字 123 宽容转为 "123"
    items = parse_dispatch_items(
        json.dumps([{"desc": "a", "done_when": "b", "agent": 123}])
    )
    assert items[0].get("agent") == "123"


# --- 2. dispatch 元工具端到端透传 agent（§3.2→§3.3 链路）----------------------
def test_dispatch_tool_passes_agent_through():
    tool = _dispatch_tool(available_slots=["worker", "researcher"])
    out = _invoke(tool, items=json.dumps(
        [{"desc": "a", "done_when": "A", "agent": "researcher"},
         {"desc": "b", "done_when": "B"}]))
    plan = out.get("items") or []
    assert plan[0].get("agent") == "researcher"
    assert "agent" not in plan[1]


def test_dispatch_docstring_lists_slots():
    # 回归守卫：agents SDK 经 griffe 静态解析源码提取 docstring，f-string docstring
    # 会使整个 description 变空（2026-09-30 踩坑）——description 必须非空且含槽名
    tool = _dispatch_tool(available_slots=["worker", "researcher"])
    desc = getattr(tool, "description", "") or ""
    assert "researcher" in desc and "worker" in desc, "description 应列出注册表槽名"
    assert "need_verify" in desc, "description 不得回退丢失参数说明"


def _graph_runner_source():
    from omni_core.local.loop import graph_runner
    return inspect.getsource(graph_runner)


# --- 3. 门控解绑固定双层（§3.5 终稿）-----------------------------------------
def test_gate_unbound_from_executor_is_planner():
    src = _graph_runner_source()
    assert "not self.executor_is_planner" not in src, \
        "门控不得再依赖 executor_is_planner（brain 自派发为合法语义）"


def test_gate_enabled_only():
    src = _graph_runner_source()
    assert 'bool(_dispatch_cfg.get("enabled", True))' in src, "门控只剩 enabled 开关"


# --- 4. _run_subtask 按槽路由（§3.4）------------------------------------------
def test_run_subtask_routes_by_slot():
    from omni_core.local.loop.graph_runner import GraphRunnerMixin
    sig = inspect.signature(GraphRunnerMixin._run_subtask)
    assert "target_slot" in sig.parameters
    src = inspect.getsource(GraphRunnerMixin._run_subtask)
    assert "self.executors.get(slot)" in src, "按槽取执行单元"
    assert "self.default_executor_slot" in src, "缺槽回退默认槽"


def test_sub_fn_wires_agent_into_target_slot():
    src = _graph_runner_source()
    assert 'target_slot=item.get("agent")' in src, "_sub_fn 传 agent 字段"
    assert 'res["agent"]' in src, "结果回填 agent 供摘要/排障"


# --- 5. core 执行单元注册表（§3.1）--------------------------------------------
def _core_source():
    from omni_core.local.loop import core
    return inspect.getsource(core)


def test_core_has_executor_registry():
    src = _core_source()
    assert "self.executors" in src, "执行单元注册表存在"
    assert "self.default_executor_slot" in src, "默认槽存在"
    assert "self.executor = self.executors" in src, "self.executor 向后兼容别名"


def test_executor_is_planner_is_observation_only():
    src = _core_source()
    assert "self.executor_is_planner = False" in src or \
           "self.executor_is_planner = True" in src, "观测标记仍写入（兼容轨迹消费方）"
