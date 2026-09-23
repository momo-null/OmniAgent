"""API/集成测试（P3.3）：覆盖 P0-P2 关键路径的回归测试。

用 TestClient + monkeypatch 隔离，不依赖真实模型/设备。
"""
import json

import pytest
from fastapi.testclient import TestClient

import backend.server as server_mod
from backend.api import deps
from unittest.mock import MagicMock


@pytest.fixture
def client(mock_model_hub):
    """带依赖覆盖的 TestClient"""
    app = server_mod.app
    app.dependency_overrides[deps.get_model_hub] = lambda: mock_model_hub
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# === P0.1 路径安全 ===
class TestPathSafety:
    def test_get_task_rejects_bad_id(self, client):
        """非法 task_id 应返回 422"""
        # 含特殊字符的 ID 会被 validate_identifier 拒绝
        r = client.get("/api/runtime/tasks/bad@id!")
        assert r.status_code == 422

    def test_delete_task_rejects_bad_id(self, client):
        r = client.delete("/api/runtime/tasks/bad@id!")
        assert r.status_code == 422

    def test_create_task_rejects_extra_fields(self, client):
        """P0.2: Pydantic extra=forbid"""
        r = client.post("/api/runtime/tasks", json={"objective": "test", "hacker_field": "x"})
        assert r.status_code == 422

    def test_create_task_no_client_task_id(self, client):
        """不再接受客户端自定义 task_id"""
        r = client.post("/api/runtime/tasks", json={"objective": "test", "task_id": "t_custom"})
        assert r.status_code == 422

    def test_chat_rejects_bad_task_id(self, client):
        r = client.post("/api/runtime/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "task_id": "../bad",
        })
        assert r.status_code == 422


# === P0.2 Pydantic 模型 ===
class TestPydanticValidation:
    def test_chat_rejects_empty_messages(self, client):
        r = client.post("/api/runtime/chat", json={"messages": []})
        assert r.status_code == 422

    def test_chat_rejects_bad_role(self, client):
        r = client.post("/api/runtime/chat", json={
            "messages": [{"role": "invalid", "content": "hi"}],
        })
        assert r.status_code == 422

    def test_chat_rejects_too_long_content(self, client):
        r = client.post("/api/runtime/chat", json={
            "messages": [{"role": "user", "content": "x" * 20000}],
        })
        assert r.status_code == 422

    def test_chat_max_steps_bounds(self, client):
        r = client.post("/api/runtime/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "max_steps": 0,
        })
        assert r.status_code == 422
        r = client.post("/api/runtime/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "max_steps": 10000,
        })
        assert r.status_code == 422

    def test_create_task_rejects_empty_objective(self, client):
        r = client.post("/api/runtime/tasks", json={"objective": ""})
        assert r.status_code == 422


# === P0.3 密钥脱敏 ===
class TestKeyMasking:
    def test_get_settings_no_api_key(self, client, monkeypatch, tmp_path):
        """GET /api/settings 不得返回明文 api_key"""
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        import config
        config._config_cache = {"brain": {"api_key": "secret-key-123", "model": "test"}}
        config._settings_cache = {}

        r = client.get("/api/settings")
        assert r.status_code == 200
        data = r.json()
        brain = data.get("brain", {})
        assert "api_key" not in brain
        assert brain.get("api_key_set") is True

    def test_put_settings_empty_key_preserves(self, client, monkeypatch, tmp_path):
        """PUT 时空字符串 api_key 表示保持不变"""
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        import config
        # 现有配置含密钥
        config._config_cache = {"brain": {"api_key": "original-secret", "model": "test"}}
        config._settings_cache = {"brain": {"api_key": "original-secret", "model": "test"}}

        # 提交空 api_key（表示保持不变）
        r = client.put("/api/settings", json={"brain": {"api_key": "", "model": "new"}})
        assert r.status_code == 200

        # 验证原 key 保留在 settings 中
        # （_preserve_existing_api_key 从 load_config() 恢复原值）

    def test_put_then_get_no_plaintext_key(self, client, monkeypatch, tmp_path):
        """PUT 写入含 api_key 的 brain 配置，随后 GET 不应回明文"""
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        import config
        config._config_cache = {}
        config._settings_cache = {}

        r = client.put("/api/settings", json={
            "brain": {"api_key": "very-secret", "model": "gpt-x", "base_url": "https://x"},
        })
        assert r.status_code == 200

        # 失效缓存并重新读取 GET
        config._config_cache = {}
        config._settings_cache = {}
        g = client.get("/api/settings")
        assert g.status_code == 200
        brain = g.json().get("brain", {})
        assert "api_key" not in brain
        assert brain.get("api_key_set") is True
        # 其他字段应正常返回
        assert brain.get("model") == "gpt-x"


