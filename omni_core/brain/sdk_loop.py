"""M5：由 OpenAI Agents SDK `Runner` 驱动的子任务循环（替代手搓 `_run_inner`）。

职责边界（设计 §3.5 / §5.0）：
- **循环归框架**：ReAct 单步、function-call 序列化、assistant/tool 消息回填、
  工具派发（含外部 MCP）全部由 `Runner.run` 接管，内核不再手搓。
- **L2 归我们**：收尾门控（task_done 须先 verify）、升级判定（escalate /
  no_confidence / verify 连续失败 / 墙钟 / 步数预算）、世界模型与轨迹落盘，
  通过「元工具 + RunHooks + 分块 max_turns」挂在框架外面。

分块策略：每次 `Runner.run(max_turns=chunk)` 让框架连跑若干步，块与块之间
由 L2 检查终止/升级条件——既吃到框架的循环，又保留 L2 的自治权（C4）。

模型无关：Model 由 `sdk_model` / `chat_bridge` 提供（openai-compatible 端点）。
"""
import copy
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from agents import Agent, Model, RunHooks, Runner, RunConfig
from agents.exceptions import MaxTurnsExceeded
from agents import StopAtTools
from agents.tool import function_tool as sdk_function_tool
from agents.tool import FunctionTool

from omni_core.async_bridge import run_async, create_task, wait_task
from omni_core.brain.chat_bridge import build_chat_bridge_model
import config  # 全局配置（runtime.chunk_turns 可覆盖默认分块大小）

# U3/A1/A2：异常分类与自愈判定已抽到 errors.py；此处 re-export 保持原调用点
# （run_subtask_sdk 内裸名调用）与外部 `from omni_core.brain.sdk_loop import ...` 零改动。
from omni_core.brain.errors import (  # noqa: F401
    _NETWORK_ERROR_CLASSES,
    _extract_missing_tool,
    _is_context_overflow,
    _is_length_truncation,
    _is_tool_not_found,
    _provider_status_code,
    classify_provider_error,
)


@dataclass
class SubtaskState:
    """一次子任务运行的 L2 状态（由元工具与钩子写入，runner 读取决策）。"""
    done: bool = False
    done_reason: str = ""
    escalated: bool = False
    escalate_reason: str = ""
    verify_fail: int = 0
    no_confidence: bool = False
    steps: int = 0
    # M7：主 agent 调 dispatch 元工具产出的派发计划（子任务列表）
    dispatch_plan: List[Dict[str, str]] = field(default_factory=list)
    # M8：本轮是否已注入过预算提示（只提示一次，避免反复打扰）
    budget_hinted: bool = False
    # F1.2：模型调用计数（on_llm_end 每收到一次模型响应 +1，跨块累计）
    llm_calls: int = 0
    # F1.2：repeat-guard 累计失败次数（模型可自愈的参数错误等，供 retry_count 接通）
    repeat_failures: int = 0


# --- M8 长任务：预算感知提示 -------------------------------------------------
def build_budget_hint(used: int, total: int) -> str:
    """陈述性预算提示（不打断当前 turn，只陈述事实 + 给出可选动作）。

    红线：不做价值判断（禁止「请尽快 / 请加速」），否则会诱导模型为收尾而
    跳过校验、降低质量。把选择权留给模型。
    """
    return (
        f"【预算提示】本任务已用 {used}/{total} 步。"
        f"若判断无法在剩余预算内达成，可调用 escalate 交回已完成部分；"
        f"若判断可以继续，请按原计划推进。"
    )


