"""模型路由层：把「槽位 + 选择」解析成可喂 LLMClient / build_sdk_model 的端点配置。

设计（模型路由，2026-09-29；真源收敛同日）：

- **目录是端点的唯一真源**：厂商/模型清单（base_url、api_key、模型 id）只存
  ``~/.omniagent/models.json``（``config.load_models_config``）。旧配置端点键
  （顶层 ``brain`` 的 base_url/model/api_key、``runtime.executor``、
  ``llm.local_as_tool``）**不再被读取**，从 config.yaml 删除即可。
- **槽位（slot）**：``main``（主模型）/ ``worker``（子 agent 模型）。槽位是**用途**
  而非模型：目录里为槽位选了模型 = 启用该用途，没选 = 不启用（worker 由主模型兼任）。
- **选择（selection）**：``"<provider_id>/<model_id>"``。只有命中目录白名单的
  selection 才会被采纳（防止通过请求体注入任意 base_url / SSRF）。
- 引擎参数（历史压缩 / 上下文预算 / 推理模式）不属于端点，仍在 ``config.yaml``：
  本模块只映射端点字段，不吞并引擎参数。

本模块零业务 / 场景逻辑，只做「目录 → 端点 dict」的映射。
"""
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import config

# 槽位：模型的用途标识（新增多 agent 角色时在此扩展，不改动路由逻辑）
SLOTS: Tuple[str, ...] = ("main", "worker")

# selection 形如 `<provider_id>/<model_id>`；provider_id 不允许含 `/`
_SELECTION_RE = re.compile(r"^([^/\s]+)/(.+)$")


# --- 目录读写 ---------------------------------------------------------------
def _catalog() -> Dict[str, Any]:
    return config.load_models_config() or {}


def _providers() -> Dict[str, Any]:
    p = _catalog().get("providers")
    return p if isinstance(p, dict) else {}


def _defaults() -> Dict[str, Any]:
    d = _catalog().get("defaults")
    return d if isinstance(d, dict) else {}


def _models_of(provider: Dict[str, Any]) -> List[Dict[str, Any]]:
    ms = provider.get("models")
    if not isinstance(ms, list):
        return []
    return [m for m in ms if isinstance(m, dict) and str(m.get("id") or "").strip()]


def _find_model(provider: Dict[str, Any], model_id: str) -> Optional[Dict[str, Any]]:
    for m in _models_of(provider):
        if str(m.get("id")) == model_id:
            return m
    return None


def _parse_in(providers: Dict[str, Any], selection: Optional[str]) -> Optional[Tuple[str, str]]:
    """在给定 providers 映射内解析 ``provider_id/model_id``。"""
    if not selection or not isinstance(providers, dict):
        return None
    m = _SELECTION_RE.match(str(selection).strip())
    if not m:
        return None
    pid, mid = m.group(1), m.group(2).strip()
    prov = providers.get(pid)
    if not isinstance(prov, dict):
        return None
    if _find_model(prov, mid) is None:
        return None
    return pid, mid


def parse_selection(selection: Optional[str]) -> Optional[Tuple[str, str]]:
    """解析 ``provider_id/model_id``；格式非法或目录里不存在则返回 None。"""
    return _parse_in(_providers(), selection)


# --- 端点构造 ---------------------------------------------------------------
def _is_local_endpoint(base_url: str) -> bool:
    bu = str(base_url or "").lower()
    return ("127.0.0.1" in bu) or ("localhost" in bu)


def _endpoint_of(pid: str, prov: Dict[str, Any], model_id: str) -> Dict[str, Any]:
    """目录条目 → 端点 dict（字段与 LLMClient / resolve.py 的端点键一致）。"""
    model = _find_model(prov, model_id) or {}
    api_key = str(prov.get("api_key") or "").strip()
    env_key = str(prov.get("api_key_env") or "").strip()
    if not api_key and env_key:
        api_key = os.environ.get(env_key, "")
    if not api_key and _is_local_endpoint(prov.get("base_url", "")):
        # 本机 OpenAI 兼容 server（llama.cpp 等）通常不需要 key；占位以满足协议层
        api_key = "dummy"
    return {
        "provider": "openai-compatible",
        "base_url": str(prov.get("base_url") or "").rstrip("/"),
        "model": model_id,
        "api_key": api_key,
        "api_key_env": env_key,
        "capabilities": {"vision": bool(model.get("vision"))},
        "request": dict(prov.get("request") or {}),
        "model_provider_id": pid,
    }


