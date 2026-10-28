"""Loop 组件池（M1 常驻化）：把 run 之间可复用的"重对象"按配置指纹缓存。

为什么需要它（实测，2026-10-28）：
- MCP 每轮重新 ``connect``：单 server 实测 **~950ms**（spawn 子进程 + 握手），
  而 run 之间复用同一批已连接对象只要 **2.8ms**。这 1s 就是"接线税"的全部来源。
- ``LLMClient`` 每轮新建 → AsyncOpenAI/httpx 连接池重建 → 每轮新做 TCP/TLS 握手，
  直接抬高首事件延迟（语音体验的硬指标）。
- executor 槽健康探测每轮同步执行（``model_health_ok`` 单次最长 2s 超时）。

三条不变量（改这个文件前先读）：
1. **池里只放无 per-run 状态的组件**（连接 / 已包装的工具清单）。持有 per-run 状态
   的对象（wrapper 链、gate、Agent、ToolLoop 自身）一律每次新建——同配置并发 run
   时池对象被共享，任何可变状态都会串味。

   特别说明 **Agent 实例为何不复用**（原计划想缓存它）：Agent 的 ``tools`` 里含
   ``build_meta_tools`` 产出的**每 run 绑定**工具（task_done/verify/record/dispatch，
   闭包捕获当轮的 gate / SubtaskState），``instructions`` 又含每 run 的 AGENTS.md +
   记忆稳定段。要缓存 Agent 就得让这些全部按 run 重建——那等于没缓存。而 Agent
   构造本身是纯 CPU 毫秒级（实测 3 次 ToolLoop 构造共 239ms，其中首轮 233ms 是插件
   与 MCP 首次装载）。所以 Agent 每轮新建，池只负责传输层与连接。
2. **任何缓存路径失败都退化为重新构造**，池异常绝不影响主链路（记忆/工具层同款纪律）。
3. 配置指纹变（模型端点 / MCP / 工具集合）即弃旧重建，旧 MCP 连接主动清理。

指纹全部取自**内容**而非 id，因此"配置热更 → 池正确失效"是天然成立的。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from utils import get_logger

logger = get_logger("loop_pool")

_lock = threading.RLock()

# --- MCP 常驻连接 ---
_mcp_key: str = ""
_mcp_servers: List[Any] = []

# --- 共享 AsyncOpenAI 传输（连接保活） ---
_openai_cache: Dict[str, Any] = {}

# --- 已包装的工具清单（FunctionTool） ---
_sdk_tools_key: Tuple[str, ...] = ()
_sdk_tools_val: List[Any] = []

# --- 健康探测结果缓存（base_url -> (ok, ts)） ---
_health_cache: Dict[str, Tuple[Optional[bool], float]] = {}
HEALTH_TTL_SEC = 120.0

# 统计（诊断面：证明池真的命中，而不是"看起来快了"）
_stats = {"mcp_hit": 0, "mcp_build": 0, "openai_hit": 0, "openai_build": 0,
          "health_hit": 0, "tools_hit": 0}


def stats() -> Dict[str, int]:
    """池命中统计（只读；供 /api/runtime/memory 之类诊断面展示）。"""
    with _lock:
        return dict(_stats)


def _fp(obj: Any) -> str:
    """内容指纹（keys 排序，非 JSON 可序列化退化为 repr）。"""
    try:
        s = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        s = repr(obj)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- MCP 常驻连接

def mcp_servers(mcp_cfg: Dict[str, Any]) -> List[Any]:
    """按 mcp.json 内容缓存 server 对象；同配置跨 run 复用同批**已连接**实例。

    ``connect`` 仍由 ``sdk_loop._ensure_mcp_connected`` 在 run 内触发：它对
    ``session`` 非空的 server 直接复用（2~3ms），只对死掉的 server 重连——
    因此"断连自愈"语义不变，只是把 1s 的重复握手省掉。

    ``enabled=false`` 或配置为空 → 返回 ``[]``（调用方零特判）。
    """
    global _mcp_key, _mcp_servers
    try:
        if not (mcp_cfg or {}).get("enabled"):
            return []
        key = _fp(mcp_cfg)
        with _lock:
            if key == _mcp_key and _mcp_servers:
                _stats["mcp_hit"] += 1
                return list(_mcp_servers)
            old, _mcp_key = _mcp_servers, key
        _cleanup_servers(old)          # 配置变了：旧连接主动断开，不泄漏子进程
        from omni_core.tools import build_mcp_servers

        built = build_mcp_servers((mcp_cfg or {}).get("servers"))
        with _lock:
            _mcp_servers = list(built)
            _stats["mcp_build"] += 1
        return list(built)
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP 池构建失败，本轮无 MCP：%s: %s", type(e).__name__, e)
        return []


def prime_mcp(mcp_cfg: Optional[Dict[str, Any]] = None) -> None:
    """启动期预热：后台线程里把 MCP 连一次（run 路径零连接成本）。

    plan §2 验收「派发→请求 <100ms」的前提是首轮也不付 1s 连接税——只把对象池化
    还不够，**首个 run** 仍会付 connect。这里在服务启动时就把连接建好；
    ``_ensure_mcp_connected`` 看到 session 非空直接复用（2~3ms）。
    阻塞在后台线程，绝不拖慢启动。
    """
    def _warm():
        try:
            import config as _config

            cfg = mcp_cfg or _config.load_mcp_config()
            servers = mcp_servers(cfg)
            if servers:
                from omni_core.brain.sdk_loop import _ensure_mcp_connected

                t0 = time.time()
                got = _ensure_mcp_connected(list(servers))
                try:
                    print(f"[loop_pool] MCP 启动预热完成：{len(got)}/{len(servers)} 可用，"
                          f"耗时 {(time.time() - t0) * 1000:.0f}ms", flush=True)
                except Exception:
                    pass
            # 插件预热：首个 ToolLoop 构造要花 ~230ms 装插件（imports + 注册）；
            # 提前在启动期做一次，run 路径只剩几 ms 的重复注册（load_plugins 幂等）。
            try:
                from omni_core.tools.loader import PluginContext, load_plugins

                full = _config.load_config() or {}
                load_plugins(full, ctx=PluginContext(config=full, wired=True))
                try:
                    print("[loop_pool] 插件启动预热完成", flush=True)
                except Exception:
                    pass
            except Exception as e:  # noqa: BLE001
                logger.warning("插件启动预热失败（不影响运行）：%s: %s",
                               type(e).__name__, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("MCP 启动预热失败（不影响运行）：%s: %s", type(e).__name__, e)

    threading.Thread(target=_warm, name="loop-pool-prime", daemon=True).start()


def _cleanup_servers(servers: List[Any]) -> None:
    """断开旧 MCP 连接（async cleanup 走共享常驻 loop；失败只记日志）。"""
    if not servers:
        return
    try:
        from omni_core.async_bridge import run_async

        for s in servers:
            try:
                run_async(s.cleanup(), timeout=5.0)
            except Exception:
                pass
    except Exception:
        pass
    try:
        print("[loop_pool] 已清理旧 MCP 连接", flush=True)
    except Exception:
        pass


# ------------------------------------------- 共享 AsyncOpenAI 传输（连接保活）

def shared_openai(cfg: Dict[str, Any], timeout: Optional[float] = None) -> Any:
    """按端点 + key + 超时 + 代理缓存 ``AsyncOpenAI``（httpx 连接池复用）。

    返回 ``None`` 表示不池化（调用方自行新建）：key 缺失 / 构造失败 / 代理配置
    无法安全共享时都走这条路，行为与"没有池"完全一致。
    """
    try:
        base_url = str((cfg or {}).get("base_url") or "").rstrip("/")
        if not base_url:
            return None
        api_key = str((cfg or {}).get("api_key") or "").strip()
        if not api_key:
            import os

            api_key = os.environ.get((cfg or {}).get("api_key_env", "OMNI_BRAIN_API_KEY"), "")
        if not api_key:
            return None
        proxy = str((cfg or {}).get("proxy") or "").strip()
        key = _fp([base_url, hashlib.sha256(api_key.encode()).hexdigest()[:16],
                   timeout or 0, proxy])
        with _lock:
            hit = _openai_cache.get(key)
            if hit is not None:
                _stats["openai_hit"] += 1
                return hit
        import httpx
        from openai import AsyncOpenAI

        kwargs: Dict[str, Any] = {"base_url": base_url, "api_key": api_key}
        if timeout:
            kwargs["timeout"] = timeout
        if proxy:
            kwargs["http_client"] = httpx.AsyncClient(proxy=proxy)
        client = AsyncOpenAI(**kwargs)
        with _lock:
            _openai_cache[key] = client
            _stats["openai_build"] += 1
        return client
    except Exception as e:  # noqa: BLE001
        logger.warning("共享 AsyncOpenAI 不可用，回退逐轮新建：%s: %s", type(e).__name__, e)
        return None


# ------------------------------------------------------ 工具清单（包装结果缓存）

def sdk_tools(registry: Any) -> List[Any]:
    """缓存 ``registry.sdk_tools()`` 的包装结果（按工具名集合失效）。

    工具对象本身在注册表里长期存活，这里省的是每轮重新枚举 + 重新包装的
    CPU 开销（以及未来工具数量变大后的可观测成本）。返回**副本**，调用方可自由
    拼接自己的元工具。
    """
    global _sdk_tools_key, _sdk_tools_val
    try:
        names = tuple(sorted(registry.names()))
        with _lock:
            if names == _sdk_tools_key and _sdk_tools_val:
                _stats["tools_hit"] += 1
                return list(_sdk_tools_val)
        val = list(registry.sdk_tools())
        with _lock:
            _sdk_tools_key, _sdk_tools_val = names, val
        return list(val)
    except Exception:  # noqa: BLE001
        return list(registry.sdk_tools())


# -------------------------------------------------------------- 健康探测（前移）

def health_ok(base_url: Optional[str], ttl: float = HEALTH_TTL_SEC) -> Optional[bool]:
    """``model_health_ok`` 的 TTL 缓存版：run 路径零重复探测。

    语义与 ``model_health_ok`` 完全一致（True/False/None），只是同一端点在一个
    TTL 窗口内只探一次。端点掉了最多晚 ``ttl`` 秒被发现——这比"每轮同步等 2s
    超时"划算得多（plan §2：健康探测移到启动 / 配置变更时）。
    """
    if not base_url:
        return None
    bu = str(base_url).strip()
    if "127.0.0.1" not in bu and "localhost" not in bu:
        return None      # 在线 provider 不做预检（与 model_health_ok 同口径）
    now = time.time()
    with _lock:
        hit = _health_cache.get(bu)
        if hit is not None and (now - hit[1]) < ttl:
            _stats["health_hit"] += 1
            return hit[0]
    from omni_core.brain.llm import model_health_ok

    try:
        ok = model_health_ok(bu)
    except Exception:
        ok = None
    with _lock:
        _health_cache[bu] = (ok, now)
    return ok


# ------------------------------------------------------------------- 生命周期

def clear() -> None:
    """清空所有缓存（测试 / 显式热更用）。MCP 连接一并断开。"""
    global _mcp_key, _mcp_servers, _sdk_tools_key, _sdk_tools_val
    with _lock:
        old = _mcp_servers
        _mcp_key, _mcp_servers = "", []
        _sdk_tools_key, _sdk_tools_val = (), []
        _openai_cache.clear()
        _health_cache.clear()
        for k in list(_stats):
            _stats[k] = 0
    _cleanup_servers(old)


def cleanup() -> None:
    """进程退出：断开常驻 MCP 连接（幂等）。"""
    global _mcp_key, _mcp_servers
    with _lock:
        old = _mcp_servers
        _mcp_key, _mcp_servers = "", []
    _cleanup_servers(old)


atexit.register(cleanup)
