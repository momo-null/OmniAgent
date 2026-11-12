"""向量嵌入服务客户端（向量召回臂，分支 feat/vector-recall）。

架构（doc/plans/vector-recall-2026-11-13.md §4）：主进程（网关/后端）**零新增重依赖**
——嵌入推理跑在独立 venv 的 sidecar 服务（`scripts/embed_server.py`，自带
sentence-transformers），主进程只通过 localhost HTTP 调用。

红线：
- 主 .venv 一个包都不动（cu128 torch 属于语音链路，被 CPU 版覆盖 = 打坏语音）；
- 模型缺失 / sidecar 未起 / 任何异常 → `ready()=False` → 检索静默降级 FTS-only，
  工具照常返回；
- 只做候选生成：本模块只产出向量，不做语义过滤/解释/规则化（INV-3）。

sidecar 生命周期：首次使用时若「已启用 + 独立 venv 存在 + 端口无服务」则**自动
拉起**（分离进程，日志落 model_hub/embed-svc/svc.log），健康等待 ≤120s；venv 不
存在（没跑过 setup 脚本）→ 保持 not-ready，不报错。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.request import urlopen

from utils import get_logger

logger = get_logger("memory.embedder")

#: sidecar HTTP 健康缓存（秒）——入站路径每次检索都会问 ready，避免高频探测
_HEALTH_TTL = 30.0

_state: Dict[str, Any] = {"health_ts": 0.0, "health_ok": False, "start_tried": False}


def _vec_cfg(key: str, default: Any = None) -> Any:
    try:
        import config
        return config.get_config(f"runtime.knowledge.memory.vector.{key}", default)
    except Exception:
        return default


def enabled() -> bool:
    """总开关（缺省关 = 行为与无向量时代完全一致）。"""
    return bool(_vec_cfg("enabled", False))


def service_url() -> str:
    return str(_vec_cfg("service_url", "http://127.0.0.1:18743")).rstrip("/")


def _svc_home() -> str:
    try:
        import omni_core.local.runtime_paths as rp
        base = rp._GLOBAL
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".omniagent")
    # model_hub 约定目录与本仓同级（D:\AI\model_hub）或仓内回退——两个位置都探测
    repo_hub = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "model_hub")
    for cand in (os.path.join(repo_hub, "embed-svc"),
                 os.path.join(base, "model_hub", "embed-svc")):
        if os.path.isdir(cand):
            return cand
    return os.path.join(repo_hub, "embed-svc")


def _svc_python() -> Optional[str]:
    cand = _vec_cfg("service_python", "") or ""
    if cand and os.path.isfile(cand):
        return cand
    guess = os.path.join(_svc_home(), "venv", "Scripts", "python.exe")
    if os.name != "nt":
        guess = os.path.join(_svc_home(), "venv", "bin", "python")
    return guess if os.path.isfile(guess) else None


def _health() -> bool:
    try:
        with urlopen(f"{service_url()}/health", timeout=2.0) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def _maybe_start_sidecar() -> None:
    """自动拉起 sidecar（分离进程）：独立 venv 存在 + 端口无服务时才尝试一次/进程。"""
    if _state["start_tried"]:
        return
    _state["start_tried"] = True
    py = _svc_python()
    if not py:
        logger.info("embed sidecar 未配置（%s 不存在）——向量通道保持 FTS-only", _svc_home())
        return
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "scripts", "embed_server.py")
    if not os.path.isfile(script):
        return
    log = open(os.path.join(_svc_home(), "svc.log"), "ab")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [py, script, "--port", str(service_url().rsplit(":", 1)[-1]),
         "--device", str(_vec_cfg("device", "auto")),
         "--model-dir", os.path.join(_svc_home(), "model")],
        stdout=log, stderr=log, close_fds=True, creationflags=flags,
        cwd=_svc_home())
    logger.info("embed sidecar 已拉起（%s）", service_url())


def ready() -> bool:
    """向量通道是否可用（enabled + sidecar 健康，缓存 30s；自动拉起 sidecar）。"""
    if not enabled():
        return False
    now = time.time()
    if now - _state["health_ts"] < _HEALTH_TTL:
        return bool(_state["health_ok"])
    ok = _health()
    if not ok and not _state["start_tried"]:
        _maybe_start_sidecar()
        deadline = time.time() + 120.0
        while time.time() < deadline:
            if _health():
                ok = True
                break
            time.sleep(2.0)
    _state.update({"health_ts": now, "health_ok": ok})
    if not ok:
        logger.info("embed sidecar 不可用——向量通道降级 FTS-only")
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