def parse_dispatch_items(raw: Any) -> List[Dict[str, str]]:
    """归一化 dispatch 入参（不同模型序列化习惯：JSON 字符串 / dict / 列表 / 纯文本）。

    内核零领域假设：只认 desc / done_when 两个通用字段。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        try:
            raw = json.loads(s)
        except Exception:
            return [{"desc": s, "done_when": ""}] if s else []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
                item = parsed if isinstance(parsed, dict) else {"desc": item, "done_when": ""}
            except Exception:
                item = {"desc": item, "done_when": ""}
        if isinstance(item, dict) and (item.get("desc") or item.get("done_when")):
            out.append({"desc": str(item.get("desc", "")),
                        "done_when": str(item.get("done_when", ""))})
    return out


class OmniHooks(RunHooks):
    """L2 后处理钩子（设计 §5.3）：每步工具结果 -> 世界模型 / 轨迹落盘。

    与循环解耦：钩子只被动接收 (tool_name, result)，不干预框架循环。
    另挂 on_llm_end：每次模型响应 +1 模型调用计数（F1.2 指标回填）。
    """

    def __init__(self, on_step: Optional[Callable[[str, Any], None]] = None, state: Any = None):
        self.on_step = on_step
        self._state = state

    async def on_tool_end(self, context, agent, tool, result) -> None:
        if self.on_step is None:
            return
        try:
            self.on_step(getattr(tool, "name", "?"), result)
        except Exception:
            pass

    async def on_llm_end(self, context, agent, response) -> None:
        st = self._state
        if st is None:
            return
        try:
            st.llm_calls += 1
        except Exception:
            pass


class _BudgetHintModel(Model):
    """M8 T2：包一层 Model，在步数越过阈值时把预算提示塞进**下一次** LLM 输入。

    为什么不在块与块之间注入：本版 SDK 的 `Runner.run` 在 max_turns 用尽时直接抛
    `MaxTurnsExceeded`，拿不回历史（stream_events 未接通，见 M5 遗留）；为了插提示
    而人为切块会把已跑的若干轮历史丢掉。改在 Model 适配层注入后：
    - 不打断当前轮，也不切块，历史零丢失；
    - 提示出现在模型下一次决策的输入里，时机正好是阈值步。
    """

    def __init__(self, inner: Model, state: "SubtaskState", max_steps: int, ratio: float):
        self._inner = inner
        self._state = state
        # max_steps<=0 / None：不启用预算提示（长任务无硬上限）
        self._max_steps = max_steps if max_steps and max_steps > 0 else 0
        self._threshold = max(1, int(self._max_steps * ratio)) if self._max_steps > 0 else 0

    def _hint(self) -> Optional[List[Any]]:
        st = self._state
        if st.budget_hinted or self._max_steps <= 0:
            return None
        if st.steps < self._threshold or st.steps >= self._max_steps:
            return None
        st.budget_hinted = True
        return [{"role": "user", "content": build_budget_hint(st.steps, self._max_steps)}]

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        hint = self._hint()
        if hint:
            if "input" in kwargs:
                kwargs["input"] = list(kwargs["input"]) + hint
            elif len(args) >= 2:
                args = (args[0], list(args[1]) + hint) + tuple(args[2:])
        return await self._inner.get_response(*args, **kwargs)

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        # 主路径不用流式；这里只做透传（并补上预算提示注入，与 get_response 一致）
        hint = self._hint()
        if hint:
            if "input" in kwargs:
                kwargs["input"] = list(kwargs["input"]) + hint
            elif len(args) >= 2:
                args = (args[0], list(args[1]) + hint) + tuple(args[2:])
        async for chunk in self._inner.stream_response(*args, **kwargs):
            yield chunk


# --- T3.2（U1b+）：Model 适配层粘性压缩 --------------------------------------
# 八段结构化检查点：摘要按固定维度产出要点，避免自由发挥导致的要点漂移。
_SUMMARY_SECTIONS = (
    "任务目标",
    "已完成动作",
    "关键观测与工具结果",
    "已生成/修改的产物",
    "当前状态",
    "未决问题",
    "下一步计划",
    "风险与约束",
)

SUMMARY_INSTRUCTION = (
    "请把下列上下文压缩为要点式中文摘要，严格按八个检查点分段输出"
    "（每段一行，无内容写「无」）：\n"
    + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(_SUMMARY_SECTIONS))
    + "\n要求：只保留事实与结论，不复述原文；"
    "已给出的历史摘要需与新内容合并去重；总长控制在原文的 40% 以内。"
)


def _item_text(it: Any) -> str:
    """取单个会话 item 的有效文本（与 _estimate_tokens 同口径的四字段）。"""
    if it is None:
        return ""
    if isinstance(it, dict):
        parts = [str(it.get(k) or "") for k in ("content", "name", "arguments", "output")]
    else:
        parts = [str(getattr(it, k, "") or "") for k in ("content", "name", "arguments", "output")]
    return "\n".join(p for p in parts if p)


class _CompactionModel(Model):
    """T3.2：在 Model 适配层做**粘性增量压缩**（U1b+ 核心能力）。

    为什么放在 Model 层：压缩发生在「每次模型请求之前」，能拿到本次请求的真实
    items，且不影响 SDK 原始会话状态（只改本次请求入参副本）；旧实现在 chunk
    边界（ToolLoop 层）做，无状态、每次全量重压、前缀反复变化，缓存命中差。

    粘性（sticky）：实例持有 ``summary_msg`` / ``kept_from`` 压缩位点。
    后续请求若「待压缩区间无新增内容」则直接复用历史摘要，使请求前缀逐字节稳定
    → 前缀缓存友好；只有出现新增内容时才增量重压并推进位点。

    降级：摘要报错 / 为空 / 无效（新摘要 ≥ 原文 60%，视为没压动）以及超过硬上限
    时，自动退化为纯滑窗截断 ``_hard_keep``，杜绝上下文溢出崩溃。
    """

    def __init__(
        self,
        inner: Model,
        threshold_tokens: int,
        retain_ratio: float = 0.5,
        summarize: Optional[Callable[[List[Any]], Optional[str]]] = None,
        hard_ceiling_tokens: int = 0,
        keep_rounds: int = 8,
    ):
        self._inner = inner
        self._threshold = int(threshold_tokens or 0)
        self._retain_ratio = float(retain_ratio or 0.0) or 0.5
        self._summarize = summarize
        self._hard = int(hard_ceiling_tokens or 0)
        self._keep_rounds = int(keep_rounds or 8)
        # 粘性状态：上次压缩产物与其覆盖到的位点
        self.summary_msg: Optional[Dict[str, Any]] = None
        self.kept_from: int = 0

    # --- 请求改写 -------------------------------------------------------------
    @staticmethod
    def _unpack(args: tuple, kwargs: Dict[str, Any]):
        """取出 (system_instructions, input) 并返回一个「回填 input」的闭包。"""
        if "input" in kwargs:
            def pack(items):
                return args, {**kwargs, "input": items}
            return pack, kwargs.get("system_instructions"), kwargs["input"]
        if len(args) >= 2:
            def pack(items):
                return (args[0], items) + tuple(args[2:]), kwargs
            return pack, args[0], args[1]
        return (lambda items: (args, kwargs)), None, None

    def _compact(self, system_instructions: Any, items: Any) -> Optional[List[Any]]:
        """返回本次请求应使用的 items；返回 None 表示零干预（原样透传）。"""
        if self._threshold <= 0 or self._summarize is None:
            return None
        if not isinstance(items, list) or not items:
            return None
        if _estimate_tokens(items) <= self._threshold:
            return None                                   # 低于阈值：透传、零开销
        cut = self._cut_index(items)
        summary = self._sticky_summary(system_instructions, items, cut)
        if summary is None:
            return _hard_keep(items, self._keep_rounds)   # 摘要失效：纯滑窗降级
        tail_from = max(cut, self.kept_from)
        new_items = [summary] + list(items[tail_from:])
        if self._hard and _estimate_tokens(new_items) > self._hard:
            return _hard_keep(items, self._keep_rounds)   # 硬上限兜底
        return new_items

    def _cut_index(self, items: List[Any]) -> int:
        """按保留比例算出「尾部会话」起点：从末尾累计到保留预算为止。"""
        budget = max(1, int(self._threshold * self._retain_ratio))
        acc = 0
        cut = len(items)
        for i in range(len(items) - 1, -1, -1):
            acc += _estimate_tokens([items[i]])
            cut = i
            if acc >= budget:
                break
        return cut

    def _sticky_summary(self, system_instructions: Any, items: List[Any],
                        cut: int) -> Optional[Dict[str, Any]]:
        """粘性增量摘要：无新增内容则复用历史摘要（前缀稳定）；否则增量重压。"""
        if self.summary_msg is not None:
            if cut <= self.kept_from:
                return self.summary_msg          # 无新增可压内容 → 复用，前缀逐字节一致
            delta = list(items[self.kept_from:cut])
            ctx = [self.summary_msg] + delta     # 自动合并历史摘要，避免重复内容
        else:
            delta = list(items[:cut])
            ctx = list(delta)
        # 热前缀复用：沿用上下文前缀，仅追加摘要指令（不做独立冷启动请求）
        ctx = ctx + [{"role": "user", "content": SUMMARY_INSTRUCTION}]
        text = self._call_summarize(system_instructions, ctx)
        if not self._valid(text, ctx):
            return None
        self.summary_msg = {"role": "user", "content": f"[历史压缩摘要] {str(text).strip()}"}
        self.kept_from = cut
        return self.summary_msg

    def _call_summarize(self, system_instructions: Any, ctx: List[Any]) -> Optional[str]:
        """调用摘要回调：优先带系统提示（热前缀），兼容只收 items 的旧回调。"""
        try:
            if system_instructions is not None:
                try:
                    return self._summarize(ctx, system_instructions)
                except TypeError:
                    pass
            return self._summarize(ctx)
        except Exception:
            return None

    @staticmethod
    def _valid(text: Any, ctx_items: List[Any]) -> bool:
        """摘要有效性：非空，且长度 < 原文 60%（≥ 60% 视为没压动，触发滑窗降级）。"""
        if not text or not str(text).strip():
            return False
        orig = sum(len(_item_text(i)) for i in ctx_items)
        if orig <= 0:
            return True
        return len(str(text).strip()) < 0.6 * orig

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        pack, si, items = self._unpack(args, kwargs)
        new_items = self._compact(si, items)
        if new_items is not None:
            args, kwargs = pack(new_items)
        return await self._inner.get_response(*args, **kwargs)

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        pack, si, items = self._unpack(args, kwargs)
        new_items = self._compact(si, items)
        if new_items is not None:
            args, kwargs = pack(new_items)
        async for chunk in self._inner.stream_response(*args, **kwargs):
            yield chunk


class _TailInjectModel(Model):
    """F4.2：尾部重插——把记忆块追加到「压缩后」的本次请求 input 尾部。

    注：F4.1b 起**纪律文件（AGENTS.md）不再走本路径**，改为 run 起始并入 system
    prompt（缓存命中 + 覆盖语义正确）；本包装只承载 memory 这类「增长内容」。

    关键约束（与 _CompactionModel / _BudgetHintModel 同范式）：
    - **只改本次请求入参副本**：每次都构造新的 input 列表，不写回 SDK items
      → 会话历史（items）不因注入增长；
    - **发生在压缩之后**：本包装位于压缩内层（最接近真实模型），压缩先跑、本块后追加，
      故注入内容不参与压缩、不会被摘要吞掉；
    - 无块时零干预（透传，逐字节与现状一致）。

    来源层级与字符数通过 ``on_inject`` 回调上报一次（落轨迹 metrics）。
    """

    def __init__(
        self,
        inner: Model,
        block: str,
        layers: Optional[List[str]] = None,
        on_inject: Optional[Callable[[int, List[str]], None]] = None,
    ):
        self._inner = inner
        self._block = block or ""
        self._layers = list(layers or [])
        self._on_inject = on_inject
        self._reported = False

    def _inject(self, args: tuple, kwargs: Dict[str, Any]):
        if not self._block:
            return args, kwargs
        msg = {"role": "user", "content": self._block}
        if "input" in kwargs:
            return args, {**kwargs, "input": list(kwargs["input"]) + [msg]}
        if len(args) >= 2:
            return (args[0], list(args[1]) + [msg]) + tuple(args[2:]), kwargs
        return args, kwargs

    def _report_once(self) -> None:
        if self._reported or self._on_inject is None or not self._block:
            return
        self._reported = True
        try:
            self._on_inject(len(self._block), list(self._layers))
        except Exception:
            pass

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        self._report_once()
        args, kwargs = self._inject(args, kwargs)
        return await self._inner.get_response(*args, **kwargs)

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        self._report_once()
        args, kwargs = self._inject(args, kwargs)
        async for chunk in self._inner.stream_response(*args, **kwargs):
            yield chunk


def _is_tool_failure(result: Any) -> bool:
    """F2.3：业务错误分类隔离——见计划「三分法」。

    工具「协议层成功返回」的一切内容（含 ``ok:false``、业务报错文本如「资源不足」）
    均属**业务观察**，不计失败、原样回喂模型自愈，不触发重复失败提醒/升级。

    仅当结果显式携带协议层错误标记（``protocol_error``/``transport_error``，属 SDK/
    网络层语义、非业务）才判失败。这类信号在通用约定下极罕见——真正的协议错误
    （超时/断连/5xx/返回体解析失败）以**异常**形式抛出，由 ``run_subtask_sdk`` 的
    except 分支统一重试/退避/升级，不经过本函数。
    """
    if not isinstance(result, dict):
        return False
    # 协议层错误：工具显式声明传输/解析故障（非业务语义）
    if result.get("protocol_error") or result.get("transport_error"):
        return True
    # 业务观察（ok:false / error 文本 / 其它结构）一律不计失败，原样回喂模型
    return False


class _RepeatGuard:
    """T4.5：同名工具**连续**失败计数（步骤回调写入，Model 层消费）。

    - 成功 -> 计数清零；换工具名 -> 重新开始计数（只统计同名连续失败）；
    - 连续失败达阈值 -> 生成一条待注入提醒（只生成一次），随后计数清零避免刷屏；
    - 本期不校验参数一致性（同一工具不同参数也算同名连续）。
    """

    def __init__(self, threshold: int = 3):
        self.threshold = int(threshold or 0)
        self._name = ""
        self._count = 0
        self.pending: Optional[Dict[str, Any]] = None
        # F1.2：累计失败次数（与连续计数解耦——连续计数达阈值清零，累计计数只增不减）
        self.total_failures: int = 0

    def on_step(self, tool_name: str, failed: bool) -> None:
        if self.threshold <= 0:
            return
        name = str(tool_name or "?")
        if not failed:
            self._name = ""
            self._count = 0
            return
        self.total_failures += 1
        if name == self._name:
            self._count += 1
        else:
            self._name = name
            self._count = 1
        if self._count >= self.threshold and self.pending is None:
            self.pending = {
                "role": "user",
                "content": (
                    f"[执行提醒] 工具 {name} 已连续失败 {self._count} 次。"
                    f"请更换执行方案（换别的工具、换参数或换路径），"
                    f"不要继续重复同一失败调用。"
                ),
            }
            self._count = 0      # 注入一次后计数清零，避免重复刷屏

    def take_message(self) -> Optional[Dict[str, Any]]:
        msg, self.pending = self.pending, None
        return msg


class _RepeatGuardModel(Model):
    """T4.5：把重复失败提醒注入**下一次**模型请求（与预算提示同一范式）。"""

    def __init__(self, inner: Model, guard: _RepeatGuard):
        self._inner = inner
        self._guard = guard

    @staticmethod
    def _inject(args: tuple, kwargs: Dict[str, Any], msg: Dict[str, Any]):
        if "input" in kwargs:
            return args, {**kwargs, "input": list(kwargs["input"]) + [msg]}
        if len(args) >= 2:
            return (args[0], list(args[1]) + [msg]) + tuple(args[2:]), kwargs
        return args, kwargs

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        msg = self._guard.take_message()
        if msg:
            args, kwargs = self._inject(args, kwargs, msg)
        return await self._inner.get_response(*args, **kwargs)

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        msg = self._guard.take_message()
        if msg:
            args, kwargs = self._inject(args, kwargs, msg)
        async for chunk in self._inner.stream_response(*args, **kwargs):
            yield chunk


class _OutputTruncationModel(Model):
    """T4.7：输出截断可观测——单次输出触达 max_tokens 上限时回调告警。

    输出被切（finish_reason=length）**不会报错**，只会产出半截文本/JSON/工具参数，
    表现为「工具调用莫名失败」「回答残缺」且日志无异常。这里用
    「usage 输出 token 触顶上限」判定并上报，把玄学失败变成可定位事件。
    判定失败（无 usage）时静默跳过，绝不影响主流程。
    """

    def __init__(
        self,
        inner: Model,
        max_output_tokens: int,
        on_truncate: Optional[Callable[[int, int], None]] = None,
    ):
        self._inner = inner
        self._max_output_tokens = int(max_output_tokens or 0)
        self._on_truncate = on_truncate

    @staticmethod
    def _usage_output(resp: Any) -> int:
        """从 ModelResponse.usage 取输出 token 数（兼容新旧字段名）。"""
        u = getattr(resp, "usage", None)
        if u is None:
            return 0
        for k in ("output_tokens", "completion_tokens"):
            v = getattr(u, k, None)
            if isinstance(v, int) and v > 0:
                return v
        return 0

    def _check(self, resp: Any) -> None:
        if not self._max_output_tokens or self._on_truncate is None or resp is None:
            return
        out = self._usage_output(resp)
        if out >= self._max_output_tokens:
            try:
                self._on_truncate(out, self._max_output_tokens)
            except Exception:
                pass

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        resp = await self._inner.get_response(*args, **kwargs)
        self._check(resp)
        return resp

    async def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        # 流式：事件原样透传；结束时从 response.completed 事件取 usage 判定
        async for chunk in self._inner.stream_response(*args, **kwargs):
            if getattr(chunk, "type", None) == "response.completed":
                self._check(getattr(chunk, "response", None))
            yield chunk


def _deltas_of(event) -> List[tuple]:
    """从 SDK 原始流式事件抽取 ``(kind, text)`` 增量（kind ∈ {reasoning, message}）。

    真流式打字机（问题3-B）的数据源：``Runner.run_streamed`` 的 ``raw_response_event``
    携带 token 级 delta，这里只挑出文本增量（推理链 / 口播），工具调用参数增量忽略
    （工具卡片由 ``on_step`` 负责）。两类模型都覆盖：
    - ``response.output_text.delta``       → message（表层口播结论）
    - ``response.reasoning_summary_text.delta`` / ``response.reasoning_text.delta`` → reasoning
    """
    # 事件类型：优先 event.type（SDK 真实结构），兼容 event.delta.type（旧协议/个别 provider）
    t = getattr(event, "type", None)
    if t is None:
        t = getattr(getattr(event, "delta", None), "type", None)
    # 文本增量：event.delta 可能是字符串（openai-agents 标准），或对象带 .text/.delta 子字段
    d = getattr(event, "delta", None)
    if isinstance(d, str):
        txt = d
    else:
        txt = getattr(d, "delta", None) or getattr(d, "text", None)
    if not txt:
        return []
    if t == "response.output_text.delta":
        return [("message", txt)]
    if t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        return [("reasoning", txt)]
    return []


def _extract(items):
    """从 SDK output items 抽取 ``(reasoning, message, calls)``。

    - ``reasoning``：推理模型真实思考链（reasoning item 的 summary / message 块的
      reasoning_content）—— 与口播分离，单独走「深度思考」通道（问题1：思考≠结论）；
    - ``message``：表层口播结论（output_text）—— 走「对话」通道，与思考一起流式呈现；
    - ``calls``：本轮工具调用。
    """
    reasoning_parts: List[str] = []
    message_parts: List[str] = []
    calls: List[Any] = []
    for item in items or []:
        itype = getattr(item, "type", "")
        if itype == "reasoning":
            for s in getattr(item, "summary", None) or []:
                t = getattr(s, "text", None)
                if t:
                    reasoning_parts.append(t)
        elif itype == "message":
            for b in getattr(item, "content", None) or []:
                if getattr(b, "type", "") == "output_text":
                    message_parts.append(getattr(b, "text", "") or "")
                rc = getattr(b, "reasoning_content", None)
                if rc:
                    reasoning_parts.append(rc)
        elif itype == "function_call":
            calls.append({
                "name": getattr(item, "name", ""),
                "arguments": getattr(item, "arguments", ""),
            })
    reasoning = "\n".join(reasoning_parts).strip()
    message = "".join(message_parts).strip()
    return reasoning, message, calls


def _parse_llm_response(resp: Any):
    """兼容旧调用：从 ModelResponse 取 ``(reasoning, message, calls)``。"""
    return _extract(getattr(resp, "output", None) or [])


def _parse_items(result: Any):
    """从 Runner 结果抽取 ``(reasoning, message, calls)``。

    数据源优先级：
    1. ``raw_responses``：每个是 ModelResponse，其 ``.output`` 为 TResponseOutputItem
       列表（message / reasoning / function_call），与 ``_extract`` 期望完全一致；
    2. 兜底：``new_items``（RunItem）取底层 ``.item``（TResponseOutputItem）。
    """
    responses = getattr(result, "raw_responses", None) or []
    items: List[Any] = []
    for resp in responses:
        items.extend(getattr(resp, "output", None) or [])
    if not items:
        for ni in getattr(result, "new_items", None) or []:
            underlying = getattr(ni, "item", None) or getattr(ni, "raw_item", None)
            if underlying is not None:
                items.append(underlying)
    return _extract(items)


def _model_of(brain: Any) -> Model:
    """拿到大脑的 SDK Model；没有则把 `.chat()` 适配成 Model。"""
    getter = getattr(brain, "sdk_model", None)
    if callable(getter):
        model = getter()
        if model is not None:
            return model
    return build_chat_bridge_model(brain)


def _mk_task_done(state: SubtaskState, gate: Any,
                  on_state: Optional[Callable[[str], None]]) -> FunctionTool:
    @sdk_function_tool(strict_mode=False, failure_error_function=None)
    def task_done(reason: str = "") -> dict:
        """声明任务完成（会先校验完成条件；未达成会被拒绝，请继续操作）。

        Args:
            reason: 完成原因
        """
        if on_state:
            on_state("VERIFYING")
        accepted, why = gate.verify_done()
        state.verify_fail = gate.verify_count
        if accepted:
            state.done = True
            state.done_reason = reason or why
            return {"accepted": True, "verified": True, "reason": why}
        return {"accepted": False, "verified": False, "reason": f"verify 失败: {why}，目标未达成，请继续操作"}

    return task_done


def _mk_verify(state: SubtaskState, gate: Any,
               on_state: Optional[Callable[[str], None]]) -> FunctionTool:
    @sdk_function_tool(strict_mode=False, failure_error_function=None)
    def verify(condition: str = "") -> dict:
        """校验当前环境是否已达成某条件。

        Args:
            condition: 期望出现的文本条件；留空则用任务完成条件
        """
        if on_state:
            on_state("VERIFYING")
        passed, why = gate.verify(condition or "")
        state.verify_fail = gate.verify_count
        return {"pass": passed, "reason": why, "verify_fail": state.verify_fail}

    return verify


def _mk_escalate(state: SubtaskState) -> FunctionTool:
    @sdk_function_tool(strict_mode=False, failure_error_function=None)
    def escalate(reason: str = "") -> dict:
        """自己搞不定时把现场交回上级大脑重新决策。

        Args:
            reason: 卡点简述
        """
        state.escalated = True
        state.escalate_reason = reason or "worker 主动升级"
        return {"escalated": True, "reason": state.escalate_reason}

    return escalate


def _mk_record(gate: Any) -> FunctionTool:
    @sdk_function_tool(strict_mode=False, failure_error_function=None)
    def record(text: str = "") -> dict:
        """把一条文本发现持久化到世界模型（跨步骤/子任务不丢）。

        Args:
            text: 要记录的文本
        """
        return gate.note(text)

    return record


def _mk_dispatch(state: SubtaskState, gate: Any) -> FunctionTool:
    @sdk_function_tool(strict_mode=False, failure_error_function=None)
    def dispatch(items: str = "", need_verify: bool = False) -> dict:
        """把若干互不依赖的子任务分派给多个子 agent 同时执行。

        仅当这些子任务之间互不依赖、且各自耗时较长（大量读取 / 远程调用）时才使用；
        需要严格按顺序做、或你本轮就能直接做完的事，请自己继续做，不要派发。

        Args:
            items: JSON 数组，每项形如 {"desc": "子任务描述", "done_when": "完成条件"}
            need_verify: 本批次是否需二次复核（默认 false，行为与旧版一致）。
                开启后，子任务回收完成、下一轮执行前会再校验一次完成条件；
                校验不通过只追加「复核未通过」标记，保留未完成语义交由模型
                决策（不强制拦截、不做惩罚）。
        """
        plan = parse_dispatch_items(items)
        if need_verify:
            for it in plan:
                if isinstance(it, dict):
                    it["need_verify"] = True
        state.dispatch_plan = plan
        # 同时挂在 gate 上：gate 由调用方（L2）持有，编排层据此做并行扇出
        try:
            setattr(gate, "dispatch_plan", plan)
        except Exception:
            pass
        return {"dispatched": len(plan), "items": plan}

    return dispatch


def _mk_todo_write(todo_store: Any) -> FunctionTool:
    @sdk_function_tool(strict_mode=False, failure_error_function=None)
    def todo_write(items_json: str = "") -> dict:
        """写入/覆盖当前任务待办清单（整体替换），调用后立即落盘，跨步骤/子任务保留。

        使用规范：
        - 开始执行前先规划：把任务拆成若干条目，一次性写入；
        - 完成某项就把它的 status 改为 "done"，正在做的改为 "in_progress"；
        - 整体替换语义：每次调用传入完整清单（含未变条目），不要只传本次变更；
        - status 仅接受 pending / in_progress / done 三种。

        Args:
            items_json: JSON 数组字符串，每项形如
                {"content": "待办内容", "status": "pending|in_progress|done"}
        """
        try:
            parsed = json.loads(items_json) if items_json else []
        except Exception as e:
            return {"ok": False, "error": f"JSON 解析失败: {e}", "total": 0, "done": 0}
        if not isinstance(parsed, list):
            return {"ok": False, "error": "items_json 必须是 JSON 数组", "total": 0, "done": 0}
        norm: List[Dict[str, str]] = []
        for i, it in enumerate(parsed):
            if not isinstance(it, dict):
                return {"ok": False, "error": f"第 {i} 项不是对象", "total": 0, "done": 0}
            content = it.get("content")
            if not isinstance(content, str) or not content.strip():
                return {"ok": False, "error": f"第 {i} 项缺少有效 content", "total": 0, "done": 0}
            st = it.get("status", "pending")
            if st not in ("pending", "in_progress", "done"):
                return {"ok": False, "error": f"第 {i} 项 status 非法: {st}", "total": 0, "done": 0}
            norm.append({"content": content, "status": st})
        try:
            todo_store.save(norm)
        except Exception as e:
            return {"ok": False, "error": f"落盘失败: {e}", "total": 0, "done": 0}
        done = sum(1 for x in norm if x["status"] == "done")
        return {"ok": True, "total": len(norm), "done": done}

    return todo_write


def build_meta_tools(
    state: SubtaskState,
    gate: Any,
    on_state: Optional[Callable[[str], None]] = None,
    allow_dispatch: bool = False,
    todo_store: Any = None,
) -> List[FunctionTool]:
    """构造内核元工具（L2 门控）：task_done / verify / escalate / record（+ dispatch / todo_write）。

    它们与能力插件**平级**地出现在 Agent 的工具清单里，由 LLM 直接调用；
    但实现属于 L2（收尾门控 / 升级判定 / 世界模型写入），不是能力。

    Args:
        state: 本次运行的状态（工具写入、runner 读取）。
        gate: 提供 `verify(condition)` / `note(text)` 的 L2 门控对象。
        allow_dispatch: 是否暴露 `dispatch`（仅主 agent 有派发权；子 agent 没有）。
        todo_store: 可选待办存储对象（含 `load()` / `save(data)`），仅主 agent 暴露
            `todo_write` 时注入；为 None 则不暴露该工具（子 agent 永不暴露）。
    """
    tools: List[FunctionTool] = [
        _mk_task_done(state, gate, on_state),
        _mk_verify(state, gate, on_state),
        _mk_escalate(state),
        _mk_record(gate),
    ]
    if allow_dispatch:
        tools.append(_mk_dispatch(state, gate))
        if todo_store is not None:
            tools.append(_mk_todo_write(todo_store))
    return tools


async def _run_loop_streamed(agent, items, turns, hooks, on_llm_delta, on_llm_final,
                             on_turn_end=None):
    """用 ``Runner.run_streamed`` 跑一块：实时抽取 token 增量供前端打字机，结束再回传完整结果。

    端点/模型不支持流式时退化为同步 ``Runner.run``（行为不变），保证兼容旧链路与测试。
    ``on_llm_delta(kind, text)`` 在流式时按 token 回调（kind ∈ {reasoning, message}）；
    ``on_llm_final(reasoning, message, calls)`` 按**回合**回传该回合完整解析；
    ``on_turn_end()`` 在回合边界（该回合工具调用产出时）回调，供上游切块（seq++/清缓冲）。

    回合粒度说明（2026-09-23 修复）：一个 chunk 内含多回合，流式事件本身不带回合号，
    原先只在 chunk 结束调一次 ``on_llm_final`` → 上游整 chunk 的推理/口播共用同一 block key
    被 upsert 合并成一条并停在首次位置（现象：终态「结果与思考跑到最前、工具调用挤到后面」）。
    这里以流里的工具调用项作为回合边界：先把本回合内容收尾成独立块，再通知上游切块。
    """
    try:
        # 注意：run_streamed 是同步方法（直接返回 RunResultStreaming），不能 await。
        # 此前写成 await 会在首行抛 TypeError 被下方 except 无声吞掉、静默退回
        # 同步 Runner.run——表现为「打字机流式完全不存在」（2026-09-18 修复）。
        result = Runner.run_streamed(agent, items, max_turns=turns, hooks=hooks,
                                   run_config=_TOOL_NOT_FOUND_RUN_CONFIG)
        r_buf: List[str] = []
        m_buf: List[str] = []
        turn_calls: List[Any] = []
        saw_delta = False
        async for ev in result.stream_events():
            et = getattr(ev, "type", None)
            if et == "raw_response_event":
                d = getattr(ev, "data", None)
                if d is not None:
                    # 真流式打字机数据源：每帧 delta 立即回调（问题1/问题3 的 live 思考+口播）
                    for kind, text in _deltas_of(d):
                        saw_delta = True
                        (r_buf if kind == "reasoning" else m_buf).append(text)
                        if on_llm_delta is not None:
                            on_llm_delta(kind, text)
            elif et == "run_item_stream_event":
                # 回合边界：本回合的工具调用已产出 → 收尾本回合块，再切块
                item = getattr(ev, "item", None)
                if getattr(item, "type", None) == "tool_call_item":
                    raw = getattr(item, "raw_item", None) or item
                    turn_calls.append({
                        "name": getattr(raw, "name", "") or "",
                        "arguments": getattr(raw, "arguments", "") or "",
                    })
                    if on_llm_final is not None:
                        try:
                            on_llm_final("".join(r_buf), "".join(m_buf), list(turn_calls))
                        except Exception:
                            pass
                    r_buf = []
                    m_buf = []
                    turn_calls = []
                    if on_turn_end is not None:
                        try:
                            on_turn_end()
                        except Exception:
                            pass
        if on_llm_final is not None:
            try:
                if saw_delta:
                    # 流式已逐回合交付：此处只补最后一回合的尾巴，绝不回灌整 chunk 聚合内容
                    if r_buf or m_buf or turn_calls:
                        on_llm_final("".join(r_buf), "".join(m_buf), list(turn_calls))
                    if on_turn_end is not None:
                        on_turn_end()
                else:
                    # 全程无增量（端点不支持流式）：退回整块解析，行为与旧链路一致
                    reasoning, message, calls = _parse_items(result)
                    on_llm_final(reasoning, message, calls)
            except Exception:
                pass
        return result
    except MaxTurnsExceeded:
        raise
    except Exception as e:
        # 不支持流式（如 chat_bridge 适配 / 离线端点）：退回同步跑，行为不变。
        # 必须留痕：此降级曾因静默吞异常掩盖 await 事故，绝不再无声失败。
        print(f"[sdk_loop] 流式不可用，退回同步 Runner.run: {type(e).__name__}: {e}", flush=True)
        try:
            result = await Runner.run(agent, items, max_turns=turns, hooks=hooks,
                                   run_config=_TOOL_NOT_FOUND_RUN_CONFIG)
            if on_llm_final:
                try:
                    reasoning, message, calls = _parse_items(result)
                    on_llm_final(reasoning, message, calls)
                except Exception:
                    pass
            return result
        except MaxTurnsExceeded:
            raise
        except Exception:
            raise


# --- T4.2：MCP 运行间重连（指数退避）-----------------------------------------
_MCP_MAX_ATTEMPTS = 10          # 连续失败上限，达到即放弃该服务
_MCP_RETRY_BASE_SEC = 0.5       # 初始退避
_MCP_RETRY_MAX_SEC = 30.0       # 退避上限
_MCP_FAIL_STREAK: Dict[str, int] = {}      # 服务名 -> 连续失败次数
_MCP_ABANDONED: Dict[str, bool] = {}       # 连续失败达上限 -> 本次进程内放弃
_MCP_SLEEP = time.sleep                    # 可注入（测试无需真等）


def _mcp_backoff_delay(attempt: int) -> float:
    """指数退避：0.5s 起、30s 封顶。"""
    return min(_MCP_RETRY_BASE_SEC * (2 ** max(0, attempt - 1)), _MCP_RETRY_MAX_SEC)


def _mcp_note_failure(name: str) -> bool:
    """记一次失败，返回 True 表示已达上限、应放弃该服务。"""
    n = int(_MCP_FAIL_STREAK.get(name, 0)) + 1
    _MCP_FAIL_STREAK[name] = n
    if n >= _MCP_MAX_ATTEMPTS:
        _MCP_ABANDONED[name] = True
        return True
    return False


def _mcp_note_success(name: str) -> None:
    """探活成功 -> 连续失败计数清零（后续异常仍可完整重试）。"""
    _MCP_FAIL_STREAK.pop(name, None)
    _MCP_ABANDONED.pop(name, None)


def _ensure_mcp_connected(servers: List[Any]) -> List[Any]:
    """连接并探活 MCP server，隔离单个 server 的故障（常驻 loop 内复用）。

    设计原则：MCP 是增强项、不是硬依赖——**任何 server 的连接或工具发现失败都只
    跳过该 server，绝不拖垮整体 agent / 服务**。本版 agents SDK 的 Runner 不会自动
    connect mcp_servers（见 agents/agent.py 注释），须调用方自行 ``connect()``；且
    ``list_tools``/``call_tool`` 在 ``session`` 为空时抛 "Server not initialized"。
    - 已连接（session 非空）的 server 直接复用，避免重复 connect 报错；
    - connect 失败：跳过（URL 错 / 服务没起 / 网络不通都不影响 agent）；
    - connect 成功但 ``list_tools`` 探活失败（服务半死）：同样跳过，避免运行期
      SDK 聚合工具时把整个 agent 构建带崩。

    T4.2 增强（**仅运行间重连**：重试发生在「连接/探活」阶段，即两次运行之间；
    单次运行内工具调用失败不触发自动重连）：
    - 超时：连接与探活均带超时（取 SDK 原生 ``client_session_timeout_seconds``，
      由 mcp.json 的 ``timeout_ms`` 透传，默认 60000ms）；
    - 指数退避重试：0.5s 起、30s 封顶，连续 10 次失败则放弃该服务（本次进程内
      后续运行直接跳过），探活成功后计数清零。
    """
    connected: List[Any] = []
    for s in servers:
        try:
            _nm = str(getattr(s, "name", "?"))
        except Exception:
            _nm = "?"
        if _MCP_ABANDONED.get(_nm):
            print(f"[sdk_loop] MCP server {_nm} 已连续失败 {_MCP_MAX_ATTEMPTS} 次，"
                  f"本次跳过（重启后重试）", flush=True)
            continue
        _timeout = getattr(s, "client_session_timeout_seconds", None)
        ok = False
        attempt = 0
        last_err: Optional[BaseException] = None
        while attempt < _MCP_MAX_ATTEMPTS:
            attempt += 1
            try:
                if getattr(s, "session", None) is None:
                    run_async(s.connect(), timeout=_timeout)
                # 探活：能列出工具才视为可用；连上却列不出 = 半死，不纳入 agent。
                run_async(s.list_tools(), timeout=_timeout)
                ok = True
                break
            except Exception as e:
                last_err = e
                if _mcp_note_failure(_nm):
                    break
                _MCP_SLEEP(_mcp_backoff_delay(attempt))
        if ok:
            _mcp_note_success(_nm)
            connected.append(s)
            continue
        print(f"[sdk_loop] MCP server {_nm} 不可用，已跳过: "
              f"{type(last_err).__name__ if last_err else '?'}: {last_err}", flush=True)
    return connected


def _assemble_agent_stack(
    brain: Any,
    *,
    name: str,
    instructions: str,
    tools: List[FunctionTool],
    gate: Any,
    mcp_servers: Optional[List[Any]],
    max_steps: Optional[int],
    allow_dispatch: bool,
    todo_store: Any,
    tail_inject_block: str,
    tail_inject_layers: Optional[List[str]],
    on_inject: Optional[Callable[[int, List[str]], None]],
    budget_hint_ratio: float,
    max_input_tokens: int,
    compress_threshold: float,
    hard_ceiling: float,
    retain_ratio: float,
    compress_after: int,
    summarize: Optional[Callable[[List[Any]], Optional[str]]],
    max_output_tokens: int,
    on_truncate: Optional[Callable[[int, int], None]],
    model_settings: Any,
    on_state: Optional[Callable[[str], None]],
) -> Tuple[SubtaskState, Any, Any, bool, Optional[_RepeatGuard]]:
    """装配子任务的模型包装链 + `Agent` 实例，并连接 MCP（由 run_subtask_sdk 抽出，零逻辑改动）。

    包装链自内向外：尾部重插（记忆块）→ 预算提示 → 重复失败防护 → 粘性压缩 → 输出截断。
    返回 ``(state, agent, model, use_compaction, _repeat_guard)`` 供后续块循环复用。
    """
    state = SubtaskState()
    # 收尾工具集（M7：允许派发时，dispatch 也触发收尾，好让编排层立刻扇出）
    stop_tools = ["task_done", "verify", "escalate"] + (["dispatch"] if allow_dispatch else [])
    model = _model_of(brain)
    # F4.2：尾部重插（记忆块）——置于最内层（最接近真实模型），
    # 确保压缩/保留先跑、本块后追加；只改本次请求副本，不落 SDK items（会话历史不增长）。
    if tail_inject_block:
        model = _TailInjectModel(model, tail_inject_block, tail_inject_layers, on_inject)
    if budget_hint_ratio > 0:
        model = _BudgetHintModel(model, state, max_steps, budget_hint_ratio)
    # T4.5（RG-1）：工具重复失败防护（默认 3 次，配置 0 = 关闭）
    _repeat_guard: Optional[_RepeatGuard] = None
    try:
        _rg = int(config.get_config("runtime.long_task.repeat_guard", 3) or 0)
    except Exception:
        _rg = 0
    if _rg > 0:
        _repeat_guard = _RepeatGuard(_rg)
        model = _RepeatGuardModel(model, _repeat_guard)
    use_compaction = bool(max_input_tokens and max_input_tokens > 0 and summarize is not None)
    if use_compaction:
        _thr = int(max_input_tokens * compress_threshold) if compress_threshold and compress_threshold > 0 \
            else int(max_input_tokens * 0.5)
        _hard = int(max_input_tokens * hard_ceiling) if hard_ceiling and hard_ceiling > 0 \
            else int(max_input_tokens * 0.9)
        _retain = retain_ratio if retain_ratio and retain_ratio > 0 else 0.5
        model = _CompactionModel(
            model,
            threshold_tokens=max(1, _thr),
            retain_ratio=_retain,
            summarize=summarize,
            hard_ceiling_tokens=_hard,
            keep_rounds=compress_after or 8,
        )
    # T4.7：输出截断检测——包在最外层，拿到最终响应的 usage 用量
    if max_output_tokens > 0:
        model = _OutputTruncationModel(model, max_output_tokens, on_truncate)
    # MCP 生命周期（关键修复）：本版 agents SDK 的 Runner 不会自动 connect
    # mcp_servers（见 agents/agent.py 注释：调用方须自行 connect）。不在同一常驻
    # loop 内先 connect，server.session 永远为空，任何 MCP 工具调用都会抛
    # "Server not initialized" 使 agent 不可用。这里在子任务开始时连接（已连接则
    # 复用），单个 server 失败只跳过、不拖垮整体。
    _mcp_connected = _ensure_mcp_connected(list(mcp_servers or []))
    agent = Agent(
        name=name,
        model=model,
        instructions=instructions,
        tools=list(tools) + build_meta_tools(state, gate, on_state, allow_dispatch, todo_store),
        mcp_servers=_mcp_connected,
        # T4.7：把 request（temperature / max_tokens 等）真正下发给模型；
        # 此前 SDK 主链路未传 model_settings，配置里的 request.max_tokens 形同虚设。
        # 注：SDK 升级后 Agent.__post_init__ 不再接受 model_settings=None，
        # 故 None 退化为空 dict（SDK 会 coerce 为默认 ModelSettings，行为等价于原 None），
        # 仅在确有配置值时才下发真实 model_settings。
        model_settings=model_settings if model_settings is not None else {},
        # 框架原生收尾：调了 task_done / verify / escalate 就结束本块，把控制权交回 L2。
        # - task_done 被拒绝（verify 未过）→ L2 不认 done，下一块带着拒绝原因继续跑；
        # - verify 每调一次就回 L2 一次，使「连续失败升级」阈值能及时生效。
        tool_use_behavior=StopAtTools(stop_at_tool_names=stop_tools),
    )
    return state, agent, model, use_compaction, _repeat_guard


def run_subtask_sdk(
    brain: Any,
    *,
    instructions: str,
    user_input: str,
    tools: List[FunctionTool],
    gate: Any,
    mcp_servers: Optional[List[Any]] = None,
    max_steps: Optional[int] = None,
    chunk_turns: int = 0,
    wallclock_sec: float = 0.0,
    # F2.2：收尾语义——oneshot（缺省，纯文本收尾即 success）| daemon（纯文本汇报后 paused 等待续跑）
    task_mode: str = "oneshot",
    # F4.2：尾部重插——**仅记忆块**（纪律文件 F4.1b 起已迁 system prompt），
    # 非空时在压缩后追加到请求 input 尾部
    tail_inject_block: str = "",
    tail_inject_layers: Optional[List[str]] = None,
    on_inject: Optional[Callable[[int, List[str]], None]] = None,
    # F4.1b：纪律文件 run 级快照（只读比对用；不改请求）+ 中途漂移告警回调
    instructions_snapshot: Any = None,
    on_instructions_drift: Optional[Callable[[List[str]], None]] = None,
    verify_fail_max: int = 3,
    should_stop: Optional[Callable[[], bool]] = None,
    on_step: Optional[Callable[[str, Any], None]] = None,
    on_state: Optional[Callable[[str], None]] = None,
    compress_after: int = 0,
    summarize: Optional[Callable[[List[Any]], Optional[str]]] = None,
    history_keep: int = 0,
    compress_threshold: float = 0.0,
    hard_ceiling: float = 0.0,
    ctx_window: int = 0,
    prune_threshold: int = 0,
    prune_head: int = 0,
    prune_tail: int = 0,
    max_input_tokens: int = 0,
    retain_ratio: float = 0.0,
    history_items: Optional[List[Dict[str, str]]] = None,
    name: str = "omni_worker",
    allow_dispatch: bool = False,
    todo_store: Any = None,
    skill_catalog: Optional[str] = None,
    budget_hint_ratio: float = 0.0,
    on_llm: Optional[Callable[[str, str, List[Any]], None]] = None,
    on_llm_delta: Optional[Callable[[str, str], None]] = None,
    # 回合边界回调（流式）：每回合工具调用产出时触发，供上游切块（seq++/清缓冲），
    # 避免整 chunk 的推理/口播被合并成单块（2026-09-23）
    on_turn_end: Optional[Callable[[], None]] = None,
    # S1（2026-09-21 stop 不生效修复）：块级 SDK Task 句柄回传——调用方
    # （ToolLoop.request_stop）据此直接 cancel 正在跑的 Runner，mid-chunk 立即中断。
    on_sdk_task: Optional[Callable[[Any], None]] = None,
    # T4.7：输出上限（token）与触顶回调；同时把 request 配置下发给模型
    max_output_tokens: int = 0,
    on_truncate: Optional[Callable[[int, int], None]] = None,
    model_settings: Any = None,
) -> Dict[str, Any]:
    """用 SDK Runner 跑一个子任务，返回与旧 `_run_inner` 相同的结果形状。

    M7：``allow_dispatch=True`` 时暴露 `dispatch` 元工具（主 agent 才有派发权），
    主 agent 一调就停下把控制权交回编排层去并行扇出。

    Returns:
        {success, reason, steps, escalated, escalate_reason, dispatch_plan}
    """
    state, agent, model, use_compaction, _repeat_guard = _assemble_agent_stack(
        brain,
        name=name,
        instructions=instructions,
        tools=tools,
        gate=gate,
        mcp_servers=mcp_servers,
        max_steps=max_steps,
        allow_dispatch=allow_dispatch,
        todo_store=todo_store,
        tail_inject_block=tail_inject_block,
        tail_inject_layers=tail_inject_layers,
        on_inject=on_inject,
        budget_hint_ratio=budget_hint_ratio,
        max_input_tokens=max_input_tokens,
        compress_threshold=compress_threshold,
        hard_ceiling=hard_ceiling,
        retain_ratio=retain_ratio,
        compress_after=compress_after,
        summarize=summarize,
        max_output_tokens=max_output_tokens,
        on_truncate=on_truncate,
        model_settings=model_settings,
        on_state=on_state,
    )
    # 「方案 B（纯文本收尾）」的例外标记：本块是否调用过 verify 元工具。
    # verify 属于 stop_tools，一调即结束本块。但它的语义是「请核对完成条件」，
    # 不是「我干完了」——若无此标记，模型中途调一次 verify 停块后，会被下面的
    # 方案 B 误判为「模型已收尾」，从而在无完成条件时把未完成的任务提前判成功
    # （现象：test_brain_long_task_runs_to_completion 步数明显偏低）。
    verify_stopped = False

    def _hook(tool: Any, result: Any) -> None:
        nonlocal verify_stopped
        if str(tool) == "verify":
            verify_stopped = True
        # 步数以「实际发生的工具调用」记账（比 new_items 计数更可靠）
        state.steps += 1
        # T4.5：同名工具连续失败统计（供下一次请求注入换方案提醒）
        if _repeat_guard is not None:
            try:
                _repeat_guard.on_step(str(tool), _is_tool_failure(result))
                state.repeat_failures = _repeat_guard.total_failures
            except Exception:
                pass
        if on_step is not None:
            on_step(tool, result)

    hooks = OmniHooks(_hook, state=state)
    should_stop = should_stop or (lambda: False)
    started = time.time()

    items: List[Any] = []
    # 会话历史注入（单链路统一）：标准 user/assistant 交替历史，置于本轮
    # user_input 之前——模型据此区分「历史对话」与「当前问题」，替代旧的
    # 历史单路注入（两条 user 连排会让模型误读为用户连说两件事）。
    # 仅进起始 items；后续块由 res.to_input_list() 自然继承。
    for h in history_items or []:
        _r = str(h.get("role", "user"))
        _c = str(h.get("content", "") or "")
        if _c:
            items.append({"role": _r if _r in ("user", "assistant") else "user", "content": _c})
    # T2.4（O4'）：技能目录改为固定模板 User 消息，插在历史之后、本轮 user_input 之前；
    # 记忆注入仍在 system（instructions），保持不变。
    if skill_catalog:
        items.append({"role": "user", "content": skill_catalog})
    items.append({"role": "user", "content": user_input})

    # chunk_turns 默认从 config 读取（runtime.chunk_turns，默认 50）；
    # 调用方显式传入时优先使用传入值。
    if not chunk_turns:
        chunk_turns = int(config.get_config("runtime.chunk_turns", 50))

    # T3.3（U1d）：上下文溢出自动恢复——同一子任务内只允许重试一次，防止无效重试循环
    overflow_retried = False
    # A2（2026-09-21 崩溃自愈）：输出 length 截断重试计数器（< LENGTH_RETRY_LIMIT，防弱模型死循环）
    length_retry = 0
    # A1（2026-09-21 崩溃自愈）：工具不存在自愈重试计数器（< TOOL_NOT_FOUND_RETRY_LIMIT，双保险）
    tool_not_found_retry = 0
    # F4.1b：纪律文件 run 级快照已在 run 起始锁定（system 逐字节稳定 → 前缀缓存命中）。
    # 这里只做**只读比对**：run 中途文件被改动 → 告警（沿用快照，改动自下个 run 生效）。
    # 已告警过的变更集合只报一次，避免逐块刷屏。
    _drift_reported: set = set()
    while max_steps is None or state.steps < max_steps:
        if should_stop():
            return _result(False, "用户主动停止", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)

        if on_state:
            on_state("EXECUTING")
        turns = chunk_turns if max_steps is None else max(1, min(chunk_turns, max_steps - state.steps))
        blk_before = state.steps   # T4.6：块起始步数（记账兜底基线）
        verify_stopped = False     # 逐块重置：只关心「本块」是否由 verify 收尾
        # F4.1b：逐块检查纪律文件是否在 run 中途被改动（只读，不参与本次注入）
        if instructions_snapshot is not None and on_instructions_drift is not None:
            try:
                _changed = tuple(instructions_snapshot.check_drift() or [])
            except Exception:
                _changed = ()
            if _changed and _changed not in _drift_reported:
                _drift_reported.add(_changed)
                try:
                    on_instructions_drift(list(_changed))
                except Exception:
                    pass
        try:
            # 用 streamed 跑：即使本块步数用尽也能拿回完整历史
            # （Runner.run 在 max_turns 时会直接抛，历史会丢）。
            before = state.steps
            if on_llm_delta is not None:
                _coro = _run_loop_streamed(
                    agent, items, turns, hooks, on_llm_delta, on_llm, on_turn_end)
            else:
                _coro = Runner.run(agent, items, max_turns=turns, hooks=hooks,
                                   run_config=_TOOL_NOT_FOUND_RUN_CONFIG)
            if on_sdk_task is None:
                res = run_async(_coro)
            else:
                # S1（stop 不生效修复）：把整块协程提交为独立 Task 并把句柄回传给
                # 调用方——request_stop 可直接 cancel 正在跑的 Runner（流式/同步
                # 两分支统一覆盖）。CancelledError 是 BaseException，不会被下方
                # except Exception 吞掉，沿 run_subtask_sdk → graph 节点冒泡至
                # tool_loop 的硬停止收尾（「用户主动停止」）。句柄用后即清（cancel
                # 已完成任务为 no-op，清空只为不滞留引用）。
                _sdk_task = create_task(_coro)
                on_sdk_task(_sdk_task)
                try:
                    res = wait_task(_sdk_task)
                finally:
                    on_sdk_task(None)
            if on_llm_delta is None and on_llm:
                reasoning, message, calls = _parse_items(res)
                on_llm(reasoning, message, calls)
            items = res.to_input_list()
            if state.steps == before:
                # 本块未产生任何工具调用（如模型只回了文本）：记 1 步防记账停摆
                state.steps += 1
        except MaxTurnsExceeded:
            # 本块步数用尽且未触发收尾工具。
            # T4.6（O5+）：步数以 Hook 计数为唯一权威，此处**不再批量累加 turns**
            # （此前会让步数虚高、与 Hook 记账不一致）；仅在 Hook 未记账时兜底
            # 推进 1 步，防止记账停摆/倒退导致死循环。
            if state.steps <= blk_before:
                state.steps = blk_before + 1
            if max_steps is None:
                # 长任务：单批步数用尽但模型仍在用工具推进，不判失败，继续下一批
                continue
            # B1 修复（2026-09-22）：有硬上限时**必须校验全局真实步数**是否抵达上限，
            # 不能仅凭「本块回合跑满」就谎报预算耗尽——T4.6 移除跨块续跑判断时遗留的 bug：
            # 单块 turns = min(chunk_turns, max_steps - state.steps)，首个块边界即抛
            # MaxTurnsExceeded，导致所有带步数上限的任务在第一个 chunk_turns 块被误终止
            # （实机现象：步数上限 1000/2000，任务仅跑 94/97 步即「虚假已达步数上限」）。
            if state.steps >= max_steps:
                # 真实步数耗尽：正常终止（reason 标注真实上下限，可观测、可追溯）
                return _result(False, f"budget_exhausted: 已达步数上限 {max_steps}（实际执行 {state.steps} 步）",
                               state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
            # 仅单块回合跑满、全局仍有预算：跨块续跑下一块（有限块数内必抵终态，无死循环）
            continue
        except Exception as e:
            # T3.3（U1d）/ F3.2（P4）/ A1+A2（2026-09-21 崩溃自愈增强）：
            # 分类型自愈，避免弱模型的 ModelBehaviorError 两变体（工具不存在 / 输出 length 截断）
            # 一次性致命终止子任务。主动停止优先于一切重试（禁止无效重试）。
            # - 上下文溢出（_is_context_overflow）：维持原单次硬截历史后重试（overflow_retried 防死循环）。
            # - 输出 length 截断（_is_length_truncation）：计数器重试（< LENGTH_RETRY_LIMIT），
            #   截断历史减压 + 追加纠正提示，让弱模型跳出「反复空转」。
            # - 工具不存在（_is_tool_not_found）：官方通道已默认 return_error_to_model 不抛异常，
            #   此处仅作双保险（仍可能由其他路径抛出），限次追加纠正提示。
            if _is_context_overflow(e):
                if should_stop():
                    return _result(False, "用户主动停止", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
                if not overflow_retried:
                    overflow_retried = True
                    keep_rounds = getattr(model, "_keep_rounds", 0) or compress_after or 8
                    items = _hard_keep(items, keep_rounds)
                    continue
                # 上下文溢出重试仍失败 -> 落入下方原有失败逻辑（落 reason）
            elif _is_length_truncation(e):
                if should_stop():
                    return _result(False, "用户主动停止", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
                length_retry += 1
                if length_retry >= LENGTH_RETRY_LIMIT:
                    return _result(False,
                        f"runner 异常: 输出多次被 max_tokens 截断且无有效内容(length x{length_retry})，弱模型反复空转",
                        state.steps, True,
                        f"runner 异常: 输出多次被 max_tokens 截断(length x{length_retry})",
                        llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
                keep_rounds = getattr(model, "_keep_rounds", 0) or compress_after or 8
                items = _hard_keep(items, keep_rounds)
                items = _append_runtime_note(items, _LEN_TRUNC_MSG)
                continue
            elif _is_tool_not_found(e):
                if should_stop():
                    return _result(False, "用户主动停止", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
                tool_not_found_retry += 1
                name = _extract_missing_tool(e)
                if tool_not_found_retry >= TOOL_NOT_FOUND_RETRY_LIMIT:
                    return _result(False,
                        f"runner 异常: 模型反复调用不存在的工具{' ' + name if name else ''}（x{tool_not_found_retry}），无法自愈",
                        state.steps, True,
                        f"runner 异常: 反复调用不存在的工具{' ' + name if name else ''}",
                        llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
                items = _append_runtime_note(items,
                    f"工具{' ' + name if name else ''}不存在，请严格使用系统提供的真实工具列表，不要臆造工具名。")
                continue
            # 大脑/工具链路异常 -> 升级（与旧循环一致：不崩溃，交回上级）
            provider_msg = classify_provider_error(e)
            if provider_msg is not None:
                # U3：服务侧错误友好降级——escalated 保持 true，替换原生报错文案
                return _result(False, provider_msg, state.steps, True, provider_msg,
                               provider_error=True, llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
            _err_first = str(e).splitlines()[0] if str(e).strip() else type(e).__name__
            return _result(False, f"runner 异常: {type(e).__name__}: {_err_first}", state.steps, True,
                           f"runner 异常: {type(e).__name__}: {_err_first}", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)

        # 方案 B（正常 agent 收尾）：模型以「纯文本」结束本批——即未调 task_done/
        # verify/escalate/dispatch、也无待执行工具调用——且已执行过动作，则视为主动
        # 宣布完成。无验证条件时信任大脑直接判成功；有验证条件则走 verify_done（未
        # 满足则提示模型继续，不强行判失败）。这正是 ChatGPT/Claude 类 agent 的收尾
        # 方式：说完就停，不要求用户给条件、也不要求模型记得调特殊工具。
        # `verify_stopped` 是该前提的代码化：本块以 verify 收尾就不是「主动收尾」，
        # 直接落回下面的常规收尾/继续判定（模型会看到 verify 结果后继续推进）。
        if state.steps > 0 and not state.done and not state.escalated and not state.dispatch_plan \
                and not verify_stopped:
            accepted, why = gate.verify_done()
            state.verify_fail = gate.verify_count
            if accepted:
                state.done = True
                # F2.2：daemon 模式纯文本收尾不触发 success 终态，改为 paused 等待续跑
                if task_mode == "daemon":
                    return _result(False, "daemon 模式汇报后暂停，等待唤醒", state.steps, False, "",
                                   paused=True, llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
                return _result(True, why or "模型已收尾，判定完成", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
            # 有验证条件但未满足：不直接判失败，提示模型继续推进
            items.append({
                "role": "user",
                "content": f"你已停止输出工具调用，但完成条件尚未满足：{why}。"
                           f"请继续推进直到达成目标，不要反复声明完成。",
            })

        # 钩子把「工具结果无置信」记在 gate 上（如 template_match 未匹配）
        if getattr(gate, "no_confidence", False):
            state.no_confidence = True

        # 升级判定优先于收尾（沿旧循环语义）：已达成但仍触发升级条件时交回上级。
        if state.escalated:
            return _result(False, state.escalate_reason, state.steps, True, state.escalate_reason, llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
        if state.no_confidence:
            return _result(False, "no_confidence: 工具结果无置信", state.steps, True,
                           "no_confidence: 工具结果无置信", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
        if verify_fail_max and state.verify_fail >= verify_fail_max:
            return _result(False, f"verify 连续失败 {state.verify_fail} 次", state.steps, True,
                           f"verify 连续失败 {state.verify_fail} 次", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
        if wallclock_sec and (time.time() - started) >= wallclock_sec:
            return _result(False, "墙钟超时（wallclock_sec）", state.steps, True, "墙钟超时（wallclock_sec）", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)

        # 显式完成条件短路（沿旧循环语义）：已达成即收尾，不必等 task_done。
        if gate.has_condition:
            if on_state:
                on_state("VERIFYING")
            passed, why = gate.peek()
            if passed:
                return _result(True, why or "状态命中完成条件", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)

        if state.done:
            return _result(True, state.done_reason or "task_done", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
        # M7：主 agent 派发了子任务 → 立刻交回编排层去做并行扇出（不要接着跑下一块）
        if state.dispatch_plan:
            return _result(False, "dispatch: 已派发子任务", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)
        if should_stop():
            return _result(False, "用户主动停止", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)

        # 注：M8 的预算提示由 `_BudgetHintModel` 在 Model 适配层注入（见其文档字符串），
        # 不走块与块之间——本版 SDK 在 max_turns 用尽时拿不回历史，切块会丢上下文。

        # 长任务上下文管理（N7 分层策略）：
        # - worker（history_keep>0）：硬滑窗——无条件只留最近 N 轮，防本地小模型 ctx 溢出；
        # - 大脑：token 占比触发压缩；有 compress_threshold 按占比（抢在 context rot 前），
        #   hard_ceiling 占比则强制压缩（即便摘要失败也硬截断兜底）；无占比配置退化为轮数 compress_after。
        # T3.2：启用 Model 适配层粘性压缩后，本段整体让位（新旧逻辑互斥），
        # 压缩改由 _CompactionModel 在每次请求前于 Model 层执行。
        if use_compaction:
            pass
        elif history_keep and history_keep > 0:
            items = _hard_keep(items, history_keep)
        elif ctx_window and ctx_window > 0:
            ratio = _estimate_tokens(items) / float(ctx_window)
            if hard_ceiling and ratio >= hard_ceiling:
                # 压缩前先 Prune-First：修剪超长 tool 输出（仅作用后续请求输入）
                items, _ = _prune_tool_results(items, prune_threshold, prune_head, prune_tail)
                # 修剪后若已低于压缩阈值：纯截断已够，跳过摘要（不调 _maybe_compress）
                if (not compress_threshold) or (_estimate_tokens(items) / float(ctx_window)) >= compress_threshold:
                    items = _maybe_compress(items, compress_after or 8, summarize) \
                        if summarize else _hard_keep(items, compress_after or 8)
            elif compress_threshold and ratio >= compress_threshold and summarize:
                items, _ = _prune_tool_results(items, prune_threshold, prune_head, prune_tail)
                if _estimate_tokens(items) / float(ctx_window) >= compress_threshold:
                    items = _maybe_compress(items, compress_after or 8, summarize)
        elif compress_after and summarize:
            # 退化为轮数压缩分支（无 ctx_window 占比概念）：先修剪超长 output，再按轮数压缩
            items, _ = _prune_tool_results(items, prune_threshold, prune_head, prune_tail)
            items = _maybe_compress(items, compress_after, summarize)

    # 走到这里 = 步数预算耗尽（未 task_done、未 verify 通过、未升级）。
    # 与旧循环一致：预算耗尽即失败，并写 budget_exhausted 供编排层终止。
    return _result(False, f"budget_exhausted: 已达步数上限 {max_steps}", state.steps, False, "", llm_calls=state.llm_calls, repeat_failures=state.repeat_failures)


def _maybe_compress(items: List[Any], keep: int, summarize: Callable[[List[Any]], Optional[str]]) -> List[Any]:
    """历史超过 keep 轮时，把较早轮次压成一条摘要消息（防上下文溢出）。"""
    # “一轮” = 模型的一次动作（function_call）；超 keep 轮则压缩较早历史
    user_idx = [i for i, it in enumerate(items) if _kind(it) == "function_call"]
    if len(user_idx) <= keep:
        return items
    cut = user_idx[-keep]
    old, recent = items[:cut], items[cut:]
    try:
        summary = summarize(old)
    except Exception:
        return items
    if not summary:
        return items
    return [{"role": "user", "content": f"[历史压缩摘要] {summary}"}] + recent


def _hard_keep(items: List[Any], keep: int) -> List[Any]:
    """硬滑窗（N7 worker）：只保留最近 keep 轮 function_call，更早的无条件丢弃。

    本地小模型无摘要能力，溢出只能靠裁剪；约束块走 system prompt（instructions）
    不在此 items 内，故可安全截断。
    """
    user_idx = [i for i, it in enumerate(items) if _kind(it) == "function_call"]
    if len(user_idx) <= keep:
        return items
    cut = user_idx[-keep]
    return items[cut:]


def _prune_tool_results(items: List[Any], threshold: int, head: int, tail: int) -> Tuple[List[Any], int]:
    """Prune-First：压缩前先对超长 tool 输出做首尾截断。

    仅处理 ``function_call_output`` 的 ``output`` 字段；字段长度超过 ``threshold``
    字符时截断为「头部 head 字符 + 省略标记 + 尾部 tail 字符」。严格按码点
    （``list(text)``）切分，规避 UTF-16 代理对断裂。返回 ``(新items, 修剪条数)``，
    **绝不修改入参**，仅作用于后续模型请求输入（阈值以下零干预）。
    """
    if threshold <= 0:
        return list(items), 0
    ellipsis = "\n…[已截断过长工具输出]…\n"
    out: List[Any] = []
    pruned = 0
    for it in items:
        if _kind(it) != "function_call_output":
            out.append(it)
            continue
        # 取出 output 文本（dict 用 .get，对象用 getattr）
        if isinstance(it, dict):
            text = it.get("output")
        else:
            text = getattr(it, "output", None)
        if not isinstance(text, str) or len(text) <= threshold:
            out.append(it)
            continue
        chars = list(text)  # 按码点切分，规避代理对
        h = "".join(chars[:head]) if head > 0 else "".join(chars[:threshold])
        t = "".join(chars[-tail:]) if tail > 0 else ""
        new_text = h + ellipsis + t
        # 构造新 item，不修改原对象
        if isinstance(it, dict):
            new_it = dict(it)
            new_it["output"] = new_text
        else:
            try:
                new_it = copy.copy(it)
                new_it.output = new_text
            except Exception:
                new_it = {
                    "type": "function_call_output",
                    "call_id": getattr(it, "call_id", ""),
                    "output": new_text,
                }
        out.append(new_it)
        pruned += 1
    return out, pruned


def _estimate_tokens(items: List[Any]) -> int:
    """启发式 token 估算（N7 token 占比触发用）。

    不依赖 tiktoken：按字符数 / 4 估算（中英文混排足够做占比判断）。
    SDK item 可能是 dict 或对象，兼容两者取文本内容。

    依次累加四类文本字段：message 的 content / name，工具调用的
    function_call.arguments 与 function_call_output.output。原实现仅统计
    content / name，漏算 arguments / output，导致工具参数或返回超长时
    token 估算严重偏低、压缩阈值永不触发（上下文溢出风险）。
    """
    total = 0
    for it in items:
        # 依次累加 content / name / arguments / output 四个字段的有效文本；
        # 字段不存在（None / 空串）则跳过，字典用 .get、对象用 getattr。
        if isinstance(it, dict):
            parts = (
                it.get("content"),
                it.get("name"),
                it.get("arguments"),
                it.get("output"),
            )
        else:
            parts = (
                getattr(it, "content", None),
                getattr(it, "name", None),
                getattr(it, "arguments", None),
                getattr(it, "output", None),
            )
        text = "".join(str(p) for p in parts if p)
        total += max(1, len(text) // 4)
    return total


def _kind(item: Any) -> str:
    if isinstance(item, dict):
        return item.get("type") or item.get("role") or ""
    return getattr(item, "type", None) or getattr(item, "role", None) or ""


# --- A1+A2（2026-09-21 崩溃自愈）---------------------------------------------------
# 重试上限：防弱模型陷入无效重试死循环。
LENGTH_RETRY_LIMIT = 3
TOOL_NOT_FOUND_RETRY_LIMIT = 3

# A1 主修复：官方自愈通道——把「工具不存在」作为 function_call_output 回给模型，run 不中断、
# 模型在 run 内自纠（替代 SDK 默认 raise_error 直接抛 ModelBehaviorError 崩子任务）。
# 三处 Runner 调用统一复用同一实例。
_TOOL_NOT_FOUND_RUN_CONFIG = RunConfig(tool_not_found_behavior="return_error_to_model")

# A2 纠正提示：针对输出侧 length 截断（_hard_keep 只压 input，救不了 output，故靠提示跳出空转）。
_LEN_TRUNC_MSG = (
    "上一轮输出因长度限制被截断且未生成有效内容。请精简回答、分步输出，"
    "不要一次性生成过长文本或过长工具参数。"
)


def _append_runtime_note(items: List[Any], note: str) -> List[Any]:
    """往 SDK 输入列表追加一条 user 纠正提示（dict 形态，与既有 items 一致，<kind> 兼容）。"""
    items = list(items)
    items.append({"role": "user", "content": note})
    return items


def _result(success: bool, reason: str, steps: int, escalated: bool, escalate_reason: str,
            provider_error: bool = False, llm_calls: int = 0, repeat_failures: int = 0,
            paused: bool = False) -> Dict[str, Any]:
    return {
        "success": success,
        "reason": reason,
        "steps": steps,
        "escalated": escalated,
        "escalate_reason": escalate_reason,
        "provider_error": bool(provider_error),
        "llm_calls": int(llm_calls or 0),
        "repeat_failures": int(repeat_failures or 0),
        "paused": bool(paused),
    }
