"""U3/A1/A2：子任务异常分类与自愈判定（由 sdk_loop.py 抽出，零逻辑改动）。

集中承载 provider 服务侧错误识别，以及「上下文溢出 / 输出截断 / 工具不存在」
三类可自愈异常判定。全部只依赖通用协议语义（HTTP 状态码 / 异常类名 / 消息关键字），
不绑定任何厂商文案（内核零领域逻辑）。
"""
from __future__ import annotations

import re
from typing import Optional


# U3：provider 错误友好降级——单一判定入口。
# 主判定走 HTTP 状态码（与具体厂商无关），覆盖「服务侧可恢复」错误；
# 网络层异常无状态码，保留通用类名兜底。
# 不绑定任何厂商专属依赖或文案（内核零领域逻辑）。

# 网络/超时类异常：没有 HTTP 状态码，只能靠通用类名兜底
_NETWORK_ERROR_CLASSES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "APIConnectionTimeoutError",
})


def _provider_status_code(e: Exception) -> Optional[int]:
    """从异常中取出 HTTP 状态码（兼容直接挂在异常上或包在 .response 里）。"""
    status = getattr(e, "status_code", None)
    if not isinstance(status, int):
        resp = getattr(e, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
    return status if isinstance(status, int) else None


def classify_provider_error(e: Exception) -> Optional[str]:
    """判定异常是否为模型服务侧错误（配额/限流/认证/权限/服务端/网络）。

    命中返回友好降级文案；非服务类异常（如 400 请求参数错误）返回 None，走原有失败逻辑。
    主判定依赖 HTTP 状态码（厂商无关），网络层异常无状态码时退化为通用类名兜底。
    """
    if e is None:
        return None
    status = _provider_status_code(e)
    if status is not None:
        # 402 = 配额/余额不足，文案独立
        if status == 402:
            return ("模型服务配额/余额不足，任务已暂停；"
                    "请检查服务配额后重新发送消息继续。")
        # 431 = 请求头字段过大：上游偶发或请求体积过大，归「服务侧可恢复」并给明确提示
        if status == 431:
            return ("模型服务请求头过大(431)，任务已暂停；"
                    "请检查请求体积（系统提示/工具列表/历史）或稍后重试。")
        # 其余「服务侧可恢复」状态码统一归「暂时不可用」
        if status in (401, 403, 408, 429) or 500 <= status <= 599:
            return "模型服务暂时不可用（限流/网络/认证），任务已暂停；请稍后重试。"
        # 其他状态码（如 400 请求错误）不视为可降级的服务异常
        return None
    # 无状态码：网络/超时层兜底；空 choices 型 ModelBehaviorError 也归此类（瞬时 provider 抖动）
    if type(e).__name__ in _NETWORK_ERROR_CLASSES:
        return "模型服务暂时不可用（网络/超时），任务已暂停；请稍后重试。"
    if _is_empty_choices(e):
        return "模型服务返回空响应(no choices)，任务已暂停；请稍后重试。"
    if _is_malformed_response(e):
        return ("模型服务返回非标准响应体（疑似网关/内容过滤软拦截），任务已暂停；"
                "请调整输入内容、缩小上下文或更换模型端点后重试。")
    return None


def is_transient_network_error(e: Exception) -> bool:
    """3.6：判定异常是否为「网络/超时」类（无 HTTP 状态码、纯连接/超时抖动），可瞬时重试。

    与 ``classify_provider_error`` 区分：本函数只认无状态码的网络层异常
    （``APIConnectionError`` / ``APITimeoutError`` / ``APIConnectionTimeoutError``），
    命中后由 Runner 外层做有限次指数退避重试，期间任务保持 running；重试耗尽才暂停。
    带 HTTP 状态码的「服务侧可恢复」错误（429/5xx 等）不在此范围，走原分支直接暂停。
    仅依赖通用类名兜底，不绑定任何厂商文案（内核零领域逻辑）。
    """
    if e is None:
        return False
    if _provider_status_code(e) is not None:
        return False
    return type(e).__name__ in _NETWORK_ERROR_CLASSES


def _is_empty_choices(e: Exception) -> bool:
    """U3 扩展：判定「provider 返回空 choices」型 ModelBehaviorError。

    SDK 在 ChatCompletion 响应无 ``choices``（上游网关偶发返回空体/空数组）时抛此异常，
    属瞬时 provider 抖动，可退避重试恢复。仅依赖通用类名 + 消息关键字，不绑厂商文案
    （内核零领域逻辑）。
    """
    if e is None:
        return False
    if type(e).__name__ != "ModelBehaviorError":
        return False
    return "no choices" in str(e).lower()


def _is_malformed_response(e: Exception) -> bool:
    """网关软拦截型 ModelBehaviorError（sdk_model._MalformedResponseGuard 产出）。

    上游网关 HTTP 200 但返回非标准体（裸字符串等，常见于内容过滤软拦截），
    SDK 因此崩 AttributeError 被卫兵转换。属**确定性**拒绝（重发同样内容无意义），
    不进入 is_retryable_provider_glitch 的退避重试集，直接按 provider 侧错误暂停。
    """
    if e is None:
        return False
    if type(e).__name__ != "ModelBehaviorError":
        return False
    return "非标准响应体" in str(e)


def is_retryable_provider_glitch(e: Exception) -> bool:
    """U3 扩展：可退避重试的瞬时 provider 抖动——网络/超时层 + 空 choices。

    与 ``is_transient_network_error`` 区分：本函数在其基础上额外纳入「空 choices」
    （``ModelBehaviorError`` 子类、无 HTTP 状态码、纯 provider 侧偶发空响应）。
    命中后由 Runner 外层做有限次指数退避重试，期间任务保持 running；重试耗尽才暂停。
    仅依赖通用协议语义，不绑定任何厂商文案（内核零领域逻辑）。
    """
    return is_transient_network_error(e) or _is_empty_choices(e)


def _is_context_overflow(e: Exception) -> bool:
    """T3.3（U1d）：判定是否为「上下文/请求体超限」异常。

    仅依赖全生态通用的协议语义，不绑定任何厂商文案：
      - 异常类名：``BadRequestError`` / ``APIStatusError``（请求体被拒的通用类型）
      - HTTP 状态码：``400`` / ``413``（Bad Request / Payload Too Large）
      - 异常信息含 ``context`` 关键字（上下文/窗口超限的通用表述）
    """
    if e is None:
        return False
    if type(e).__name__ not in ("BadRequestError", "APIStatusError"):
        return False
    status = _provider_status_code(e)
    if status not in (400, 413):
        return False
    return "context" in str(e).lower()


def _is_length_truncation(e: Exception) -> bool:
    """F3.2（P4）：判定是否为「输出被模型 max_tokens 截断（finish_reason='length'）且空产出」。

    仅依赖通用 SDK 语义（异常类名 + 消息中的 length 信号），不绑定任何厂商文案——
    与内核红线「错误识别走通用协议语义」一致。

    注意（F3.1 结论）：SDK 仅在「截断且 content/refusal/tool_calls 全空」时才抛此异常
    （见 agents/models/openai_chatcompletions.py:330-341）；「部分产出（句中截断）」不抛异常，
    走既有工具报错自愈链路，不在此函数覆盖范围。
    """
    if e is None:
        return False
    if type(e).__name__ != "ModelBehaviorError":
        return False
    return "length" in str(e).lower()


def _is_tool_not_found(e: Exception) -> bool:
    """A1 双保险：判定「模型调用不存在的工具」型 ModelBehaviorError。

    官方通道 tool_not_found_behavior='return_error_to_model' 已让 SDK 在 run 内自纠、不再抛此异常；
    此函数仅作兜底（如其他路径/旧 SDK 仍抛），命中后追加纠正提示并限次重试，而非一刀切终止。
    仅依赖通用协议语义（类名 + 消息中的 'not found'），不绑定任何厂商文案。
    """
    if e is None:
        return False
    if type(e).__name__ != "ModelBehaviorError":
        return False
    return "not found" in str(e).lower()


def _extract_missing_tool(e: Exception) -> str:
    """从 ModelBehaviorError 消息里抠出被臆造的工具名（用于 reason/纠正提示）。

    SDK 两处文案：``Tool {qualified_name} not found in agent {name}``（turn_resolution.py）
    与 ``Tool '{tool_name}' not found.``（_default_tool_not_found_message）。
    """
    msg = str(e)
    m = re.search(r"Tool ['\"]?([^'\"]+?)['\"]?\s+not found", msg)
    if not m:
        m = re.search(r"['\"]?([\w.\-]+?)['\"]?\s+not found", msg)
    return m.group(1).strip() if m else ""
