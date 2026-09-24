"""阶段 0.5（AgentRun 过渡层）验收测试。

三组：
1. 配置快照稳定性 —— ToolLoop 使用注入的 run 级 config_snapshot，运行期内不再重读全局 config。
2. 按 (task_id, agent_id) 停止/注入 —— 控制接口以复合键查 AgentRun，取代旧全局 _loop。
3. 跨 task 的 SSE / outbox 隔离 —— 不同 task 的事件/输出队列互不串流。
"""

import pytest


from tests._env import install_fake_env  # noqa: E402


class _FakeExec:
    """最小执行模块替身（满足环境契约 kind / text_of / verify_done）。"""

    kind = "host"

    def text_of(self, percept):
        return ""

    def verify_done(self, condition, percept):
        return (False, "noop")


class _FakeLoop:
    """最小运行体句柄替身。"""

    def __init__(self):
        self.stopped = False

    def request_stop(self):
        self.stopped = True

    def inject_message(self, text):
        return True


# ---------------------------------------------------------------------------
# 1. 配置快照稳定性
# ---------------------------------------------------------------------------
def test_config_snapshot_used_not_global(monkeypatch):
    import omni_core.local.loop.core as tl

    install_fake_env(monkeypatch, _FakeExec)

    sentinel = {"runtime": {"from_global": True}}
    monkeypatch.setattr(tl.config, "load_config", lambda: sentinel)

    # 注入快照：loop._cfg 必须是该快照本体（identity），不读全局
    snapshot = {"runtime": {"from_snapshot": True}}
    loop = tl.ToolLoop(
        brain_cfg={"model": "m", "base_url": "http://localhost:9", "api_key": "test"},
        config_snapshot=snapshot,
    )
    assert loop._cfg is snapshot
    assert loop.agent_id == "main"

    # 未注入：回退到全局 load_config 的结果（值相等）
    loop2 = tl.ToolLoop(
        brain_cfg={"model": "m", "base_url": "http://localhost:9", "api_key": "test"}
    )
    assert loop2._cfg == sentinel


# ---------------------------------------------------------------------------
# 2. 按 (task_id, agent_id) 停止 / 注入
# ---------------------------------------------------------------------------
def test_agent_run_keyed_lookup_and_single_task_mutex():
    import backend.api.router_runtime as RR

    tid_a = "t_agent_run_A"
    tid_b = "t_agent_run_B"
    try:
        assert RR._try_start_task(tid_a) is True
        # 单任务互斥：已有运行体在跑时，新 task 启动应被拒
        assert RR._try_start_task(tid_b) is False
        assert RR._is_any_running() is True
        assert RR._running_task_id() == tid_a
        assert RR._is_task_running(tid_a) is True

        # 以复合键 (task_id, agent_id) 注入并查回运行体句柄（阶段 1：RuntimeContext 属性访问）
        fake = _FakeLoop()
        ctx = RR.manager.get(tid_a, RR.AGENT_MAIN)
        ctx.loop = fake
        assert ctx.loop is fake

        # 模拟 /stop 的解析路径：按 (task_id, agent_id) 取 loop 并请求停止
        loop = ctx.loop
        loop.request_stop()
        assert fake.stopped is True

        RR._finish_task(tid_a)
        assert RR._is_task_running(tid_a) is False
    finally:
        RR.manager.pop(tid_a, RR.AGENT_MAIN)
        RR.manager.pop(tid_b, RR.AGENT_MAIN)


def test_agent_run_agent_id_default_main():
    import backend.api.router_runtime as RR

    tid = "t_agent_run_default"
    try:
        assert RR._try_start_task(tid) is True
        # 默认 agent_id 为 "main"，可用 (tid, "main") 直接查到
        assert RR.manager.get(tid, RR.AGENT_MAIN) is not None
        assert RR._is_task_running(tid) is True  # 默认 agent_id 解析
    finally:
        RR._finish_task(tid)
        RR.manager.pop(tid, RR.AGENT_MAIN)


# ---------------------------------------------------------------------------
# 3. 跨 task 的 SSE / outbox 隔离
# ---------------------------------------------------------------------------
def test_outbox_isolation_across_tasks():
    import backend.api.router_runtime as RR

    out_a = RR._ensure_outbox("task_A")
    out_b = RR._ensure_outbox("task_B")
    # 不同 task 必须拿到不同的队列容器
    assert out_a is not out_b
    assert "chat" in out_a and "chat" in out_b

    # 向 task_A 推事件，task_B 不应串流
    out_a["chat"].append("hello_a")
    assert "hello_a" not in list(out_b["chat"])
    assert len(list(out_b["chat"])) == 0


# ---------------------------------------------------------------------------
# 4. 阶段 1 验收：每个 run 可完整导出运行配置
# ---------------------------------------------------------------------------
def test_runtime_context_export_includes_run_config():
    """阶段 1 验收项：运行 context 能完整导出运行配置（config_hash/project_id/能力组）。"""
    import backend.api.router_runtime as RR

    tid = "t_export_run_config"
    try:
        assert RR._try_start_task(tid) is True
        ctx = RR.manager.get(tid, RR.AGENT_MAIN)
        ctx.config_hash = "abc123def456"
        ctx.project_id = "p_demo"

        exported = ctx.export()
        assert exported["config_hash"] == "abc123def456"
        assert exported["project_id"] == "p_demo"

        # 阶段 1 目标 3：工具 registry 与设备后端也随运行体导出（完整运行配置）
        ctx.tool_registry = {"click": 1, "shell_exec": 1}
        ctx.execution_backend = type("B", (), {"kind": "host"})()
        exported2 = ctx.export()
        assert exported2["tool_names"] == ["click", "shell_exec"]
        assert exported2["execution_backend"] == "host"

        # 运行表也能按复合键导出同一份完整配置
        assert RR.manager.get(tid, RR.AGENT_MAIN).export()["config_hash"] == "abc123def456"
    finally:
        RR.manager.pop(tid, RR.AGENT_MAIN)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
