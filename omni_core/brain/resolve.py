"""模型解析层：把 ``runtime.agents.<id>.model`` 引用解析成可喂给 LLMClient / build_sdk_model 的端点配置。

设计（doc/plans/multi-agent-redesign-2026-09-13.md §2.2 / §6.1）：

- ``llm.providers.<name>`` 定义模型端点（provider / base_url / model / api_key / ...）。
- ``runtime.agents.<id>.model`` 引用某个 provider，实现「定义 ↔ 引用分离」：
  一个 provider 可被多个 agent 复用；本地 / 在线只是 provider 的 base_url 不同，
  不再需要 ``runtime.mode`` 三态开关。
- 旧 schema（顶层 ``brain`` / ``runtime.executor``）保留读取并映射，过渡期不破坏既有配置。
- 本模块零业务 / 场景逻辑，只做「配置 → 端点 dict」的映射，符合内核红线。
"""
from typing import Any, Dict

# provider / agent 配置里与「模型端点」相关的字段（直接喂 LLMClient / build_sdk_model）
_ENDPOINT_KEYS = (
    "provider",
    "base_url",
    "model",
    "api_key",
    "api_key_env",
    "capabilities",
    "request",
    "long_task",
    # T3.2：模型级上下文上限（0=关闭 Model 适配层粘性压缩）
    "maxInputTokens",
)

# 旧 schema 回退时使用的 agent 别名
_LEGACY_EXECUTOR_ALIASES = ("worker", "executor")


def _providers(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return (cfg.get("llm") or {}).get("providers") or {}


def resolve_agent_model(cfg: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    """解析某个 agent 的模型端点配置，返回兼容 LLMClient / build_sdk_model 的 dict。

    解析优先级（设计 §2.2 / §6.1 兼容段）：
    1. 新 schema：``runtime.agents.<agent_id>`` 存在且 ``model`` 指向 ``llm.providers`` 中
       某个已定义 provider → 合并 agent 自身字段 + provider 端点，剔除 ``model`` 引用。
       新 schema 下：agent 未显式 ``enabled: false`` 即视为启用（有 provider 引用即生效）。
    2. 旧 schema 回退（过渡期，不破坏既有配置）：
         - ``agent_id == "main"``                → 顶层 ``brain``
         - ``agent_id in ("worker","executor")``  → ``runtime.executor``
    3. 都不存在 → 返回空 dict（调用方据此退化为「主模型兼任」，即单 agent 模式）。
    """
    cfg = cfg or {}
    agents = (cfg.get("runtime") or {}).get("agents") or {}
    agent = agents.get(agent_id) or {}

    ref = agent.get("model")
    prov = _providers(cfg).get(ref) if ref else None
    if prov is not None:
        out: Dict[str, Any] = {}
        # 先放 agent 自身字段（enabled / tools / max_steps / dispatchable 等）
        for k, v in agent.items():
            if k != "model":
                out[k] = v
        # 新 schema：agent 未显式关闭即视为启用（区别于旧 schema 需显式 enabled:true）
        if "enabled" not in out:
            out["enabled"] = True
        # 再用 provider 端点覆盖模型相关字段
        for k in _ENDPOINT_KEYS:
            if k in prov:
                out[k] = prov[k]
        return out

    # 旧 schema 回退
    if agent_id == "main":
        return dict(cfg.get("brain") or {})
    if agent_id in _LEGACY_EXECUTOR_ALIASES:
        return dict((cfg.get("runtime") or {}).get("executor") or {})
    return dict(agent)


def resolve_all_agent_models(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """解析配置里声明的所有 agent 模型（便于前端枚举 / 测试）。

    仅返回「新 schema 下确实声明了 runtime.agents」的解析结果；
    若未声明任何 agent，返回空 dict（表示走旧 schema / 单 agent 默认）。
    """
    cfg = cfg or {}
    agents = (cfg.get("runtime") or {}).get("agents") or {}
    if not agents:
        return {}
    return {aid: resolve_agent_model(cfg, aid) for aid in agents}
