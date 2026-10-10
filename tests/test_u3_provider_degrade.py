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
from agents.exceptions import ModelBehaviorError

from omni_core.brain.sdk_loop import classify_provider_error, run_subtask_sdk
from omni_core.brain.errors import is_retryable_provider_glitch, _is_empty_choices


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


def test_classify_431_header_too_large():
    # 431 = 请求头字段过大，此前漏判（落 runner 异常硬终止）。现应降级暂停并给明确提示。
    msg = classify_provider_error(_make_status_error(431))
    assert msg is not None, "status 431 应降级但返回 None"
    assert "431" in msg and "暂停" in msg
    assert "runner 异常" not in msg


def test_classify_empty_choices_message():
    # 空 choices 型 ModelBehaviorError：provider 偶发空响应，应降级暂停（不落 runner 异常）。
    err = ModelBehaviorError("ChatCompletion response has no choices (possible provider error payload)")
    msg = classify_provider_error(err)
    assert msg is not None, "no choices 应降级但返回 None"
    assert "no choices" in msg and "暂停" in msg


def test_is_empty_choices_and_retryable_glitch():
    # _is_empty_choices 只认 ModelBehaviorError + 'no choices' 关键字
    err = ModelBehaviorError("ChatCompletion response has no choices")
    assert _is_empty_choices(err) is True
    assert _is_empty_choices(ModelBehaviorError("other behavior")) is False
    assert _is_empty_choices(ValueError("no choices")) is False
    assert _is_empty_choices(None) is False
    # is_retryable_provider_glitch = 网络/超时层 + 空 choices
    req = httpx.Request("POST", "http://127.0.0.1:9")
    assert is_retryable_provider_glitch(APIConnectionError(message="c", request=req)) is True
    assert is_retryable_provider_glitch(err) is True
    # 带状态码的服务侧错误（429/5xx）不算瞬时抖动（走原分支直接暂停，不重试）
    assert is_retryable_provider_glitch(_make_status_error(429)) is False
    assert is_retryable_provider_glitch(_make_status_error(500)) is False
    assert is_retryable_provider_glitch(ValueError("x")) is False
    assert is_retryable_provider_glitch(None) is False


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
    # 3.6：退避重试会让瞬时错误持续抛时真睡数秒；测试里把退避 sleep 置为 no-op。
    monkeypatch.setattr(sl, "_PROVIDER_SLEEP", lambda *a, **k: None)
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


# --- 3.6 网络/超时退避重试（自愈，不静默暂停）-----------------------------------
def test_is_transient_network_error():
    """is_transient_network_error 只认无状态码的网络层异常，状态码类/非服务异常均否。"""
    from omni_core.brain.errors import is_transient_network_error

    req = httpx.Request("POST", "http://127.0.0.1:9")
    assert is_transient_network_error(APIConnectionError(message="c", request=req)) is True
    assert is_transient_network_error(APITimeoutError(request=req)) is True
    # 带状态码的「服务侧可恢复」错误不算瞬时网络层（走原分支直接暂停）
    assert is_transient_network_error(_make_status_error(429)) is False
    assert is_transient_network_error(_make_status_error(500)) is False
    # 非服务异常 / None
    assert is_transient_network_error(ValueError("x")) is False
    assert is_transient_network_error(None) is False


def _run_with_provider_async(monkeypatch, behavior):
    """behavior(call_count) -> 返回模拟 RunResult 或抛异常。run_async 被替换为该行为。"""
    import omni_core.brain.sdk_loop as sl

    monkeypatch.setattr(sl, "_PROVIDER_SLEEP", lambda *a, **k: None)

    class _FakeRes:
        def to_input_list(self):
            return []

    state = {"n": 0}

    def _fake_run_async(coro, timeout=None):
        state["n"] += 1
        out = behavior(state["n"])
        if isinstance(out, Exception):
            raise out
        return _FakeRes()

    monkeypatch.setattr(sl, "run_async", _fake_run_async)
    brain = {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}
    gate = types.SimpleNamespace(
        verify_done=lambda: (True, "done"), verify_count=0, has_condition=False)
    res = run_subtask_sdk(
        brain, instructions="do it", user_input="hi", tools=[], gate=gate, mcp_servers=None)
    res["_calls"] = state["n"]
    return res


def test_provider_retry_recovers_on_single_transient_error(monkeypatch):
    """验收①：注入 1 次模型 API 超时 → 任务自动重试恢复、不暂停（provider_error=False）。"""
    req = httpx.Request("POST", "http://127.0.0.1:9")

    def _behavior(n):
        # 第 1 次调用网络抖动，之后恢复
        if n == 1:
            return APIConnectionError(message="conn", request=req)
        return None  # 模拟正常 RunResult

    res = _run_with_provider_async(monkeypatch, _behavior)
    assert res["_calls"] == 2, "应重试 1 次后恢复"
    assert res["provider_error"] is False
    assert res["success"] is True, "应自动恢复而非暂停"
    assert res["paused"] is False