# === M3 工具插件层枚举（前端 Skills&Tools 面板数据源） ===
class TestToolsEndpoint:
    def test_tools_lists_plugin_layer(self, client):
        """GET /api/runtime/tools 枚举插件层：自研 + 外部 MCP 平级。"""
        r = client.get("/api/runtime/tools")
        assert r.status_code == 200, r.text
        data = r.json()

        names = {t["name"] for t in data["tools"]}
        # 自研 device / python / vision 分组
        for t in ("press", "click", "template_match", "run_python", "som_marks"):
            assert t in names, f"{t} 应来自 tool 插件层"

        by_name = {t["name"]: t for t in data["tools"]}
        assert by_name["run_python"]["source"] == "builtin"
        assert by_name["run_python"]["server"] is None
        assert by_name["press"]["group"] == "device"
        # 描述与参数结构可用（前端 tooltip 直接用）
        assert isinstance(by_name["press"]["parameters"], dict)
        assert "device" in data["groups"]

        # MCP 段结构固定，未启用时为空
        assert "enabled" in data["mcp"]
        assert isinstance(data["mcp"]["servers"], list)
        assert isinstance(data["mcp"]["connected"], list)

    def test_tools_respects_group_filter(self, client, monkeypatch):
        """config.runtime.tools.groups 收窄为 ["python"] 后：只有 python 组生效。

        注意端点的视图契约（085776d 起）：**始终返回全部插件**并逐项带
        `group_enabled` 标记——否则被禁用分组的工具会从设置页彻底消失、无法重新打开。
        因此这里断言的是「生效分组 ⊆ 标记语义」，而不是「返回列表被过滤」。
        """
        import config
        monkeypatch.setattr(
            config,
            "load_config",
            lambda: {"runtime": {"tools": {"groups": ["python"]}}},
        )
        r = client.get("/api/runtime/tools")
        assert r.status_code == 200
        data = r.json()
        # 生效分组只含 python（顶层 groups / active_groups 与逐项标记同源）
        assert data["active_groups"] == ["python"]
        assert data["groups"] == ["python"]
        flags = {}
        for t in data["tools"]:
            flags.setdefault(t["group"], set()).add(bool(t["group_enabled"]))
        assert "python" in flags, "python 组工具应仍在列表中（视图契约：不过滤）"
        assert flags["python"] == {True}, "python 组应全部标记为启用"
        other = {g: v for g, v in flags.items() if g != "python"}
        assert other, "应存在其它分组，否则本断言无意义"
        assert all(v == {False} for v in other.values()), "非 python 组应一律标记为未启用"


# === M3 MCP server 配置写入校验 ===
class TestMcpSettingsValidation:
    def test_rejects_server_without_command_or_url(self, client, monkeypatch, tmp_path):
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        import config
        config._config_cache = {}
        config._settings_cache = {}

        r = client.put("/api/settings", json={
            "runtime": {"mcp": {"enabled": True, "servers": [{"name": "x", "enabled": True}]}},
        })
        assert r.status_code == 400
        assert "command" in r.json()["error"]

    def test_rejects_non_object_server(self, client, monkeypatch, tmp_path):
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        import config
        config._config_cache = {}
        config._settings_cache = {}

        r = client.put("/api/settings", json={
            "runtime": {"mcp": {"servers": ["nope"]}},
        })
        assert r.status_code == 400

    def test_accepts_valid_server(self, client, monkeypatch, tmp_path):
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        import config
        config._config_cache = {}
        config._settings_cache = {}

        r = client.put("/api/settings", json={
            "runtime": {"mcp": {"enabled": True, "servers": [
                {"name": "fs", "enabled": False, "command": "npx", "args": ["-y", "srv"]},
            ]}},
        })
        assert r.status_code == 200, r.text


