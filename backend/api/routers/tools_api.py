"""工具管理接口：/tools 与 PATCH /tools/disabled（零逻辑改动）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.routers.helpers import (
    TOOL_REGISTRY,
    config,
    copy,
)

router = APIRouter(tags=["runtime"])

@router.get("/tools")
async def list_tools():
    """agent 外层 tool 插件层当前暴露的工具（只读枚举）。

    M3 起能力=插件：自研工具（device / vision / python）与外部 MCP 工具进同一张
    注册表、同一条派发路径，完全平级，这里用 `source` 区分来源（builtin / mcp）。
    启停由 config.runtime.tools.groups 过滤（不配置 = 全部启用）。
    """
    try:
        import config as app_config
        from omni_core.tools import (
            build_plugin_registry,
            configure_local_model_from_config,
        )
        from omni_core.tools.base import TOOL_REGISTRY

        cfg = app_config.load_config() or {}
        rt = cfg.get("runtime") or {}
        # M9：本地模型工具是配置驱动的动态插件，枚举前先按配置登记一次
        configure_local_model_from_config(cfg)
        # 与 tool_loop 保持一致：groups 缺省剔除 shell（高危默认关），
        # full_access=true 时放行 shell，使枚举与实际运行的分组对齐。
        tools_cfg = (rt.get("tools") or {})
        groups = tools_cfg.get("groups")
        if groups is None:
            groups = sorted({p.group for p in TOOL_REGISTRY.values() if p.group != "shell"})
        else:
            groups = list(groups)
        if tools_cfg.get("full_access") and "shell" not in groups:
            groups.append("shell")
        # 视图级工具禁用：读取 config.runtime.tools.disabled，过滤后构建注册表。
        # 在分组过滤之后执行，高权限放行工具仍可被单工具禁用。
        disabled = list(tools_cfg.get("disabled") or [])
        _disabled_set = {str(x) for x in disabled}
        _active_groups = {str(g) for g in groups}
        # 枚举「全部」插件（不做分组/禁用过滤），由视图侧按标记渲染开关，
        # 否则被禁用/未启用分组的工具会彻底消失、无法在界面上重新打开。
        registry = build_plugin_registry(None)

        tools = []
        for p in registry.plugins:
            fn = (p.schema or {}).get("function") or {}
            tools.append(
                {
                    "name": p.name,
                    "description": fn.get("description", "") or "",
                    "group": p.group,
                    "source": p.source,
                    # 视图开关状态：分组是否启用 + 是否被单工具禁用
                    "group_enabled": p.group in _active_groups,
                    "disabled": p.name in _disabled_set,
                    # 外部 MCP 工具额外标出来自哪个 server（自研为 None）
                    "server": p.meta.get("server") if p.source == "mcp" else None,
                    "parameters": fn.get("parameters") or {},
                }
            )
        tools.sort(key=lambda t: (t["group"], t["name"]))

        mcp_cfg = config.load_mcp_config()
        servers = []
        for s in mcp_cfg.get("servers") or []:
            if not isinstance(s, dict):
                continue
            servers.append(
                {
                    "name": s.get("name", ""),
                    "enabled": bool(s.get("enabled", True)),
                    "command": s.get("command", ""),
                    "args": list(s.get("args") or []),
                    "url": s.get("url", ""),
                    # env 只回传键名，不回传值（防密钥泄露）
                    "env_keys": sorted((s.get("env") or {}).keys()),
                }
            )
        return JSONResponse(
            {
                "tools": tools,
                # 当前生效的分组（与 tools 里的 group_enabled 一致）
                "groups": sorted(_active_groups),
                # 分组开关（Web「技能与工具 → 工具」页）：
                #   all_groups    = 注册表里存在的全部分组（含默认关闭的高危 shell）
                #   active_groups = 当前实际生效的分组（含 full_access 放行的 shell）
                "all_groups": sorted({p.group for p in TOOL_REGISTRY.values()}),
                "active_groups": sorted(set(groups)),
                # 视图级禁用名单：当前配置禁用的工具（与可用工具列表同源，
                # 仅做视图展示，不改动全局 TOOL_REGISTRY 原始数据）。
                "disabled": disabled,
                "mcp": {
                    "enabled": bool(mcp_cfg.get("enabled", False)),
                    "servers": servers,
                    "connected": sorted(
                        {t["server"] for t in tools if t["source"] == "mcp" and t["server"]}
                    ),
                },
            }
        )
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"工具枚举失败: {type(e).__name__}: {e}"}, status_code=500
        )

@router.patch("/tools/disabled")
async def set_disabled_tools(request: Request):
    """动态更新禁用工具列表（config.runtime.tools.disabled）。

    - body: {"disabled": ["tool_a", "tool_b"]}（字符串数组，空数组=清空）。
    - 前置校验：工具名必须存在于当前注册表，非法名返回 400。
    - 持久化到 ~/.omniagent/config.yaml，重启构建注册表后生效（无热更新）。
    不改动全局 TOOL_REGISTRY 原始数据（仅配置化视图过滤）。
    """
    import config as app_config

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    disabled = (body or {}).get("disabled")
    if not isinstance(disabled, list) or not all(isinstance(x, str) for x in disabled):
        return JSONResponse(
            {"ok": False, "error": "disabled 必须是字符串数组"}, status_code=400
        )
    # 前置工具名校验（非法工具名返回 400 报错）
    valid = set(TOOL_REGISTRY.keys())
    for name in disabled:
        if name not in valid:
            return JSONResponse(
                {"ok": False, "error": f"非法工具名: {name}"}, status_code=400
            )
    try:
        # 读取现有 ~/.omniagent/config.yaml，仅覆盖 runtime.tools.disabled，不丢其他节
        existing = copy.deepcopy(app_config.load_settings() or {})
        rt = existing.get("runtime") or {}
        tools = rt.get("tools") or {}
        tools["disabled"] = list(disabled)
        rt["tools"] = tools
        existing["runtime"] = rt
        app_config.save_settings(existing)
        # 失效配置缓存，使后续 load_config() 即时读到新配置（注册表仍在重启时重建）
        app_config.reload_config()
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"持久化失败: {type(e).__name__}: {e}"}, status_code=500
        )
    return JSONResponse({"ok": True, "disabled": list(disabled)})