def test_provider_retry_exhausts_then_pauses(monkeypatch):
    """验收②：连续超 N 次 → 才暂停并保留进度（provider_error=True，绝静默 success）。"""
    req = httpx.Request("POST", "http://127.0.0.1:9")

    def _behavior(n):
        return APIConnectionError(message="conn", request=req)  # 始终网络抖动

    res = _run_with_provider_async(monkeypatch, _behavior)
    # 初始 1 次 + 默认 PROVIDER_RETRY_LIMIT=3 次退避重试 = 4 次模型调用后才暂停
    assert res["_calls"] == 1 + 3, f"重试耗尽才暂停，应调用 4 次: {res['_calls']}"
    assert res["provider_error"] is True
    assert res["escalated"] is True
    assert res["success"] is False
    # repeat_failures 应接通重试次数（可观测，非静默）
    assert res["repeat_failures"] >= 3, f"重试计数应接通: {res['repeat_failures']}"


def test_provider_retry_disabled_pauses_immediately(monkeypatch):
    """配置 brain.provider_retry.enabled=False → 单次瞬时错误即直接暂停（不重试）。"""
    import omni_core.brain.sdk_loop as sl
    import config as _cfg

    monkeypatch.setattr(
        _cfg, "get_config",
        lambda path, default=None: False if path == "brain.provider_retry.enabled" else default)
    monkeypatch.setattr(sl, "_PROVIDER_SLEEP", lambda *a, **k: None)

    req = httpx.Request("POST", "http://127.0.0.1:9")
    state = {"n": 0}

    def _fake_run_async(coro, timeout=None):
        state["n"] += 1
        raise APIConnectionError(message="conn", request=req)

    monkeypatch.setattr(sl, "run_async", _fake_run_async)
    brain = {"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}
    gate = types.SimpleNamespace(verify_done=lambda: (False, ""), verify_count=0)
    res = run_subtask_sdk(
        brain, instructions="do it", user_input="hi", tools=[], gate=gate, mcp_servers=None)
    assert state["n"] == 1, "禁用后不应重试"
    assert res["provider_error"] is True


# --- U3 扩展：空 choices（ModelBehaviorError）退避重试 -------------------------
def test_provider_retry_recovers_on_single_empty_choices(monkeypatch):
    """验收③：注入 1 次空 choices 响应 → 任务自动重试恢复、不暂停（provider_error=False）。"""
    def _behavior(n):
        if n == 1:
            return ModelBehaviorError("ChatCompletion response has no choices (possible provider error payload)")
        return None  # 模拟正常 RunResult

    res = _run_with_provider_async(monkeypatch, _behavior)
    assert res["_calls"] == 2, "应重试 1 次后恢复"
    assert res["provider_error"] is False
    assert res["success"] is True, "应自动恢复而非暂停"
    assert res["paused"] is False


def test_provider_retry_empty_choices_exhausts_then_pauses(monkeypatch):
    """验收④：连续空 choices 超 N 次 → 才暂停并保留进度（provider_error=True，绝静默 success）。"""
    def _behavior(n):
        return ModelBehaviorError("ChatCompletion response has no choices (possible provider error payload)")

    res = _run_with_provider_async(monkeypatch, _behavior)
    # 初始 1 次 + 默认 PROVIDER_RETRY_LIMIT=3 次退避重试 = 4 次模型调用后才暂停
    assert res["_calls"] == 1 + 3, f"重试耗尽才暂停，应调用 4 次: {res['_calls']}"
    assert res["provider_error"] is True
    assert res["escalated"] is True
    assert res["success"] is False
    # 耗尽后落友好文案（含 no choices），不再带「runner 异常」原生前缀
    assert "runner 异常" not in res["reason"]
    assert "no choices" in res["reason"]
    assert res["repeat_failures"] >= 3, f"重试计数应接通: {res['repeat_failures']}"


def test_run_subtask_sdk_431_sets_provider_error(monkeypatch):
    """431 请求头过大也必须降级——不再落 runner 异常硬终止。"""
    res = _run_with_provider_error(monkeypatch, _make_status_error(431))
    assert res["provider_error"] is True
    assert res["escalated"] is True
    assert "runner 异常" not in res["reason"]
    assert "431" in res["reason"]


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
    monkeypatch.setattr(sl, "_PROVIDER_SLEEP", lambda *a, **k: None)

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
