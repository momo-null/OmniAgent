"""T4.2（O1 配套）— MCP 超时与运行间重连。

覆盖：
1. 间歇性连接失败可自动重试恢复（指数退避：0.5s → 1s → …）。
2. 连续 10 次失败自动放弃当前服务，且不影响其他 MCP 服务。
3. 探活成功后计数清零，后续异常仍可正常重试。
4. 超时透传：连接/探活带上 SDK 原生 client_session_timeout_seconds（且被总预算封顶）。
5. 总预算：不可达 server 最坏只等 _MCP_CONNECT_BUDGET_SEC，不挡首次大脑调用。
"""
import pytest

from omni_core.brain import sdk_loop as sl
from omni_core.tools.mcp_servers import DEFAULT_TIMEOUT_MS, _timeout_seconds


class _FakeServer:
    """MCP server 替身：按 plan 决定每次 connect 是否失败。"""

    def __init__(self, name, plan=None, timeout_s=None, fail_list=False):
        self.name = name
        self.session = None
        self.client_session_timeout_seconds = timeout_s
        self.plan = list(plan or [])
        self.fail_list = fail_list
        self.connect_calls = 0
        self.list_calls = 0

    async def connect(self):
        self.connect_calls += 1
        fail = self.plan.pop(0) if self.plan else False
        if fail:
            raise RuntimeError("connect boom")
        self.session = object()

    async def list_tools(self):
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError("list_tools boom")
        return []


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """每例重置重连状态，并用假 sleep 免真等。

    本节多数用例验证的是**退避/放弃语义本身**（0.5→30s、满 10 次放弃），
    故把总预算设为 0=不限——否则 8s 预算会提前掐断重试，语义不可达。
    预算本身的行为由 test_dead_server_gives_up_within_budget /
    test_attempt_timeout_capped_by_budget 单独覆盖。
    """
    sl._MCP_FAIL_STREAK.clear()
    sl._MCP_ABANDONED.clear()
    monkeypatch.setattr(sl, "_MCP_CONNECT_BUDGET_SEC", 0)
    sleeps = []
    monkeypatch.setattr(sl, "_MCP_SLEEP", lambda d: sleeps.append(d))
    return sleeps


# --- 1. 间歇性失败 -> 自动重试恢复 --------------------------------------------
def test_intermittent_failure_recovers():
    srv = _FakeServer("fs", plan=[True, True, False])
    got = sl._ensure_mcp_connected([srv])
    assert got == [srv], "间歇性失败应重试后恢复"
    assert srv.connect_calls == 3
    assert sl._MCP_FAIL_STREAK.get("fs") is None, "成功后计数应清零"


def test_backoff_delays_are_exponential(monkeypatch):
    delays = []
    monkeypatch.setattr(sl, "_MCP_SLEEP", delays.append)
    srv = _FakeServer("fs", plan=[True, True, True, False])
    got = sl._ensure_mcp_connected([srv])
    assert got == [srv]
    assert delays == [0.5, 1.0, 2.0], f"退避应为 0.5s 起指数增长: {delays}"


def test_backoff_caps_at_30s(monkeypatch):
    delays = []
    monkeypatch.setattr(sl, "_MCP_SLEEP", delays.append)
    # 连续失败 8 次成功 -> 应看到退避封顶 30s
    srv = _FakeServer("fs", plan=[True] * 8 + [False])
    got = sl._ensure_mcp_connected([srv])
    assert got == [srv]
    assert delays[-1] == 30.0, f"退避上限应为 30s: {delays}"


# --- 2. 连续 10 次失败 -> 放弃该服务，不影响其他服务 ----------------------------
def test_ten_consecutive_failures_abandon_server(monkeypatch):
    delays = []
    monkeypatch.setattr(sl, "_MCP_SLEEP", delays.append)
    bad = _FakeServer("bad", plan=[True] * 20)
    good = _FakeServer("good", plan=[])
    got = sl._ensure_mcp_connected([bad, good])

    assert got == [good], "坏服务应被放弃，好服务不受影响"
    assert bad.connect_calls == 10, "连续失败达上限即停止重试"
    assert sl._MCP_ABANDONED.get("bad") is True
    assert len(delays) == 9, "第 10 次失败后不再退避（直接放弃）"

    # 后续运行直接跳过已放弃的服务
    got2 = sl._ensure_mcp_connected([bad, good])
    assert got2 == [good]
    assert bad.connect_calls == 10, "已放弃的服务不应再尝试连接"