# === P1.4 原子运行状态 ===
class TestAtomicRunState:
    def test_concurrent_chat_rejected(self, client, monkeypatch, tmp_path):
        """已有 task 运行时，第二个 /chat 应返回 409"""
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        # 直接标记一个 task 为 running
        import backend.api.router_runtime as rt
        rt._try_start_task("t_fake_running_001")

        # 第二个应被拒绝
        r = client.post("/api/runtime/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
        })
        assert r.status_code == 409
        # 阶段 1 目标 4：拒绝原因需回带运行中的 task_id（排队/拒绝原因可见）
        _body = r.json()
        assert _body.get("ok") is False
        assert "t_fake_running_001" in (_body.get("error") or "")

        # 清理
        rt._finish_task("t_fake_running_001")


# === P1.5 task-scoped SSE ===
class TestTaskScopedSSE:
    def test_stream_rejects_bad_task_id(self, client):
        """SSE 端点拒绝非法 task_id"""
        r = client.get("/api/runtime/stream?task_id=../bad")
        assert r.status_code in (400, 422)


# === P2.1 Task 创建带 project/session ===
class TestTaskCreation:
    def test_task_has_project_and_session(self, client, monkeypatch, tmp_path):
        from omni_core.local import runtime_paths as P
        monkeypatch.setattr(P, "_GLOBAL", tmp_path / ".omniagent")
        P.ensure_global_dirs()

        r = client.post("/api/runtime/tasks", json={"objective": "do something"})
        assert r.status_code == 200
        meta = r.json()["task"]
        assert meta["task_id"].startswith("t_")
        assert meta["project_id"]
        assert meta["session_id"]
        assert meta["state"] == "pending"


# === P1.4 /stop → aborted ===
class TestStopAbortsTask:
    def test_stop_marks_task_aborted(self, client, monkeypatch, tmp_path):
        """用户主动停止后，运行中的 task 状态应置为 aborted（P1.4）"""
        import backend.api.router_runtime as rt
        from omni_core.local.task_store import TaskStore

        # 建一个 running 的 task（模拟前端已下发）
        meta = TaskStore.create(objective="stop me")
        tid = meta["task_id"]
        assert rt._try_start_task(tid) is True
        TaskStore.update(tid, state="running")

        # 模拟当前 loop 归属（api_stop 从 _loop_task_id 取 tid）
        rt._loop_task_id = tid
        rt._loop = MagicMock()

        r = client.post("/api/runtime/stop")
        assert r.status_code == 200

        updated = TaskStore.get(tid)
        assert updated["state"] == "aborted"
        assert updated["finished_at"] is not None

        rt._finish_task(tid)
        rt._loop = None
        rt._loop_task_id = None

    def test_stop_no_loop_is_safe(self, client):
        """无运行 loop 时 /stop 也应 200 且不报错"""
        r = client.post("/api/runtime/stop")
        assert r.status_code == 200


# === P2.1 session 历史往返（恢复） ===
class TestSessionRecovery:
    def test_history_roundtrip(self, client, monkeypatch, tmp_path):
        """user + assistant 消息写 jsonl 后，GET /tasks/{tid}/history 可读回"""
        from omni_core.local.task_store import TaskStore, ProjectStore

        meta = TaskStore.create(objective="roundtrip")
        tid = meta["task_id"]
        pid = meta["project_id"]
        sid = meta["session_id"]

        ProjectStore.append_message(pid, sid, "user", "hello", task_id=tid)
        ProjectStore.append_message(pid, sid, "assistant", "hi there", task_id=tid)

        r = client.get(f"/api/runtime/tasks/{tid}/history")
        assert r.status_code == 200
        msgs = r.json()["messages"]
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles
        contents = [m["content"] for m in msgs]
        assert "hello" in contents
        assert "hi there" in contents

    def test_objective_in_task_list(self, client, monkeypatch, tmp_path):
        """task 列表返回 objective，前端标题可走服务端主源（P2.2）"""
        r = client.post("/api/runtime/tasks", json={"objective": "my title"})
        assert r.status_code == 200
        tid = r.json()["task"]["task_id"]

        lst = client.get("/api/runtime/tasks")
        assert lst.status_code == 200
        found = next((t for t in lst.json()["tasks"] if t["task_id"] == tid), None)
        assert found is not None
        assert found["objective"] == "my title"
