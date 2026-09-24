"""U3：provider 错误友好降级（Batch-1 任务 U3）。

覆盖三层：
1. classify_provider_error 单测：用**真实 openai 异常类**构造（不再伪造类名），
   覆盖 402/429/401/403/408/5xx/网络层/非服务异常/.response 嵌套状态码。
2. run_subtask_sdk 集成：SDK Runner 抛服务侧真实异常时，结果带 provider_error=True、
   escalated 保持 True、文案被替换为友好降级语（不再带「runner 异常」原生前缀）。
3. 落盘回归：provider 错误命中时，任务索引 state=paused 且不写 finished_at。

注：此前版本用伪造异常类名（_FakeProviderError）冒充 APIStatusError，恰好骗过字符串匹配，
把 5xx/403 等真实漏判藏住了；本版一律使用 openai 真实异常类，确保测的是真实 SDK 行为。
"""
import httpx
import types

import pytest

from openai import (
    APIStatusError,
    RateLimitError,
    AuthenticationError,
    InternalServerError,
    PermissionDeniedError,
    BadRequestError,
    APIConnectionError,
    APITimeoutError,
)

from omni_core.brain.sdk_loop import classify_provider_error, run_subtask_sdk


# --- 真实 openai 异常构造 -----------------------------------------------------
def _make_status_error(code: int):
    """构造带真实 HTTP 状态码的 openai 服务侧异常（贴近真实 SDK 的子类名）。"""
    req = httpx.Request("POST", "http://127.0.0.1:9")
    resp = httpx.Response(code, request=req)
    cls = {
        401: AuthenticationError,
        402: APIStatusError,
        403: PermissionDeniedError,
        408: APIStatusError,
        429: RateLimitError,
        500: InternalServerError,
        503: InternalServerError,
    }.get(code, APIStatusError)
    return cls(f"http{code}", response=resp, body=None)


# --- 1. classify_provider_error 单测 ------------------------------------------
def test_classify_402_quota_message():
    msg = classify_provider_error(_make_status_error(402))
    assert msg and "配额" in msg and "余额" in msg and "暂停" in msg


def test_classify_429_rate_limit_message():
    msg = classify_provider_error(_make_status_error(429))
    assert msg and "暂时不可用" in msg and "限流" in msg


def test_classify_401_403_408_5xx_all_degrade():
    # 修复前 5xx 与 403 被漏判（返回 None）。此处覆盖服务侧可恢复状态码。
    for code in (401, 403, 408, 500, 503):
        msg = classify_provider_error(_make_status_error(code))
        assert msg is not None, f"status {code} 应降级但返回 None"
        assert "暂时不可用" in msg
        assert "暂停" in msg


def test_classify_network_errors_degrade_without_status():
    # 网络层无 HTTP 状态码，靠通用类名兜底
    req = httpx.Request("POST", "http://127.0.0.1:9")
    assert "网络" in (classify_provider_error(APIConnectionError(message="conn", request=req)) or "")
    assert "网络" in (classify_provider_error(APITimeoutError(request=req)) or "")


def test_classify_non_provider_returns_none():
    # 普通异常 / 400 请求参数错误 不应降级（走原有失败逻辑）
    assert classify_provider_error(ValueError("boom")) is None
    assert classify_provider_error(RuntimeError("x")) is None
    assert classify_provider_error(None) is None
    assert classify_provider_error(
        BadRequestError(
            "bad",
            response=httpx.Response(400, request=httpx.Request("POST", "http://127.0.0.1:9")),
            body=None,
        )
    ) is None


# --- 2. run_subtask_sdk 集成 --------------------------------------------------
def _make_raiser(exc):
    def _raiser(*a, **k):
        raise exc

    return _raiser


def _run_with_provider_error(monkeypatch, exc):
    """monkeypatch sdk_loop.run_async 抛 exc，跑一次 run_subtask_sdk，返回结果 dict。"""
    import omni_core.brain.sdk_loop as sl

    monkeypatch.setattr(sl, "run_async", _make_raiser(exc))
    brain = {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}
    gate = types.SimpleNamespace(verify_done=lambda: (False, ""), verify_count=0)
    return run_subtask_sdk(
        brain,
        instructions="do it",
        user_input="hi",
        tools=[],
        gate=gate,
        mcp_servers=None,
    )


