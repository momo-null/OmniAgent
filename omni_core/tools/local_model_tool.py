"""M9：本地模型作为**工具**暴露（设计 doc/plans/multi-agent-redesign-2026-09-13.md §6.2）。

要点：
- 本地模型不再由代码写死成某个角色（"执行器"/"视觉通道"），而是一个与其它能力
  **完全平级的工具**，由主 LLM 看完边界声明后自己决定要不要派发。
- 边界（适合 / 不适合）来自配置 ``llm.local_as_tool.boundary``；本文件只有通用模板，
  不含任何领域词（红线 3/4：能力=工具，领域数据走配置）。
- 视觉派发沿用既有 ``vision_describe``（它本来就是本地 VLM 的感知通道），
  这里不再新增一个重复的 local_vision 工具；``capabilities.vision`` 只用于在
  工具描述里说明本地模型是否具备视觉能力。

配置示例（config.yaml）::

    llm:
      local_as_tool:
        enabled: true
        base_url: http://127.0.0.1:8085
        model: <本地模型名，由 Web 设置面板填写>
        capabilities: {vision: true}
        boundary:
          good_for: ["短指令解析", "低成本重复推理"]
          not_for:  ["长链规划", "需要大上下文的汇总"]
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any, Dict, List, Optional

from agents.tool import function_tool as sdk_function_tool

from omni_core.tools.base import ToolPlugin, register_tool, unregister_tool, _tool_error_result
from omni_core.brain.llm import LLMClient, model_health_ok

TOOL_NAME = "local_infer"
GROUP = "local_model"

# 未配置 boundary 时的中性兜底（不含任何领域词）
_DEFAULT_GOOD_FOR: List[str] = ["短小明确的单次推理", "低成本重复推理"]
_DEFAULT_NOT_FOR: List[str] = ["长链规划", "需要大上下文的汇总", "需要最新知识的判断"]

_client: Any = None
_cfg: Dict[str, Any] = {}


def _as_list(v: Any, fallback: List[str]) -> List[str]:
    if isinstance(v, str):
        v = [v]
    items = [str(x).strip() for x in (v or []) if str(x).strip()]
    return items or list(fallback)


def render_description(cfg: Optional[dict] = None) -> str:
    """渲染工具描述：通用模板 + 配置声明的边界（代码零领域词）。"""
    cfg = cfg or {}
    good = _as_list((cfg.get("boundary") or {}).get("good_for"), _DEFAULT_GOOD_FOR)
    bad = _as_list((cfg.get("boundary") or {}).get("not_for"), _DEFAULT_NOT_FOR)
    lines = [
        "把一次推理任务派发给本地模型（成本低、延迟低，但能力弱于你自己）。",
        "本地模型看不到对话上下文，请把任务写成自包含的完整描述。",
        f"适合：{'、'.join(good)}。",
        f"不适合：{'、'.join(bad)}——这些请自己做。",
    ]
    if bool((cfg.get("capabilities") or {}).get("vision")):
        lines.append("本地模型具备视觉能力：需要看图时请用 vision_describe（走同一本地模型）。")
    return " ".join(lines)


def _local_infer(prompt: str) -> Dict[str, Any]:
    """把一次推理任务交给本地模型并返回它的回答。

    Args:
        prompt: 交给本地模型的完整任务描述（自包含，本地模型看不到对话上下文）
    """
    if _client is None:
        return {"ok": False, "error": "本地模型未配置或未启用"}
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt 为空"}
    try:
        reply = _client.chat(
            [{"role": "user", "content": prompt}],
            tools=None,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    text = getattr(reply, "content", "") or ""
    return {"ok": True, "text": str(text).strip(), "model": _cfg.get("model", "")}


def configure(cfg: Optional[dict], client: Any = None, on_debug: Any = None) -> bool:
    """按 ``llm.local_as_tool`` 注入本地模型，并（重新）登记工具。

    Args:
        cfg: 配置段；``enabled`` 为假则注销工具。
        client: 可直接注入客户端（测试用）；缺省用 ``LLMClient(cfg)`` 构造。
        on_debug: 可选调试回调 ``(kind, payload) -> None``，用于把可用性告警推到前端日志。

    Returns:
        工具是否已注册。
    """
    global _client, _cfg
    cfg = dict(cfg or {})
    unregister_tool(TOOL_NAME)
    _client = None
    _cfg = cfg
    if not cfg.get("enabled"):
        return False

    # M-fix：本地端点可用性预检（仅本机端点）。不可达则注销工具并告警，
    # 让大脑「知道」本地模型不可用、自行用主模型完成任务，避免空转调用烧步数。
    if model_health_ok(cfg.get("base_url")) is False:
        if on_debug:
            try:
                on_debug("local_model_unavailable", {
                    "title": "本地模型工具不可用（端点不可达）",
                    "model": cfg.get("model", ""),
                    "base_url": cfg.get("base_url", ""),
                    "reason": "本地模型未启动或端口不符，工具已从可用列表移除；大脑将自行完成任务",
                })
            except Exception:
                pass
        return False

    if client is None:
        try:
            # 本地端点通常没有 key：LLMClient 要求 api_key 字段存在，补占位值
            if not cfg.get("api_key") and not os.environ.get(
                cfg.get("api_key_env", "OMNI_EXECUTOR_API_KEY") or ""
            ):
                cfg["api_key"] = "local-no-key"
            client = LLMClient(cfg)
        except Exception:
            return False
    _client = client

    # schema 由 SDK 从签名 + docstring 产出，描述再按配置渲染（边界来自配置）
    base = sdk_function_tool(
        _local_infer,
        name_override=TOOL_NAME,
        strict_mode=False,
        use_docstring_info=True,
        failure_error_function=_tool_error_result,
    )
    tool = dataclasses.replace(base, description=render_description(cfg))
    register_tool(ToolPlugin(
        name=TOOL_NAME,
        tool=tool,
        group=GROUP,
        meta={"model": cfg.get("model", ""), "source_cfg": "llm.local_as_tool"},
    ))
    return True


def configure_from_app_config(app_cfg: Optional[dict], on_debug: Any = None) -> bool:
    """从整份应用配置里定位本地模型工具配置并（重新）登记。

    优先 ``llm.local_as_tool``；未配置时回退 ``runtime.executor`` 的端点。
    tool_loop 与只读枚举接口（``GET /api/runtime/tools``）都走这里，避免两处各写一套。
    """
    app_cfg = app_cfg or {}
    rt = app_cfg.get("runtime") or {}
    cfg = (app_cfg.get("llm") or {}).get("local_as_tool") or {}
    if not cfg:
        # 回退：优先旧 runtime.executor 端点（过渡期），无则经 resolver 取新 schema 的 worker 端点
        ex = rt.get("executor") or {}
        if not ex:
            try:
                from omni_core.brain.resolve import resolve_agent_model
                ex = resolve_agent_model(app_cfg, "worker")
            except Exception:
                ex = {}
        cfg = {**ex, "enabled": bool(ex.get("enabled"))}
    return configure(cfg, on_debug=on_debug)


def is_registered() -> bool:
    from omni_core.tools.base import TOOL_REGISTRY

    return TOOL_NAME in TOOL_REGISTRY
