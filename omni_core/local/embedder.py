"""向量嵌入服务客户端（内核侧：**只连地址，不管进程**）。

服务本身的启动、探活、崩溃重启、日志由 ``model_hub`` 统一负责
（``~/.omniagent/model_hub.yaml`` 里的 ``embed-svc``）。本模块只剩三件事：

1. 读 core 自己的开关与地址（``runtime.knowledge.memory.vector``）；
2. 探活（带 30s 缓存，避免每次检索都打 HTTP）；
3. 把文本编码成向量。

这样换成任何 OpenAI 兼容的**线上**向量服务，只改 ``service_url`` 即可，内核零改动。
这也保证了分层：内核不认识 venv、模型路径、进程管理。

红线：服务未起 / 模型缺失 / 任何异常 → ``ready()=False`` → 检索静默降级 FTS-only，
工具照常返回（fail-soft）。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
from urllib.request import urlopen

from utils import get_logger

logger = get_logger("memory.embedder")

#: 健康缓存（秒）——入站路径每次检索都会问 ready，避免高频探测
_HEALTH_TTL = 30.0

_state: Dict[str, Any] = {"health_ts": 0.0, "health_ok": False}


def _vec_cfg(key: str, default: Any = None) -> Any:
    try:
        import config
        return config.get_config(f"runtime.knowledge.memory.vector.{key}", default)
    except Exception:
        return default


def enabled() -> bool:
    """core 是否启用向量通道（业务层开关，与 model_hub 的「是否启动」正交）。"""
    return bool(_vec_cfg("enabled", False))


def service_url() -> str:
    """向量服务地址；换线上服务只改这一项。"""
    return str(_vec_cfg("service_url", "http://127.0.0.1:18743")).rstrip("/")


def _health() -> bool:
    try:
        with urlopen(f"{service_url()}/health", timeout=2.0) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def ready() -> bool:
    """地址通不通（enabled + 探活，缓存 30s）。**不负责拉起**。"""
    if not enabled():
        return False
    now = time.time()
    if now - _state["health_ts"] < _HEALTH_TTL:
        return bool(_state["health_ok"])
    ok = _health()
    _state.update({"health_ts": now, "health_ok": ok})
    if not ok:
        logger.info("向量服务不可用（%s）——降级 FTS-only", service_url())
    return ok


def encode(texts: List[str]) -> Optional[List[List[float]]]:
    """批量嵌入（已归一化，余弦=点积）。任何失败返回 None（调用方降级 FTS-only）。"""
    if not texts or not ready():
        return None
    try:
        req = json.dumps({"texts": list(texts)}).encode("utf-8")
        with urlopen(f"{service_url()}/embed", data=req, timeout=120.0) as r:
            data = json.loads(r.read().decode("utf-8"))
        vecs = data.get("vectors")
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            return None
        return vecs
    except Exception as e:  # noqa: BLE001
        logger.warning("embed 调用失败（降级 FTS-only）: %s: %s", type(e).__name__, e)
        return None
