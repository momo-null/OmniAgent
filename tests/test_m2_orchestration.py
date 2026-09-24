"""M7：去分层通用多 agent 编排图（omni_core.orchestration.graph）。

设计：doc/plans/multi-agent-redesign-2026-09-13.md §2

核心场景：
1. 主 agent 不派发 → 单 agent 跑完就结束（默认路径，只跑一轮 main）
2. 主 agent 派发 N 项 → N 个子 agent **并发**执行（wallclock 明显小于串行）
3. 子 agent 结果回灌主 agent 下一轮，主 agent 据此收尾
4. max_rounds 兜底：主 agent 一直派发也不会无限循环
5. max_parallel 截断：单轮并发数不超上限
6. 主 agent 未完成且无派发 → 收尾为失败（无内核假成功词表）
"""
import asyncio
import time

import pytest

from omni_core.orchestration.graph import build_agent_graph


def _init(**over):
    state = {
        "objective": "o",
        "done_when": "",
        "task_id": "t_graph",
        "project_id": None,
        "max_steps": None,
        "round": 0,
        "plan": [],
        "results": [],
        "consumed": 0,
        "budget_used": 0,
        "done": False,
    }
    state.update(over)
    return state


def _finalize(state, **kw):
    return dict(kw)


def run(graph, **over):
    return asyncio.run(graph.ainvoke(_init(**over)))


# === 1. 默认单 agent =======================================================
def test_single_agent_no_dispatch_runs_once():
    calls = {"main": 0, "sub": 0}

    def main_fn(state, prev, injected=None):
        calls["main"] += 1
        return {"done": True, "plan": [], "steps": 3, "reason": "ok"}

    def sub_fn(item, budget):
        calls["sub"] += 1
        return {"success": True}

    g = build_agent_graph(run_main_fn=main_fn, run_sub_fn=sub_fn, finalize_fn=_finalize)
    final = run(g)

    assert calls == {"main": 1, "sub": 0}
    assert final["result"]["success"] is True
    assert final["result"]["steps"] == 3
    assert final["round"] == 1


# === 2. 派发 → 并发扇出 ====================================================
def test_dispatch_fans_out_in_parallel():
    calls = {"main": 0, "sub": 0}
    inflight = {"n": 0, "peak": 0}

    def main_fn(state, prev, injected=None):
        calls["main"] += 1
        if calls["main"] == 1:
            return {"done": False, "plan": [{"desc": "a"}, {"desc": "b"}, {"desc": "c"}],
                    "steps": 1}
        return {"done": True, "plan": [], "steps": 1, "reason": "done"}

    def sub_fn(item, budget):
        # 峰值并发计数：串行执行时 peak 只会是 1
        inflight["n"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["n"])
        time.sleep(0.4)                      # 模拟一个耗时子任务
        inflight["n"] -= 1
        calls["sub"] += 1
        return {"success": True, "desc": item.get("desc", "")}

    g = build_agent_graph(run_main_fn=main_fn, run_sub_fn=sub_fn, finalize_fn=_finalize,
                          max_rounds=3, max_parallel=4)
    t0 = time.time()
    final = run(g)
    elapsed = time.time() - t0

    assert calls["sub"] == 3
    assert calls["main"] == 2                       # 派发轮 + 回收后的收尾轮
    assert inflight["peak"] >= 2, "子 agent 没有真并发（峰值并发数 < 2）"
    assert elapsed < 1.2, f"疑似串行执行：3×0.4s 用了 {elapsed:.2f}s"
    assert len(final["results"]) == 3
    assert final["result"]["success"] is True


def test_sub_results_are_fed_back_to_main():
    seen = []

    def main_fn(state, prev, injected=None):
        seen.append(list(prev))
        if len(seen) == 1:
            return {"done": False, "plan": [{"desc": "x"}], "steps": 1}
        return {"done": True, "plan": [], "steps": 1, "reason": "done"}

    def sub_fn(item, budget):
        return {"success": True, "desc": item.get("desc", ""), "reason": "完成"}

    g = build_agent_graph(run_main_fn=main_fn, run_sub_fn=sub_fn, finalize_fn=_finalize)
    run(g)

    assert seen[0] == []                       # 首轮没有历史结果
    assert seen[1][0]["desc"] == "x"           # 第二轮拿到了子 agent 结果


# === 3. 兜底与上限 =========================================================
def test_max_rounds_stops_infinite_dispatch():
    rounds = {"main": 0}

    def main_fn(state, prev, injected=None):
        rounds["main"] += 1
        return {"done": False, "plan": [{"desc": f"r{rounds['main']}"}], "steps": 1}

    def sub_fn(item, budget):
        return {"success": True}

    g = build_agent_graph(run_main_fn=main_fn, run_sub_fn=sub_fn, finalize_fn=_finalize,
                          max_rounds=2)
    final = run(g)

    assert rounds["main"] == 3                  # 首轮 + 2 轮派发上限后收尾判定
    assert final["result"]["success"] is False  # 主 agent 从未宣布完成 → 失败


def test_max_parallel_caps_fanout():
    calls = {"sub": 0}

    def main_fn(state, prev, injected=None):
        if state.get("round", 0) == 0:
            return {"done": False, "plan": [{"desc": str(i)} for i in range(5)], "steps": 0}
        return {"done": True, "plan": [], "steps": 0, "reason": "done"}

    def sub_fn(item, budget):
        calls["sub"] += 1
        return {"success": True, "desc": item.get("desc", "")}

    g = build_agent_graph(run_main_fn=main_fn, run_sub_fn=sub_fn, finalize_fn=_finalize,
                          max_parallel=2)
    final = run(g)

    assert calls["sub"] == 2
    assert len(final["results"]) == 2


def test_no_dispatch_and_not_done_is_failure():
    def main_fn(state, prev, injected=None):
        return {"done": False, "plan": [], "steps": 2, "reason": "budget_exhausted"}

    def sub_fn(item, budget):
        return {"success": True}

    g = build_agent_graph(run_main_fn=main_fn, run_sub_fn=sub_fn, finalize_fn=_finalize)
    final = run(g)

    assert final["result"]["success"] is False
    assert "budget_exhausted" in final["result"]["reason"]


# === 4. 去分层的红线（防回归） =============================================
def test_graph_has_no_hardcoded_layering():
    import inspect
    from omni_core.orchestration import graph as G
    from omni_core.local import loop as TL

    src = inspect.getsource(G)
    src = src.split('"""', 2)[-1]        # 去掉模块 docstring（里面会解释性提到旧名词）
    for word in ("manager", "worker", "reflect", "planner"):
        assert word not in src.lower(), f"编排层又出现了角色分层名词: {word}"

    # 内核不再有「目标词表判断假成功」的领域硬编码
    assert "_ENUM_KEYS" not in inspect.getsource(TL)


# === 5. 预算分配 ===========================================================
def test_budget_split_across_subtasks():
    budgets = []

    def main_fn(state, prev, injected=None):
        if state.get("round", 0) == 0:
            return {"done": False, "plan": [{"desc": "a"}, {"desc": "b"}], "steps": 2}
        return {"done": True, "plan": [], "steps": 1, "reason": "done"}

    def sub_fn(item, budget):
        budgets.append(budget)
        return {"success": True}

    g = build_agent_graph(run_main_fn=main_fn, run_sub_fn=sub_fn, finalize_fn=_finalize)
    run(g, max_steps=10)

    # 总预算 10 - 主 agent 已用 2 = 8，两个子任务各 4
    assert budgets == [4, 4]
