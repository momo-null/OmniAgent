"""工具管理接口：/tools（只读枚举）+ 插件开关。

模型：
- **插件**（`plugins/<name>/`）：每个一个 on/off（`~/.omniagent/plugins/<name>.yaml`）
  → 前端 switch；操作电脑（host）/ Android（emulator）与 web / filesystem 同级，
  没有独立的"环境"概念，也没有单选。
- **工具**：只读清单（来源 plugin / core / mcp），无逐工具开关。

前端不硬编码任何插件名：所有标题 / 说明 / 状态都来自后端自报。
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.routers.helpers import config

router = APIRouter(tags=["runtime"])


@router.get("/tools")
async def list_tools():
    """枚举：插件（switch）+ 工具（只读）+ MCP。"""
    try:
        import config as app_config

        from omni_core.tools.base import TOOL_REGISTRY
        from omni_core.tools.loader import PluginContext, list_plugins, load_plugins
        from omni_core.tools.mcp_servers import build_mcp_servers, server_name, split_configs
        from omni_core.tools import mcp_state
        from omni_core.async_bridge import run_async

        cfg = app_config.load_config() or {}
        # 幂等装载插件（只读枚举：wired=False，插件不得做破坏性动作）
        load_plugins(cfg, ctx=PluginContext(config=cfg, wired=False))

        tools = []
        for p in TOOL_REGISTRY.values():
            fn = (p.schema or {}).get("function") or {}
            tools.append({
                "name": p.name,
                "description": fn.get("description", "") or "",
                "source": p.source,          # core | plugin | mcp
                "unit": p.unit,              # 提供者标识
                "server": p.meta.get("server") if p.source == "mcp" else None,
                "parameters": fn.get("parameters") or {},
            })

        plugins = list_plugins(cfg)

        mcp_cfg = config.load_mcp_config()

        # MCP 工具发现（只读枚举）：**只对 mode=eager 的 server 真连接 + list_tools**，
        # 与运行时 _ensure_mcp_connected 同机制；单个 server 连不上 / 探活失败只跳过，
        # 不拖垮整个枚举。工具名带 SDK 前缀 mcp_<server>__<tool>（与 agent 实际可见名一致）。
        #
        # lazy server **绝不在这里连接**（mcp-lazy-activation）：否则"打开设置页"就等于
        # "开了就连"，懒加载白做。它们只读 mcp_state 缓存（状态 + 上次 list_tools 的名字），
        # 参数 schema 不下发——激活后由 SDK 自己注入。
        _all_cfgs = list(mcp_cfg.get("servers") or [])
        _eager_cfgs, _lazy_cfgs = split_configs(_all_cfgs)
        try:
            _mcp_servers = build_mcp_servers(_eager_cfgs, log=lambda m: None)
            for _srv in _mcp_servers:
                _nm = str(getattr(_srv, "name", "?") or "?")
                try:
                    if getattr(_srv, "session", None) is None:
                        run_async(_srv.connect(), timeout=15)
                    # 走统一入口：配了 callable tool_filter 的 server 在上下文外裸调
                    # list_tools() 会被 SDK 拒（agents/mcp/server.py:1019）
                    _raw = mcp_state.list_tools(_srv, timeout_s=15)
                    for _rt in _raw:
                        _rn = str(getattr(_rt, "name", "") or "")
                        if not _rn:
                            continue
                        tools.append({
                            "name": f"mcp_{_nm}__{_rn}",
                            "description": getattr(_rt, "description", "") or "",
                            "source": "mcp",
                            "unit": _nm,
                            "server": _nm,
                            "parameters": getattr(_rt, "input_schema", None) or {},
                        })
                except Exception:
                    pass
                finally:
                    try:
                        _clean = getattr(_srv, "cleanup", None)
                        if _clean is not None:
                            run_async(_clean(), timeout=10)
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            _states = mcp_state.load_all()
            for _lc in _lazy_cfgs:
                _nm = server_name(_lc)
                _st = _states.get(_nm)
                for _rn in (list(_st.tools) if _st else []):
                    tools.append({
                        "name": f"mcp_{_nm}__{_rn}",
                        "description": "",
                        "source": "mcp",
                        "unit": _nm,
                        "server": _nm,
                        "parameters": {},
                        "lazy": True,
                        "state": (_st.state if _st else mcp_state.UNKNOWN),
                        "ready": bool(_st.ready) if _st else False,
                    })
        except Exception:
            pass

        tools.sort(key=lambda t: (t["source"], t["name"]))

        servers = []
        _all_states = mcp_state.load_all()
        for s in mcp_cfg.get("servers") or []:
            if not isinstance(s, dict):
                continue
            _sn = str(s.get("name") or "")
            _ss = _all_states.get(_sn)
            servers.append({
                "name": _sn,
                "enabled": bool(s.get("enabled", True)),
                "command": s.get("command", ""),
                "args": list(s.get("args") or []),
                "url": s.get("url", ""),
                # env 只回传键名，不回传值（防密钥泄露）
                "env_keys": sorted((s.get("env") or {}).keys()),
                # lazy 语义（mcp-lazy-activation）：前端据此显示"按需激活"与上次观测状态，
                # 不要拿 state 当"已连接"（它是缓存观测，可能过时）。
                "mode": str(s.get("mode") or "lazy"),
                "desc": str(s.get("desc") or ""),
                "state": (_ss.state if _ss else mcp_state.UNKNOWN),
                "ready": bool(_ss.ready) if _ss else False,
                "state_reason": (_ss.reason if _ss else ""),
                "state_age_sec": (int(_ss.fresh_sec) if _ss and _ss.ts else -1),
            })

        return JSONResponse({
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


# ── 插件配置（schema 由插件自报，宿主不认识任何具体键名）────────────────────
# plugin.json 的 "config" 是字段数组：{key, label, type, default?, desc?, options?,
# secret?, min?, max?, group?}。type ∈ text|password|path|number|bool|select。
# 本接口只做「按键写 YAML / 按 type 校验」，因此对任何插件通用（语音、OCR、视觉…）。
#
# scope=service：插件可以把**另一份独立文件的 schema** 放在 manifest 的
# "service_config": {"path": "~/.omniagent/xxx.yaml", "fields": [...]}。
# 用途是「插件只是调用端，引擎是独立服务」——服务自己的配置不该混进插件目录。
# 宿主仍然只按声明读写，不认识任何具体键名或服务名。

def _dig(data, key, default=None):
    """按点分路径取值（``openai.base_url`` 表示嵌套键）。"""
    cur = data
    for part in str(key).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _put_dig(data: dict, key: str, value) -> None:
    parts = str(key).split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _fields(raw) -> list:
    """从字段数组里剔除无效项（无 key / 无 label）。"""
    if not isinstance(raw, list):
        return []
    out = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        key = str(f.get("key") or "").strip()
        if not key or not str(f.get("label") or "").strip():
            continue
        out.append(f)
    return out


def _schema_fields(manifest: dict) -> list:
    """manifest 顶层 ``config``（插件自己目录里的那份配置）。"""
    return _fields(manifest.get("config"))


def _read_yaml(path) -> dict:
    """读一份 YAML 配置；缺失/非法一律空 dict（配置坏了不该让整页读不出来）。"""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _scope_target(manifest: dict, name: str, scope: str):
    """（路径, 字段数组）：

    - ``plugin``：``~/.omniagent/plugins/<name>.yaml``（插件自己的配置）
    - ``service``：manifest 的 ``service_config.path`` 指向的独立文件（独立服务配置）
    """
    if scope == "service":
        node = manifest.get("service_config")
        if not isinstance(node, dict) or not str(node.get("path") or "").strip():
            return None, []
        return Path(os.path.expanduser(str(node["path"]).strip())), _fields(node.get("fields"))
    from omni_core.local.runtime_paths import plugin_config_file

    return plugin_config_file(name), _fields(manifest.get("config"))


def _coerce(field: dict, value):
    """按 schema 校验/规范化单个值；不合法抛 ValueError。"""
    key = field.get("key")
    typ = str(field.get("type") or "text")
    if typ == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        return value
    if typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} 必须是数字")
        if "min" in field and value < field["min"]:
            raise ValueError(f"{key} 不能小于 {field['min']}")
        if "max" in field and value > field["max"]:
            raise ValueError(f"{key} 不能大于 {field['max']}")
        return int(value) if float(value).is_integer() else float(value)
    if typ == "select":
        allowed = [str(o.get("value")) for o in (field.get("options") or [])
                   if isinstance(o, dict)]
        if str(value) not in allowed:
            raise ValueError(f"{key} 必须是 {allowed} 之一")
        return str(value)
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    return value


@router.get("/tools/plugins/{name}/config")
async def get_plugin_config(name: str, scope: str = "plugin"):
    """配置 schema + 当前值（secret 只回「是否已设置」，明文永不出站）。

    ``scope=plugin``（默认）读插件目录那份；``scope=service`` 读 manifest 声明的
    独立服务配置。两者 schema 都来自 manifest，宿主不认键名。
    """
    try:
        import config as app_config
        from omni_core.tools.loader import plugin_manifest

        cfg = app_config.load_config() or {}
        manifest = plugin_manifest(name, cfg)
        if not manifest:
            return JSONResponse({"ok": False, "error": f"未知插件: {name}"}, status_code=404)
        if scope not in ("plugin", "service"):
            return JSONResponse({"ok": False, "error": f"未知 scope: {scope}"}, status_code=400)
        path, fields = _scope_target(manifest, name, scope)
        if path is None:
            return JSONResponse(
                {"ok": False, "error": f"插件 {name} 未声明 {scope} 配置"}, status_code=400)
        raw = _read_yaml(path)
        values, secrets_set = {}, {}
        for f in fields:
            key = f["key"]
            val = _dig(raw, key, f.get("default"))
            if f.get("secret"):
                secrets_set[key] = bool(val)
                values[key] = ""
            else:
                values[key] = val
        return JSONResponse({"ok": True, "name": name, "scope": scope, "path": str(path),
                             "schema": fields, "values": values, "secrets_set": secrets_set})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"读取配置失败: {type(e).__name__}: {e}"},
            status_code=500)


@router.put("/tools/plugins/{name}/config")
async def put_plugin_config(name: str, request: Request, scope: str = "plugin"):
    """按 schema 写回目标 YAML（同 GET 的 scope 语义）。

    - body: ``{"values": {key: value}}``；secret 字段传空串 = 保持不变
      （GET 不回传明文，避免一次保存把密钥清空）。
    - 写回用 PyYAML 整份重写，YAML 注释会丢——需要注释时改文件本身。
    """
    import config as app_config
    from omni_core.tools.loader import plugin_manifest

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    values = (body or {}).get("values") if isinstance(body, dict) else None
    if not isinstance(values, dict):
        return JSONResponse({"ok": False, "error": "需要 {values: {...}}"}, status_code=400)

    cfg = app_config.load_config() or {}
    manifest = plugin_manifest(name, cfg)
    if not manifest:
        return JSONResponse({"ok": False, "error": f"未知插件: {name}"}, status_code=404)
    if scope not in ("plugin", "service"):
        return JSONResponse({"ok": False, "error": f"未知 scope: {scope}"}, status_code=400)
    path, field_list = _scope_target(manifest, name, scope)
    if path is None:
        return JSONResponse(
            {"ok": False, "error": f"插件 {name} 未声明 {scope} 配置"}, status_code=400)
    fields = {f["key"]: f for f in field_list}
    if not fields:
        return JSONResponse({"ok": False, "error": f"插件 {name} 未声明可配置项"},
                            status_code=400)
    unknown = sorted(k for k in values if k not in fields)
    if unknown:
        return JSONResponse({"ok": False, "error": f"未知配置项: {', '.join(unknown)}"},
                            status_code=400)

    current = _read_yaml(path)
    for key, val in values.items():
        field = fields[key]
        if field.get("secret") and val == "":
            continue  # 空串 = 保持不变
        try:
            _put_dig(current, key, _coerce(field, val))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(current, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"写入配置失败: {type(e).__name__}: {e}"},
            status_code=500)
    return JSONResponse({"ok": True, "name": name, "scope": scope, "path": str(path)})
