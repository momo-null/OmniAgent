"""pytest 共享夹具

把仓库根加入 sys.path，并用 FastAPI TestClient + dependency_overrides
注入 mock 服务，从而在不拉起真实模型 / agent 的前提下，
对重组后的 REST 层做端到端验证。

P3.1: 统一 _isolated_home fixture（monkeypatch runtime_paths._GLOBAL
到 tmp_path/.omniagent），避免各测试文件重复定义。
"""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import backend.server as server_mod
from backend.api import deps
from omni_core.local import runtime_paths as P

# 真实用户配置（护栏比对用；测试绝不允许改动它们）
_REAL_SETTINGS = os.path.join(os.path.expanduser("~"), ".omniagent", "config.yaml")
_REAL_MCP = os.path.join(os.path.expanduser("~"), ".omniagent", "mcp.json")


def _clear_cfg_cache(_cfg) -> None:
    """清空 config 模块级缓存（含 MCP 缓存），使每个测试读到隔离后的配置。"""
    _cfg._config_cache = {}
    _cfg._settings_cache = {}
    _cfg._mcp_cache = {}


def _fingerprint(path: str):
    """文件指纹（不存在返回 None）。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


@pytest.fixture(scope="session", autouse=True)
def _guard_real_user_config():
    """会话级护栏：跑测试期间真实 ~/.omniagent 下的 config.yaml / mcp.json 不许被改动。

    2026-09-23 事故：``test_api_integration`` 的 ``PUT /api/settings`` 直接写进了**真实**
    ``~/.omniagent/config.yaml``（把用户配置覆写成夹具值 ``very-secret`` / ``gpt-x``），
    ``mcp.json`` 同理会丢。此护栏让同类回归在测试结束时立刻显式失败，而不是静默丢数据。
    """
    targets = (_REAL_SETTINGS, _REAL_MCP)
    before = {p: _fingerprint(p) for p in targets}
    yield
    changed = [p for p in targets if _fingerprint(p) != before[p]]
    assert not changed, (
        "测试改动了真实用户配置（测试必须只写 tmp_path）：" + ", ".join(changed)
    )


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """把全局根 ~/.omniagent 重定向到 tmp_path/.omniagent，避免污染真实用户数据。

    ⚠️ 必须同时重定向 ``config`` 模块的**模块级路径常量**（``SETTINGS_PATH`` /
    ``MCP_CONFIG_PATH``）：它们在 import 时就由 ``os.path.expanduser("~")`` 固化为真实
    路径，只 monkeypatch ``runtime_paths._GLOBAL`` 是不够的——否则 ``save_settings()`` /
    ``save_mcp_config()`` 依旧写进真实 ``~/.omniagent/``（2026-09-23 实测事故，见
    上面 ``_guard_real_user_config``）。任何直接调用 config 写接口的测试都依赖这里。
    """
    fake = tmp_path / ".omniagent"
    monkeypatch.setattr(P, "_GLOBAL", fake)
    import config as _cfg
    monkeypatch.setattr(_cfg, "SETTINGS_PATH", str(fake / "config.yaml"))
    monkeypatch.setattr(_cfg, "MCP_CONFIG_PATH", str(fake / "mcp.json"))
    _clear_cfg_cache(_cfg)
    P.ensure_global_dirs()
    yield fake
    _clear_cfg_cache(_cfg)


#: 环境包（各自 `tools.py` 有一个模块级后端全局）
_ENV_KINDS = ("host", "emulator")


@pytest.fixture(autouse=True)
def _unbind_env_tool_backends():
    """用例间**解绑**环境工具后端，消除顺序依赖。

    `environments/<kind>/tools.py` 的后端是**模块级全局**（`bind()` 注入；工具跑在
    AsyncBridge 的后台线程上，故不能用 ContextVar——见 MEMORY）。若 A 用例绑了真实
    后端、B 用例只 patch 了内核句柄，B 的工具调用就会打到 A 的后端（真实屏幕 / 失效 fake）。

    只做解绑（**不碰 `TOOL_REGISTRY`**，风险最低）：用例自己 `_bind(...)` 或
    `install_fake_env(...)`；没绑就用 → 立即报「未绑定后端」，而不是静默用别人的。
    """
    yield
    for kind in _ENV_KINDS:
        mod = sys.modules.get(f"environments.{kind}.tools")
        if mod is not None:
            bind = getattr(mod, "bind", None)
            if callable(bind):
                bind(None)


@pytest.fixture
def enable_plugin():
    """打开一个插件：写它的自有配置 ``~/.omniagent/plugins/<name>.yaml`` 的 ``enabled``。

    插件默认开关写在各自 ``plugin.json``（`vision` 默认 ``false``，治"每次都用 vision"）；
    测试要用它的工具时，先显式打开（全局根已由 ``_isolated_home`` 隔离）。
    """

    def _enable(name: str) -> None:
        import yaml

        from omni_core.local import runtime_paths as _P

        path = _P.plugin_config_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["enabled"] = True
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    return _enable


@pytest.fixture
def mock_model_hub():
    """ModelManager 替身：覆盖 router_models 依赖的所有方法（扫描式，无注册表）

    model-meta-refactor 后不再有 models_cfg 属性；model_status 端点依赖
    ``scan_and_build_models``（此处返回含 m1 的列表），故一并模拟。
    """
    m = MagicMock()
    m.list_models.return_value = [{"name": "m1", "status": "stopped"}]
    m.get_active_models.return_value = []
    m.scan_and_build_models.return_value = [{"name": "m1", "port": 8085}]
    m.start_model.return_value = {"name": "m1", "status": "ready"}
    m.start_model_path.return_value = {"name": "x", "status": "ready", "port": 8085}
    m.stop_model.return_value = {"name": "m1", "status": "stopped"}
    m.get_model_logs.return_value = {"out": "log line", "err": ""}
    m.scan_models_dir.return_value = []
    m.stop_all.return_value = []
    m.save_model_meta.return_value = {"name": "m1", "status": "saved"}
    return m


@pytest.fixture
def mock_training():
    """TrainingService 替身：覆盖 router_training 依赖的所有方法"""
    m = MagicMock()
    m.run.return_value = {"ok": True, "step": "all", "status": "started"}
    m.get_status.return_value = {"running": False, "step": "idle"}
    m.get_logs.return_value = "train log"
    return m


@pytest.fixture
def client(mock_model_hub):
    """带依赖覆盖的 TestClient"""
    app = server_mod.app
    app.dependency_overrides[deps.get_model_hub] = lambda: mock_model_hub
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