def test_list_tools_failure_also_retried(monkeypatch):
    monkeypatch.setattr(sl, "_MCP_SLEEP", lambda d: None)
    srv = _FakeServer("half", plan=[], timeout_s=None, fail_list=True)
    got = sl._ensure_mcp_connected([srv])
    assert got == [], "探活（list_tools）失败同样视为不可用"
    assert sl._MCP_FAIL_STREAK.get("half") == 10


# --- 3. 成功后计数清零，后续异常可正常重试 -------------------------------------
def test_success_resets_counter_for_later_retries(monkeypatch):
    monkeypatch.setattr(sl, "_MCP_SLEEP", lambda d: None)
    srv = _FakeServer("fs", plan=[True, True, False])
    assert sl._ensure_mcp_connected([srv]) == [srv]
    # 若计数未清零，再失败 8 次就会累计到 10 被放弃；清零后仍可完整重试
    srv.plan = [True] * 8 + [False]
    got = sl._ensure_mcp_connected([srv])
    assert got == [srv], "计数清零后，后续异常应能正常重试恢复"
    assert sl._MCP_ABANDONED.get("fs") is None


# --- 4. 超时透传（且在总预算内）------------------------------------------------
def test_timeout_passed_to_connect_and_probe(monkeypatch):
    seen = []

    def fake_run_async(coro, timeout=None):
        seen.append(timeout)
        try:
            coro.close()
        except Exception:
            pass
        return []

    monkeypatch.setattr(sl, "run_async", fake_run_async)
    # 配置超时（2s）小于总预算（8s）→ 原样透传
    srv = _FakeServer("fs", timeout_s=2.0)
    sl._ensure_mcp_connected([srv])
    assert seen == [2.0, 2.0], f"连接与探活都应带上超时: {seen}"


def test_attempt_timeout_capped_by_budget(monkeypatch):
    """配置超时再大也不能击穿总预算：单次超时被压到剩余预算内。"""
    monkeypatch.setattr(sl, "_MCP_CONNECT_BUDGET_SEC", 8.0)  # 覆盖 fixture 的"不限"
    seen = []

    def fake_run_async(coro, timeout=None):
        seen.append(timeout)
        try:
            coro.close()
        except Exception:
            pass
        return []

    monkeypatch.setattr(sl, "run_async", fake_run_async)
    srv = _FakeServer("fs", timeout_s=999.0)
    sl._ensure_mcp_connected([srv])
    assert seen, "应至少尝试一次"
    assert all(t <= sl._MCP_CONNECT_BUDGET_SEC for t in seen), \
        f"单次超时必须 ≤ 总预算: {seen}"


# --- 5. 总预算：不可达 server 不得挡住「首次大脑调用」---------------------------
def test_dead_server_gives_up_within_budget(monkeypatch):
    """回归：曾有不可达 server 让 10 次退避累计空等 ~143s 且全过程零输出
    （用户表现为「发消息后什么都不返回」）。现在最坏只等总预算。"""
    import time as _t
    srv = _FakeServer("dead", plan=[True] * 100)      # 永远连不上
    monkeypatch.setattr(sl, "_MCP_SLEEP", _t.sleep)   # 用真 sleep 实测耗时
    monkeypatch.setattr(sl, "_MCP_CONNECT_BUDGET_SEC", 0.6)
    t0 = _t.monotonic()
    got = sl._ensure_mcp_connected([srv])
    elapsed = _t.monotonic() - t0
    assert got == [], "不可达 server 不应进入 agent"
    assert elapsed <= 1.5, f"应在预算 0.6s 附近放弃，实测 {elapsed:.2f}s"
    assert srv.connect_calls < 10, "不应跑满 10 次重试"


def test_timeout_ms_config_parsing():
    assert _timeout_seconds({}) == DEFAULT_TIMEOUT_MS / 1000.0
    assert _timeout_seconds({"timeout_ms": 5000}) == 5.0
    # 非法值回退默认，不抛异常
    assert _timeout_seconds({"timeout_ms": "abc"}) == DEFAULT_TIMEOUT_MS / 1000.0
    assert _timeout_seconds({"timeout_ms": 0}) == DEFAULT_TIMEOUT_MS / 1000.0
