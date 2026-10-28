"""U2 能力门禁单测：**环境单选 + 插件开关**（无 per-tool 禁用）。

设计：
- 环境（`environments/<kind>/`）**单选**，写 `runtime.backend`（前端 radio）；
- 插件（`plugins/<name>/`）**各自一个 `enabled` 开关**，写 `~/.omniagent/plugins/<name>.yaml`
  （前端 switch；默认值在 `plugin.json`）；
- 工具清单**只读**——没有逐工具开关（旧 `runtime.tools.disabled` / `groups` 已删）。

不依赖 GPU / 模型 / 模拟器；测试用隔离的全局数据根。
"""


def test_tools_endpoint_exposes_plugins_and_tools(client):
    g = client.get("/api/runtime/tools").json()
    assert {"plugins", "tools", "mcp"} <= set(g)
    # env 概念已退场：没有环境单选，也没有 env 来源的工具
    assert "environments" not in g
    assert all(t["source"] != "env" for t in g["tools"])
    # 插件自报标题与状态（前端零硬编码）
    assert all("enabled" in p and p.get("title") for p in g["plugins"])
    # 工具只读：无 per-tool 禁用字段
    assert all("disabled" not in t for t in g["tools"])


def test_environment_patch_endpoint_removed(client):
    """env 单选端点已随 env 概念删除（404）。"""
    r = client.patch("/api/runtime/tools/environment", json={"kind": "host"})
    assert r.status_code in (404, 405)


def test_plugin_patch_validates_name(client):
    r = client.patch("/api/runtime/tools/plugins", json={"name": "no_such_plugin", "enabled": True})
    assert r.status_code == 400
    assert "未知插件" in r.json()["error"]


def test_plugin_patch_writes_own_config_not_core_config(client):
    """开关落插件自有文件；core config 里不留任何插件键。"""
    from omni_core.local.runtime_paths import plugin_config_file
    from omni_core.tools.loader import plugin_config

    plugins = client.get("/api/runtime/tools").json()["plugins"]
    assert plugins, "至少应有一个插件（filesystem / web / …）"
    name = plugins[0]["name"]
    target = not bool(plugins[0]["enabled"])

    r = client.patch("/api/runtime/tools/plugins", json={"name": name, "enabled": target})
    assert r.status_code == 200, r.text
    assert plugin_config(name).get("enabled") is target
    assert plugin_config_file(name).is_file()

    # 复原
    client.patch("/api/runtime/tools/plugins", json={"name": name, "enabled": not target})


def test_plugin_enabled_flag_reflected_in_endpoint(client):
    from omni_core.tools.loader import plugin_config

    plugins = {p["name"]: p for p in client.get("/api/runtime/tools").json()["plugins"]}
    name = sorted(plugins)[0]
    target = not bool(plugins[name]["enabled"])
    client.patch("/api/runtime/tools/plugins", json={"name": name, "enabled": target})
    after = {p["name"]: p for p in client.get("/api/runtime/tools").json()["plugins"]}
    assert after[name]["enabled"] is target
    client.patch("/api/runtime/tools/plugins", json={"name": name, "enabled": not target})
    assert plugin_config(name) is not None


def test_no_legacy_per_tool_disable_or_groups():
    """零兼容：旧 `run_python` / 逐工具禁用 / 分组键都不再存在。"""
    from omni_core.tools.base import TOOL_REGISTRY, schemas

    assert "run_python" not in TOOL_REGISTRY
    assert all(s["function"]["name"] != "run_python" for s in schemas())

    import config as app_config

    runtime_keys = set((app_config.CONTEXT_DEFAULTS.get("runtime") or {}).keys())
    assert "python_exec" not in runtime_keys
    assert "tools" not in runtime_keys
