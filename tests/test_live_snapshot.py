"""回归：当前轮「实时过程快照」——修复执行中刷新/切任务后过程流消失。

背景（2026-09-23）：实时过程（thinking / toolcall / message）只写 task 级内存 outbox，
而 SSE 生成器是「消费即清空」的队列（`chat_runtime._stream_gen`），会话历史又只在轮末
才落盘 → 刷新或切换 task 后，进行中的这一轮没有任何可回放来源，前端全程无输出，
直到本轮结束、终态 agent 消息到达才一次性出现。

方案 A：outbox 之外另存一份**按 id upsert 的有界快照**，由 `GET /api/runtime/live`
提供给前端在挂载/切任务时回放。本文件覆盖：
1. 流式增量按 block_id 在快照内原地覆盖（不膨胀成多条）；
2. 工具调用也带 id（前端据此 upsert，快照回放与 SSE 增量不重复成两张卡）；
3. 快照有界（超限丢最旧）、可清空、返回深拷贝；
4. `GET /api/runtime/live` 形状正确。
"""
from fastapi.testclient import TestClient

import backend.server as server_mod
from backend.api.routers import helpers as H


def test_stream_deltas_upsert_in_snapshot():
    tid = "t_live_upsert"
    H.clear_live_snapshot(tid)
    H.push_thinking_stream("brain", "m", "brain-R0", "第一段", task_id=tid)
    H.push_thinking_stream("brain", "m", "brain-R0", "第一段+第二段", task_id=tid)

    snap = H.live_snapshot(tid)
    assert len(snap["thinking"]) == 1, "同一 block_id 应原地覆盖，不新增"
    assert snap["thinking"][0]["content"] == "第一段+第二段"
    assert snap["thinking"][0]["id"] == "brain-R0"
    H.clear_live_snapshot(tid)


def test_toolcall_gets_id_for_upsert():
    tid = "t_live_toolcall"
    H.clear_live_snapshot(tid)
    H.push_tool_call("brain", "web_search", '{"q":1}', "ok", "m", task_id=tid)

    snap = H.live_snapshot(tid)
    assert len(snap["toolcall"]) == 1
    item = snap["toolcall"][0]
    assert item["name"] == "web_search"
    assert item.get("id"), "工具调用必须带 id，供前端按 id upsert"
    # outbox 与快照是同一份内容（前端可能从任一路径拿到）
    box = H._ensure_outbox(tid)
    assert box["toolcall"][0]["id"] == item["id"]
    H.clear_live_snapshot(tid)


def test_snapshot_bounded_and_clearable():
    tid = "t_live_bound"
    H.clear_live_snapshot(tid)
    for i in range(H._LIVE_LIMIT + 25):
        H.push_tool_call("brain", f"tool{i}", "", "", "m", task_id=tid)

    snap = H.live_snapshot(tid)
    assert len(snap["toolcall"]) == H._LIVE_LIMIT, "超限应丢最旧，保持有界"
    assert snap["toolcall"][-1]["name"] == f"tool{H._LIVE_LIMIT + 24}"

    # 返回的是深拷贝：外部改动不影响内部状态
    snap["toolcall"].clear()
    assert len(H.live_snapshot(tid)["toolcall"]) == H._LIVE_LIMIT

    H.clear_live_snapshot(tid)
    empty = H.live_snapshot(tid)
    assert all(not empty[ch] for ch in H._LIVE_CHANNELS)


def test_live_endpoint_shape():
    tid = "t_live_api"
    H.clear_live_snapshot(tid)
    H.push_message_stream("brain", "m", "brain-M0", "口播", task_id=tid)
    with TestClient(server_mod.app) as c:
        r = c.get("/api/runtime/live", params={"task_id": tid})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["task_id"] == tid
    assert isinstance(d["running"], bool)
    assert d["message"][0]["content"] == "口播"
    H.clear_live_snapshot(tid)