def test_run_subtask_sdk_402_sets_provider_error(monkeypatch):
    res = _run_with_provider_error(monkeypatch, _make_status_error(402))
    assert res["provider_error"] is True
    assert res["escalated"] is True
    assert "runner 异常" not in res["reason"]
    assert "配额" in res["reason"] and "余额" in res["reason"]


def test_run_subtask_sdk_429_sets_provider_error(monkeypatch):
    res = _run_with_provider_error(monkeypatch, _make_status_error(429))
    assert res["provider_error"] is True
    assert res["escalated"] is True
    assert "runner 异常" not in res["reason"]
    assert "暂时不可用" in res["reason"]


def test_run_subtask_sdk_5xx_sets_provider_error(monkeypatch):
    # 真实服务端错误（5xx）也必须降级——这是修复前的漏判点
    res = _run_with_provider_error(monkeypatch, _make_status_error(500))
    assert res["provider_error"] is True
    assert res["escalated"] is True
    assert "runner 异常" not in res["reason"]
    assert "暂时不可用" in res["reason"]


def test_run_subtask_sdk_network_sets_provider_error(monkeypatch):
    res = _run_with_provider_error(monkeypatch, APIConnectionError(message="conn", request=httpx.Request("POST", "http://127.0.0.1:9")))
    assert res["provider_error"] is True
    assert "网络" in res["reason"]


def test_run_subtask_sdk_non_provider_no_flag(monkeypatch):
    res = _run_with_provider_error(monkeypatch, ValueError("boom"))
    assert res["provider_error"] is False
    assert "runner 异常" in res["reason"]


# --- 3. 落盘回归：paused 且不写 finished_at ------------------------------------
def test_provider_error_task_stored_paused_no_finished_at(tmp_path, monkeypatch):
    """走真实 ToolLoop.run_task 全链路：SDK 抛 402 -> 任务索引 paused、无 finished_at。"""
    from omni_core.local import runtime_paths as P
    from omni_core.local.task_store import TaskStore
    from omni_core.local.loop import ToolLoop, TaskSpec

    # 隔离 home（conftest 已做，这里再确保一次以自包含）
    fake = tmp_path / ".omniagent"
    monkeypatch.setattr(P, "_GLOBAL", fake)
    P.ensure_global_dirs()

    import omni_core.brain.sdk_loop as sl

    monkeypatch.setattr(sl, "run_async", _make_raiser(_make_status_error(402)))

    # 生产里任务由 API submit 创建；这里先建一个拿到确定性 task_id
    meta = TaskStore.create(objective="o")
    tid = meta["task_id"]

    loop = ToolLoop(
        {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k", "capabilities": {}},
        verbose=False,
    )
    res = loop.run_task(TaskSpec(objective="o", task_id=tid, max_steps=1))

    assert res.get("provider_error") is True
    after = TaskStore.get(tid)
    assert after is not None, "任务索引应已落盘"
    assert after["state"] == "paused"
    assert after.get("finished_at") in (None, "")


def test_task_store_paused_does_not_write_finished_at():
    """直接验证 TaskStore 契约：paused 状态不会补写 finished_at（T3 分支依赖此契约）。"""
    from omni_core.local.task_store import TaskStore

    meta = TaskStore.create(objective="x")
    tid = meta["task_id"]
    # provider 错误分支：只传 state=paused，不传 finished_at
    updated = TaskStore.update(tid, state="paused", success=False, runs=["r1"])
    assert updated["state"] == "paused"
    assert updated.get("finished_at") in (None, "")
    # 对照组：failed 应补写 finished_at（确保契约区分对待）
    updated2 = TaskStore.update(tid, state="failed", success=False, runs=["r1"])
    assert updated2["state"] == "failed"
    assert updated2.get("finished_at")
