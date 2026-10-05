"""OmniAgent 后端单一入口

创建唯一的 FastAPI 应用，挂载 5 个 API router，并配置 CORS。
启动方式: ``python -m backend.server`` 或 ``uvicorn backend.server:app``。

注意：本入口**不**在启动时自动拉起任何模型；但会探测并接管
因后端异常退出而残留的 llama-server 孤儿进程（见 lifespan）。
"""
from contextlib import asynccontextmanager
from typing import Dict, Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
from utils import get_logger

logger = get_logger("backend_server")

# NOTE: router_agent(X2 AgentControl) 已随 X3 两层架构重构移除
#       （归档见 git 历史）。X3 agent 运行时由
#       router_runtime 提供（SSE + run/skill），与 models/llm/system 统一挂载。
from backend.api.router_models import router as models_router
from backend.api.router_runtime import router as runtime_router
from backend.api.router_llm import router as llm_router
from backend.api.router_system import router as system_router
from backend.api.routers.debug_api import router as debug_router
from backend.api.router_settings import router as settings_router

# ── 服务配置 ──────────────────────────────────────────────
_server_cfg = (config.load_config() or {}).get("server", {}) or {}
HOST: str = _server_cfg.get("host", "127.0.0.1")
PORT: int = int(_server_cfg.get("port", 8000))

# P0.3 安全边界：仅 loopback 默认可绑；非回环地址必须配置 auth_token，
# 否则拒绝启动（CORS 不是鉴权，管理/运行控制接口会暴露密钥面）。


def _require_auth_for_nonloopback() -> None:
    """非回环绑定但缺认证 token 时拒绝启动（安全门）。"""
    host = (HOST or "127.0.0.1").strip().lower()
    is_loopback = host in {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}
    if is_loopback:
        return
    # 允许显式"全网卡但需认证"的 0.0.0.0 / 空串（视为非回环，需 token）
    if not _server_cfg.get("auth_token"):
        raise SystemExit(
            f"[安全] 服务绑定到非回环地址 {HOST!r}，但 server.auth_token 未配置。\n"
            f"        若需对外暴露，请在 config.yaml 的 server 段设置 auth_token；"
            f"否则请将 host 改回 127.0.0.1。"
        )


_require_auth_for_nonloopback()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # B：启动接管遗留的 llama-server 孤儿进程（后端异常退出后仍在跑的实例）。
    # 仅纳入管理视野、不自动杀掉（停止以用户操作为准）；用户可在前端
    # 「强制释放全部」或运行 scripts/kill_llama.py 释放。
    try:
        from backend.api.deps import get_model_hub
        mgr = get_model_hub()
        orphans = mgr.detect_and_adopt_orphans()
        if orphans:
            logger.warning(
                "启动接管 %d 个遗留 llama-server 孤儿: %s",
                len(orphans),
                ", ".join(f"{o['name']}(pid={o['pid']},port={o['port']})" for o in orphans),
            )
    except Exception as e:  # 探测失败不应阻断启动
        logger.warning("启动探测孤儿 llama-server 失败（已忽略）: %s", e)
    # 预建全局目录 ~/.omniagent（skills/memory/projects/tasks），保证用户设置目录稳定存在。
    # 文档约定「服务启动时调用」，此前遗漏；缺失会导致首次保存前目录不存在。
    try:
        from omni_core.local import runtime_paths
        runtime_paths.ensure_global_dirs()
        logger.info("全局数据根: %s", runtime_paths.global_omni())
    except Exception as e:  # 建目录失败不应阻断启动
        logger.warning("启动预建全局目录失败（已忽略）: %s", e)
    # 栈看门狗：独立守护线程定期写「RSS + 全线程栈」到 ~/.omniagent/logs/stackwatch*.log。
    # 用于事后取证——进程被拖垮（HTTP 全挂）时诊断端点也访问不了，只能靠落盘快照。
    # 关闭：OMNI_STACK_WATCH=0
    try:
        from backend.services.stack_watch import start_stack_watch
        if start_stack_watch():
            logger.info("栈看门狗已启动（~/.omniagent/logs/stackwatch.log）")
    except Exception as e:  # 看门狗失败不阻断启动
        logger.warning("启动栈看门狗失败（已忽略）: %s", e)
    # S2 审批后端：注册 SSE sink（未注册时内核回落 AutoDenySink，fail-closed）
    try:
        from backend.services.approvals import install_approval_sink
        install_approval_sink()
        logger.info("S2 审批 sink 已注册（SSE 推卡 + REST 决议）")
    except Exception as e:  # 注册失败不阻断启动（回落 AutoDeny，安全侧）
        logger.warning("注册 S2 审批 sink 失败（将回落 AutoDeny）: %s", e)
    yield
    # P1 工具插件：进程退出前依次调用各插件 shutdown()（异常吞掉，不阻断退出）。
    try:
        from omni_core.tools.loader import shutdown_plugins
        shutdown_plugins()
    except Exception as e:  # 插件清理失败不应阻断退出
        logger.warning("插件 shutdown 失败（已忽略）: %s", e)


app = FastAPI(title="OmniAgent Backend", version="1.0.0", lifespan=lifespan)


# 统一把 ValueError（标识符校验失败等）转成 422
@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)


# CORS：允许 web 前端（dev: vite 5173）跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 挂载路由 ──────────────────────────────────────────────
app.include_router(models_router)
app.include_router(runtime_router)
app.include_router(llm_router)
app.include_router(system_router)
app.include_router(settings_router)
# ── Debug 模块挂载（可整体删除：删本行 + backend/api/routers/debug_api.py +
#    前端 web/src/components/DebugPanel.tsx 与 web/src/api/debugApi.ts） ──
app.include_router(debug_router)


@app.get("/")
async def root() -> Dict[str, Any]:
    """根路径信息"""
    return {"service": "OmniAgent Backend", "version": "1.0.0", "status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.server:app", host=HOST, port=PORT, reload=False)