def resolve_slot(slot: str, selection: Optional[str] = None) -> Dict[str, Any]:
    """解析某槽位最终使用的端点配置（**只查目录**）。

    Args:
        slot: ``main`` / ``worker``。
        selection: 显式选择 ``provider_id/model_id``；非法则忽略（不报错，回退槽位默认）。

    Returns:
        端点 dict；目录里该槽位没有选择时返回空 dict——语义是「该用途未启用」
        （worker 由主模型兼任），**不再回退旧配置**。
    """
    parsed = parse_selection(selection) or parse_selection(_defaults().get(slot))
    if not parsed:
        return {}
    pid, mid = parsed
    prov = _providers().get(pid)
    if not isinstance(prov, dict):
        return {}
    return _endpoint_of(pid, prov, mid)


def current_selection(slot: str) -> Optional[str]:
    """槽位当前生效的 selection；来自目录或「跟随配置」时返回 None（由调用方标注）。"""
    d = _defaults().get(slot)
    return str(d).strip() if d else None


# --- 目录视图（供 API / 前端） ----------------------------------------------
def list_models() -> Dict[str, Any]:
    """返回按提供方分组的模型目录 + 各槽位当前生效端点（密钥脱敏）。"""
    providers: List[Dict[str, Any]] = []
    for pid, p in _providers().items():
        if not isinstance(p, dict):
            continue
        providers.append({
            "id": pid,
            "label": str(p.get("label") or pid),
            "base_url": str(p.get("base_url") or ""),
            "api_key_set": bool(str(p.get("api_key") or "").strip())
            or bool(str(p.get("api_key_env") or "").strip()),
            "models": [
                {
                    "id": str(m.get("id")),
                    "label": str(m.get("label") or m.get("id")),
                    "vision": bool(m.get("vision")),
                }
                for m in _models_of(p)
            ],
        })

    current: Dict[str, Any] = {}
    for slot in SLOTS:
        sel = current_selection(slot)
        # 目录默认已在 resolve_slot 内部参与解析，此处直接用槽位解析结果
        ep = resolve_slot(slot)
        current[slot] = {
            "selection": sel or "",
            "model": str(ep.get("model") or ""),
            "base_url": str(ep.get("base_url") or ""),
            "provider_id": str(ep.get("model_provider_id") or ""),
            # source: catalog=目录真源；none=未配置
            "source": "catalog" if ep else "none",
        }
    return {"providers": providers, "defaults": dict(_defaults()), "current": current}


# --- 目录写回 ---------------------------------------------------------------
def _clean_provider(node: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """清洗单个 provider：丢未知字段、空 api_key 表示「保持不变」。"""
    out: Dict[str, Any] = {
        "label": str(node.get("label") or "").strip(),
        "base_url": str(node.get("base_url") or "").strip(),
        "api_key": str(node.get("api_key") or "").strip(),
        "api_key_env": str(node.get("api_key_env") or "").strip(),
    }
    # 空字符串 api_key = 保持不变（与 /api/settings 一致，避免前端 GET 脱敏视图回传清空密钥）
    if not out["api_key"]:
        out["api_key"] = str(existing.get("api_key") or "").strip()
    req = node.get("request")
    if isinstance(req, dict) and req:
        out["request"] = req

    models: List[Dict[str, Any]] = []
    for m in node.get("models") or []:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        models.append({
            "id": mid,
            "label": str(m.get("label") or "").strip(),
            "vision": bool(m.get("vision")),
        })
    out["models"] = models
    return out


def save_providers(providers: Dict[str, Any]) -> None:
    """整体写回 providers（调用前已完成校验）。"""
    existing_all = _providers()
    cleaned: Dict[str, Any] = {}
    for pid, node in (providers or {}).items():
        if not isinstance(node, dict):
            continue
        key = str(pid).strip()
        if not key:
            continue
        existing = existing_all.get(key)
        cleaned[key] = _clean_provider(node, existing if isinstance(existing, dict) else {})

    data = dict(_catalog())
    data["providers"] = cleaned
    # 清理指向已删除 provider/model 的默认选择（按**写入后**的目录判定，
    # 否则会拿旧缓存把已失效的选择保留下来）
    defaults = data.get("defaults")
    if isinstance(defaults, dict):
        data["defaults"] = {
            s: v for s, v in defaults.items() if _parse_in(cleaned, v) is not None
        }
    config.save_models_config(data)
    config.reload_config()


def set_default(slot: str, selection: Optional[str]) -> None:
    """设置某槽位的默认模型；``selection`` 为空串/None 表示「跟随配置」。"""
    if slot not in SLOTS:
        raise ValueError(f"未知槽位: {slot}")
    data = dict(_catalog())
    defaults = data.get("defaults")
    defaults = dict(defaults) if isinstance(defaults, dict) else {}
    sel = str(selection or "").strip()
    if sel and parse_selection(sel) is None:
        raise ValueError(f"模型选择不存在于目录: {sel}")
    if sel:
        defaults[slot] = sel
    else:
        defaults.pop(slot, None)
    data["defaults"] = defaults
    config.save_models_config(data)
    config.reload_config()
