"""工具管理接口：/tools（只读枚举）+ 环境单选 + 插件开关。

新模型：
- **环境**（`environments/<kind>/`）：单选（`runtime.backend`），自带工具面 → 前端 radio；
- **插件**（`plugins/<name>/`）：每个一个 on/off（`~/.omniagent/plugins/<name>.yaml`）→ 前端 switch；
- **工具**：只读清单（来源 env / plugin / core / mcp），无逐工具开关。

前端不硬编码任何环境 / 插件名：所有标题 / 说明 / 状态都来自后端自报。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.routers.helpers import config

router = APIRouter(tags=["runtime"])


@router.get("/tools")
async def list_tools():
    """枚举：环境（radio）+ 插件（switch）+ 工具（只读）+ MCP。"""
    try:
        import config as app_config

        from devices import list_environments, registered_kinds
        from omni_core.tools import activate_environment, configure_local_model_from_config
        from omni_core.tools.base import TOOL_REGISTRY
        from omni_core.tools.env_loader import active_kind
        from omni_core.tools.loader import PluginContext, list_plugins, load_plugins

        cfg = app_config.load_config() or {}
        # M9：本地模型工具是配置驱动的动态注册，枚举前先按配置登记一次
        configure_local_model_from_config(cfg)
        env_kind = active_kind(cfg)
        # 激活当前环境以登记其工具（只读枚举路径，与运行一致）
        try:
            activate_environment(cfg)
        except Exception:
            pass
        # 幂等装载插件（只读枚举：wired=False，插件不得做破坏性动作）
        load_plugins(cfg, ctx=PluginContext(config=cfg, wired=False, env_kind=env_kind))

        tools = []
        for p in TOOL_REGISTRY.values():
            fn = (p.schema or {}).get("function") or {}
            tools.append({
                "name": p.name,
                "description": fn.get("description", "") or "",
                "source": p.source,          # core | env | plugin | mcp
                "unit": p.unit,              # 提供者标识
                "server": p.meta.get("server") if p.source == "mcp" else None,
                "parameters": fn.get("parameters") or {},
            })
        tools.sort(key=lambda t: (t["source"], t["name"]))

        environments = [
            {"kind": e["kind"], "title": e["title"], "active": e["kind"] == env_kind}
            for e in list_environments()
        ]
        plugins = list_plugins(cfg, env_kind=env_kind)

        mcp_cfg = config.load_mcp_config()
        servers = []
        for s in mcp_cfg.get("servers") or []:
            if not isinstance(s, dict):
                continue
            servers.append({
                "name": s.get("name", ""),
                "enabled": bool(s.get("enabled", True)),
                "command": s.get("command", ""),
                "args": list(s.get("args") or []),
                "url": s.get("url", ""),
                # env 只回传键名，不回传值（防密钥泄露）
                "env_keys": sorted((s.get("env") or {}).keys()),
            })

        return JSONResponse({
            "environments": environments,
            "plugins": plugins,
            "tools": tools,
            "mcp": {
                "enabled": bool(mcp_cfg.get("enabled", False)),
                "servers": servers,
                "connected": sorted({
                    t["server"] for t in tools if t["source"] == "mcp" and t["server"]
                }),
            },
        })
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"工具枚举失败: {type(e).__name__}: {e}"}, status_code=500
        )


@router.patch("/tools/environment")
async def set_environment(request: Request):
    """切换当前环境（写 ``runtime.backend``）；重启生效。

    - body: ``{"kind": "host"}``（必须是已注册的环境）。
    """
    import config as app_config
    from devices import registered_kinds

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    kind = (body or {}).get("kind")
    if not isinstance(kind, str) or kind not in registered_kinds():
        return JSONResponse(
            {"ok": False, "error": f"未知环境: {kind!r}（可选 {registered_kinds()}）"},
            status_code=400,
        )
    try:
        existing = app_config.load_settings() or {}
        rt = existing.get("runtime") or {}
        rt["backend"] = kind
        existing["runtime"] = rt
        app_config.save_settings(existing)
        app_config.reload_config()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"持久化失败: {type(e).__name__}: {e}"}, status_code=500
        )
    return JSONResponse({"ok": True, "environment": kind})


@router.patch("/tools/plugins")
async def set_plugin_enabled(request: Request):
    """开关一个插件（写 ``~/.omniagent/plugins/<name>.yaml`` 的 ``enabled``）；重启生效。

    - body: ``{"name": "vision", "enabled": true}``。
    """
    import config as app_config
    from omni_core.local.runtime_paths import plugin_config_file
    from omni_core.tools.loader import list_plugins, plugin_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    name = (body or {}).get("name")
    enabled = (body or {}).get("enabled")
    if not isinstance(name, str) or not isinstance(enabled, bool):
        return JSONResponse(
            {"ok": False, "error": "需要 {name: str, enabled: bool}"}, status_code=400
        )
    cfg = app_config.load_config() or {}
    known = {p["name"] for p in list_plugins(cfg)}
    if name not in known:
        return JSONResponse({"ok": False, "error": f"未知插件: {name}"}, status_code=400)
    try:
        import yaml
        data = plugin_config(name) or {}
        data["enabled"] = enabled
        path = plugin_config_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"写入插件配置失败: {type(e).__name__}: {e}"}, status_code=500
        )
    return JSONResponse({"ok": True, "name": name, "enabled": enabled})
