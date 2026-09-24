"""设置路由（UI 完整配置面板后端）

- GET  /api/settings      返回当前生效配置（config.yaml + ~/.omniagent/config.yaml 合并后的
                          与「推理模式/模型端点」相关节，以及是否本地模型随服务自启）。
                          密钥脱敏：api_key 不返回明文，只返回 api_key_set:bool。
- PUT  /api/settings      把前端提交的配置写回 ~/.omniagent/config.yaml（deepMerge 覆盖
                          config.yaml，不污染 config.yaml）。空字符串 api_key 表示"保持不变"。

设计：config.yaml（项目根）只放基础默认；用户配置集中在 ~/.omniagent/config.yaml
（去明文 key、跨项目共享）。本路由是 UI 设置面板的唯一写入点。

三通道单一真源（方案 A）：
- brain（顶层）：planner 大脑
- runtime.executor（嵌套）：执行器 worker
- runtime.vision（嵌套）：视觉通道
- llm.local_as_tool（M9）：本地模型以工具形态暴露及其边界声明

M9：取消 runtime.mode 三态——主模型可以在线也可以直接配本地端点，
是否另起本地模型由 runtime.executor（子 agent 模型）与 llm.local_as_tool 决定。

前端按此嵌套结构读写；不再接受顶层 executor/vision 镜像。
"""
import copy
import os
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import config

router = APIRouter(prefix="/api/settings", tags=["settings"])

# UI 可编辑的配置节（白名单，避免前端任意写导致破坏）
# 三通道单一真源：brain 顶层，executor/vision 嵌套在 runtime 下。
_EDITABLE_KEYS = ("runtime", "brain", "local_model", "llm")

# 各通道中需脱敏的密钥字段名
_API_KEY_FIELDS = ("api_key",)

# GET 脱敏时生成的伪字段（api_key_set 等）：写回前必须剔除，
# 否则前端把 GET 的整块视图 PUT 回来时会把伪字段写进真实配置。
_MASK_FIELDS = {f"{k}_set" for k in _API_KEY_FIELDS}


def _strip_mask_artifacts(node: Any) -> Any:
    """剔除脱敏伪字段（api_key_set）与 None 值，返回清洗后的副本。

    GET 返回的是脱敏视图，前端会整块回传；若不清洗，伪字段会被当成真实配置
    写进 ~/.omniagent/config.yaml。
    """
    if isinstance(node, dict):
        return {
            k: _strip_mask_artifacts(v)
            for k, v in node.items()
            if k not in _MASK_FIELDS and v is not None
        }
    if isinstance(node, list):
        return [_strip_mask_artifacts(x) for x in node]
    return node


def _prune_defaults(node: Any, base: Any) -> Any:
    """只保留与出厂默认（项目 config.yaml）不同的分支，用户文件只存差集。

    目的：避免前端把「合并后的整份配置」回传后被原样持久化——那会让用户文件
    变成一份默认值快照，之后项目侧改配置会被这份旧快照盖住（表现为配置变回默认）。
    差集语义下，与默认相同的键一律不写（有效配置不变，因为 base 已提供该值）。
    """
    if not isinstance(node, dict):
        return node
    base_node = base if isinstance(base, dict) else {}
    out: Dict[str, Any] = {}
    for k, v in node.items():
        b = base_node.get(k)
        if isinstance(v, dict):
            sub = _prune_defaults(v, b if isinstance(b, dict) else {})
            if sub:
                out[k] = sub
        elif v != b:
            out[k] = v
    return out


def _mask_secrets(node: Any) -> Any:
    """递归把 dict 中的 api_key 字段脱敏为 api_key_set:bool。

    保留原结构，只替换密钥字段。非 dict 原样返回。
    """
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if k in _API_KEY_FIELDS:
                out[f"{k}_set"] = bool(v)
            else:
                out[k] = _mask_secrets(v)
        return out
    if isinstance(node, list):
        return [_mask_secrets(x) for x in node]
    return node


@router.get("")
async def get_settings() -> Dict[str, Any]:
    """返回当前生效配置中与设置面板相关的节（密钥脱敏）。"""
    # 生效视图 = 代码缺省 < 项目 config.yaml < 用户配置；前端看到的就是真实生效值，
    # 不会显示裸 0（避免用户以为功能关闭、也不存在把 0 保存回去的路径）。
    cfg = config.deep_merge(config.CONTEXT_DEFAULTS, config.load_config())
    out: Dict[str, Any] = {}
    for k in _EDITABLE_KEYS:
        v = cfg.get(k)
        if v is not None:
            out[k] = _mask_secrets(copy.deepcopy(v))
    # 给前端一个干净的默认骨架，避免首次无 ~/.omniagent/config.yaml 时缺字段
    out.setdefault("llm", {})
    # M-new：新 schema 默认骨架（providers 定义端点，agents 引用）
    out["llm"].setdefault("providers", {})
    out.setdefault("runtime", {})
    out["runtime"].setdefault("executor", {"enabled": True})
    out["runtime"].setdefault("vision", {"enabled": False})
    out["runtime"].setdefault("agents", {})
    out.setdefault("local_model", {"auto_start": False, "default_model": ""})
    out.setdefault("brain", {})
    # M9：本地模型工具（enabled 缺省跟随 executor，边界缺省为空→用代码里的中性兜底）
    out["llm"].setdefault("local_as_tool", {"enabled": bool(
        (out["runtime"].get("executor") or {}).get("enabled", True))})
    # 暴露设置文件实际位置与是否存在，便于确认持久化（排查「配置丢失」）。
    out["_meta"] = {
        "settings_path": config.SETTINGS_PATH,
        "settings_exists": os.path.exists(config.SETTINGS_PATH),
    }
    return out


