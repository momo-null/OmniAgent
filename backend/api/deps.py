"""共享依赖与模块级工具

提供 FastAPI 依赖单例（model_hub 管理器），
并暴露 ``_resolve_model_name`` / ``_ensure_model_running`` 供 router_llm 复用
（逻辑从原 ``llm_runtime/api_gateway.py`` 搬来）。

注：X2 的 AgentService（StateManager/CognitionModule 封装）已随 X3 两层架构
重构移除（归档见 git 历史）；X3 agent 运行时由
``backend.api.router_runtime`` 提供（SSE + run/skill）。
"""
from typing import Dict, Any, Optional

from model_hub.manager import ModelManager

# ── 单例缓存 ──────────────────────────────────────────────
_model_hub: Optional[ModelManager] = None


def get_model_hub() -> ModelManager:
    """返回 model_hub 管理器单例"""
    global _model_hub
    if _model_hub is None:
        _model_hub = ModelManager()
    return _model_hub


def _resolve_model_name(model_name: str, mgr: Optional[ModelManager] = None) -> Optional[str]:
    """解析模型名，支持 default / 别名

    重构后不再依赖 config 注册表，改为扫描目录后匹配模型名。

    Args:
        model_name: 请求中传入的模型名
        mgr: 可选的 ModelManager 实例，缺省时取单例

    Returns:
        解析后的模型名；无法解析时返回 None
    """
    if mgr is None:
        mgr = get_model_hub()
    if model_name in ("default", "local", ""):
        return mgr.get_default_model()
    # 扫描目录得到所有模型名
    names = [m["name"] for m in mgr.scan_and_build_models()]
    # 精确匹配
    if model_name in names:
        return model_name
    # 模糊匹配：名字包含
    low = model_name.lower()
    for scanned_name in names:
        if low in scanned_name.lower():
            return scanned_name
    return None


def _ensure_model_running(name: str, mgr: Optional[ModelManager] = None) -> Dict[str, Any]:
    """确保模型正在运行，未启动则自动拉起（lazy load）

    重构后从扫描结果定位模型并启动；端口冲突由进程表与自动分配处理。

    Args:
        name: 解析后的模型名
        mgr: 可选的 ModelManager 实例，缺省时取单例

    Returns:
        启动/状态结果字典
    """
    if mgr is None:
        mgr = get_model_hub()
    active = mgr.get_active_models()
    running_names = [m["name"] for m in active if m["process_alive"] and m["healthy"]]

    if name in running_names:
        return {"name": name, "status": "already_running"}

    return mgr.start_model(name, wait_ready=True, timeout=90)