@router.put("")
async def put_settings(req: Request) -> JSONResponse:
    """把前端提交的配置写入 ~/.omniagent/config.yaml（仅白名单节，密钥空串表示保持不变）。"""
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "请求体必须是对象"}, status_code=400)

    # 只保留白名单节，且每个节必须是 dict
    patch: Dict[str, Any] = {}
    for k in _EDITABLE_KEYS:
        if k in body:
            v = body[k]
            if not isinstance(v, dict):
                return JSONResponse(
                    {"ok": False, "error": f"字段 {k} 必须是对象"}, status_code=400
                )
            patch[k] = v

    # 外部 MCP 配置抽离到 ~/.omniagent/mcp.json（不再写进 config.yaml）
    mcp_patch = (patch.get("runtime") or {}).pop("mcp", None)
    if mcp_patch is not None:
        # M3: 校验（数组项须为对象，启用的必须有 command 或 url）
        if not isinstance(mcp_patch, dict) or "servers" not in mcp_patch:
            return JSONResponse(
                {"ok": False, "error": "runtime.mcp 必须是含 servers 的对象"}, status_code=400
            )
        servers = mcp_patch["servers"]
        if not isinstance(servers, list):
            return JSONResponse(
                {"ok": False, "error": "runtime.mcp.servers 必须是数组"}, status_code=400
            )
        for s in servers:
            if not isinstance(s, dict):
                return JSONResponse(
                    {"ok": False, "error": "runtime.mcp.servers 每项必须是对象"}, status_code=400
                )
            if s.get("enabled", True) and not (s.get("command") or s.get("url")):
                return JSONResponse(
                    {"ok": False, "error": "启用的 MCP server 必须提供 command 或 url"},
                    status_code=400,
                )
        config.save_mcp_config(mcp_patch)

    # 密钥"保持不变"逻辑：空字符串 api_key → 从现有配置恢复原值
    existing_cfg = config.load_config()
    for section in ("brain",):
        if section in patch:
            _preserve_existing_api_key(patch[section], existing_cfg.get(section, {}))
    if "runtime" in patch:
        rt_patch = patch["runtime"]
        rt_existing = existing_cfg.get("runtime", {})
        for sub in ("executor", "vision"):
            if sub in rt_patch:
                _preserve_existing_api_key(rt_patch[sub], rt_existing.get(sub, {}))
    # M-new：llm.providers.* 的 api_key 同样支持「空串=保持不变」
    if "llm" in patch:
        llm_patch = patch["llm"]
        if isinstance(llm_patch, dict):
            llm_existing = existing_cfg.get("llm", {})
            prov_patch = llm_patch.get("providers")
            if isinstance(prov_patch, dict):
                prov_existing = llm_existing.get("providers", {})
                for name, pnode in prov_patch.items():
                    if isinstance(pnode, dict):
                        _preserve_existing_api_key(pnode, prov_existing.get(name, {}))

    # 读取现有 ~/.omniagent/config.yaml，做 deepMerge 覆盖（不丢其他节）
    existing = copy.deepcopy(config.load_settings())
    for k, v in patch.items():
        if k in existing and isinstance(existing[k], dict) and isinstance(v, dict):
            existing[k] = config.deep_merge(existing[k], v)
        else:
            existing[k] = v

    # 只写「差集」：剔除脱敏伪字段，并丢掉与出厂默认相同的键，
    # 防止 GET 的合并视图被整份快照进 ~/.omniagent/config.yaml。
    cleaned = _prune_defaults(_strip_mask_artifacts(existing), config.load_effective_defaults())
    config.save_settings(cleaned)
    # 失效配置缓存，使 /chat 等即时读到新设置（见 config.reload_config）
    config.reload_config()
    return JSONResponse({"ok": True})


def _preserve_existing_api_key(patch_node: dict, existing_node: dict) -> None:
    """如果 patch 中 api_key 为空字符串，从 existing 恢复原值（保持不变语义）。

    原地修改 patch_node。
    """
    for k in _API_KEY_FIELDS:
        if k in patch_node and not patch_node[k]:
            # 空字符串 → 保持原值（从现有配置恢复）
            orig = existing_node.get(k, "") if isinstance(existing_node, dict) else ""
            if orig:
                patch_node[k] = orig
            else:
                del patch_node[k]
