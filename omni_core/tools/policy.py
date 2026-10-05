"""安全与权限策略层（S0/S2 门本体）。

分层（内核不碰 SSE）：
- **S0 权限模式**：``standard | read_only | full_access``（config ``security.mode``，随
  run 快照进入 SecurityContext）⊕ per-task ``full_access``。read_only 拒绝
  exec/network/actuate（危险动作自动拒绝，fail-closed）；``full_access`` 档=全局免审批
  （所有任务/run 危险动作直接放行，与 per-task ``full_access`` 取「或」，任一为真即免审）。
- **S2 审批**：standard 档下 exec/actuate 逐卡人工批准（任务级类记忆：一次同意覆盖
  该类全部工具）；``ApprovalSink`` 协议由后端注册实现（SSE 推卡 + REST 决议）；未注册
  （单测 / 脚本直调）→ ``AutoDenySink`` 立即拒绝，fail-closed 且不挂测试。
- **写入不设防**：S1 路径围栏已整体删除（含允许根/绝对拒绝区/按名写拒）——个人助手
  定位下文件写入不做路径门；可见性靠审计，风险靠 S0 档位与危险动作审批把关。
- **审计**：门的干预落 ``~/.omniagent/audit/YYYY-MM.jsonl``（月轮转）。

红线：内核零工具名字面量（风险类别由工具经 ``risk=`` 自声明）；纯标准库，可上机复用。
"""
from __future__ import annotations

import functools
import inspect
import json
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional


# ---------------------------------------------------------------------------
# 拒绝异常（统一出口：base.py 包裹层捕获 → 结构化结果 + 审计）
# ---------------------------------------------------------------------------
class PolicyRefusal(Exception):
    """S2 拒绝。包裹层据此产出 ``{"ok": False, "denied": True, ...}`` 回给模型。"""

    def __init__(self, policy: str, rule: str, error: str, hint: str):
        super().__init__(error)
        self.policy = policy
        self.rule = rule
        self.error = error
        self.hint = hint


_REFUSAL_HINT = "不要重试或绕过；改用任务目录，或向用户说明需要人工处理"


def refusal_result(r: PolicyRefusal) -> Dict[str, Any]:
    """PolicyRefusal → 回给模型的结构化拒绝结果（S2 共用契约）。"""
    return {"ok": False, "denied": True, "policy": r.policy, "rule": r.rule,
            "error": r.error, "hint": r.hint}


# ---------------------------------------------------------------------------
# S0：运行期安全上下文（run 起始由 graph_runner 绑定，随 run 快照冻结）
# ---------------------------------------------------------------------------
@dataclass
class SecurityContext:
    """一次 run 的安全上下文（从运行级配置快照一次性凝固，run 内不变）。"""

    task_id: str
    run_id: str = ""
    full_access: bool = False
    mode: str = "standard"                       # standard | read_only | full_access
    wait_seconds: float = 600.0                  # 0 = 无限等
    audit_enabled: bool = True
    # 审批等待累计（秒）：墙钟结算时扣除（等待是人的延迟，不是 agent 的延迟）
    _gate_wait: float = 0.0
    _gate_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_gate_wait(self, seconds: float) -> None:
        with self._gate_lock:
            self._gate_wait += max(0.0, seconds)

    @property
    def gate_wait_seconds(self) -> float:
        with self._gate_lock:
            return self._gate_wait


_run_security: ContextVar[Optional[SecurityContext]] = ContextVar("omni_security", default=None)


def bind(task_id: str, full_access: bool = False,
         security_cfg: Optional[Dict[str, Any]] = None, run_id: str = "") -> SecurityContext:
    """run 起始绑定（graph_runner 与 workspace.set_task 同点调用）。"""
    cfg = security_cfg or {}
    approval_cfg = (cfg.get("approval") or {})
    try:
        wait_seconds = float(approval_cfg.get("wait_seconds", 600) or 0)
    except (TypeError, ValueError):
        wait_seconds = 600.0
    mode = str(cfg.get("mode") or "standard")
    # 全局完全访问档：所有任务/run 免审批（与 per-task full_access 取「或」，任一为真即免审）
    full = bool(full_access) or (mode == "full_access")
    ctx = SecurityContext(
        task_id=task_id or "",
        run_id=run_id or f"r_{int(time.time())}",
        full_access=full,
        mode=mode,
        wait_seconds=max(0.0, wait_seconds),
        audit_enabled=bool(cfg.get("audit", True)),
    )
    _run_security.set(ctx)
    return ctx


def unbind() -> None:
    """run 结束解绑（graph_runner finally 与 set_task(None) 同点）。"""
    _run_security.set(None)


def current() -> Optional[SecurityContext]:
    return _run_security.get()


def gate_wait_seconds() -> float:
    """本 run 累计的审批等待秒数（墙钟结算扣除用；未绑定为 0）。"""
    ctx = _run_security.get()
    return ctx.gate_wait_seconds if ctx else 0.0


# ---------------------------------------------------------------------------
# S2：审批 sink 协议 + 门
# ---------------------------------------------------------------------------
@dataclass
class ApprovalCard:
    """推给前端的审批卡（SSE ``approval`` 事件载荷 / REST 决议寻址键）。"""

    approval_id: str = field(default_factory=lambda: f"ap_{int(time.time() * 1000)}_{id(threading.current_thread()) % 10000}")
    task_id: str = ""
    run_id: str = ""
    tool: str = ""
    unit: str = ""
    risk: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    wait_seconds: float = 600.0


@dataclass
class Decision:
    approved: bool
    rule: str = ""          # user_deny | timeout | cancelled | auto_deny
    remember: bool = False
    wait_ms: int = 0


class ApprovalSink:
    """审批后端协议（后端注册实现；内核零 SSE 依赖）。"""

    def request(self, card: ApprovalCard) -> Decision:
        raise NotImplementedError

    def remembered(self, task_id: str, key: tuple) -> bool:
        return False

    def remember(self, task_id: str, key: tuple) -> None:
        raise NotImplementedError


class AutoDenySink(ApprovalSink):
    """未注册 sink（单测 / 脚本直调工具）的兜底：立即拒绝 + 审计，fail-closed 且不挂测试。"""

    def request(self, card: ApprovalCard) -> Decision:
        return Decision(approved=False, rule="auto_deny")

    def remember(self, task_id: str, key: tuple) -> None:
        pass


_sink: Optional[ApprovalSink] = None
_sink_lock = threading.Lock()


def set_sink(sink: Optional[ApprovalSink]) -> None:
    """注册审批后端（backend.server 启动时调；传 None 回落 AutoDeny）。"""
    global _sink
    with _sink_lock:
        _sink = sink


def _get_sink() -> Optional[ApprovalSink]:
    with _sink_lock:
        return _sink


def _request_approval(card: ApprovalCard) -> Decision:
    """阻塞请求人工决议。未绑定 run / 未注册 sink → AutoDeny（fail-closed）。"""
    ctx = current()
    sink = _get_sink()
    if ctx is None or sink is None:
        return Decision(approved=False, rule="auto_deny")
    card.task_id = ctx.task_id
    card.run_id = ctx.run_id
    card.wait_seconds = ctx.wait_seconds
    t0 = time.time()
    try:
        decision = sink.request(card)
    except Exception:
        decision = Decision(approved=False, rule="auto_deny")
    waited = time.time() - t0
    ctx.add_gate_wait(waited)
    if decision is None:
        decision = Decision(approved=False, rule="auto_deny")
    decision.wait_ms = int(waited * 1000)
    return decision


def _pre_gate(risk: str, tool: str, unit: str,
              arguments: Dict[str, Any], ctx: Optional[SecurityContext]) -> Optional[PolicyRefusal]:
    """前置门（risk 声明类）。返回 None=放行；返回 PolicyRefusal=拒绝（已审计）。"""
    mode = ctx.mode if ctx else "standard"
    full = bool(ctx.full_access) if ctx else False

    if risk == "network":
        # 已裁决（§6.5）：standard 放行（web 插件开关=常驻同意）；read_only 拒
        if mode == "read_only" and not full:
            audit("mode_read_only", "mode_read_only", tool=tool, unit=unit,
                  risk=risk, arguments=arguments)
            return PolicyRefusal(
                "S0", "mode_read_only",
                "当前为只读模式，网络访问已被拒绝",
                "只读模式禁止网络出站；如需联网请用户切换权限模式",
            )
        return None

    # exec / actuate（及其它声明类）
    if full:
        return None
    if mode == "read_only":
        audit("mode_read_only", "mode_read_only", tool=tool, unit=unit,
              risk=risk, arguments=arguments)
        return PolicyRefusal(
            "S0", "mode_read_only",
            "当前为只读模式，该操作已被拒绝",
            "只读模式仅允许读取与观察；如需执行请用户切换权限模式",
        )
    sink = _get_sink()
    if ctx is None or sink is None:
        audit("auto_deny", "auto_deny", tool=tool, unit=unit,
              risk=risk, arguments=arguments)
        return PolicyRefusal(
            "S2", "auto_deny",
            "审批通道不可用（未注册审批 sink），已拒绝执行",
            "直调环境请注册放行 sink 或在 config 中调整 security 设置",
        )
    # 任务级类记忆：一次同意覆盖该类全部工具（键鼠任务不应每工具一张卡）
    key = (risk,)
    if sink.remembered(ctx.task_id, key):
        return None
    card = ApprovalCard(tool=tool, unit=unit, risk=risk, arguments=arguments)
    decision = _request_approval(card)
    if decision.approved:
        audit("approved", "user_approve", tool=tool, unit=unit,
              risk=risk, arguments=arguments, wait_ms=decision.wait_ms)
        if decision.remember:
            sink.remember(ctx.task_id, key)
        return None
    rule = decision.rule or "user_deny"
    audit(rule, rule, tool=tool, unit=unit, risk=risk,
          arguments=arguments, wait_ms=decision.wait_ms)
    texts = {
        "user_deny": ("用户拒绝了该操作", "停止该方向的操作，或改用无需该权限的替代方案；不要重复尝试同一操作"),
        "timeout": ("审批等待超时，操作已被拒绝", "不要等待或重试；向用户说明需要人工确认后继续"),
        "cancelled": ("任务已停止，操作未执行", "任务正在中断，无需进一步操作"),
        "auto_deny": ("审批通道不可用，操作已被拒绝", "向用户说明需要人工确认"),
    }
    err, hint = texts.get(rule, ("操作未被批准", _REFUSAL_HINT))
    return PolicyRefusal("S2", rule, err, hint)


def wrap_tool(func: Callable, tool_name: str, unit: str,
              risk: Optional[str] = None) -> Callable:
    """base.py::function_tool 的统一包裹层（插件/环境作者零感知、无法绕过）。

    - risk 声明类（exec/actuate/network）：执行前过前置门；
    - 捕获工具内部抛出的 ``PolicyRefusal``（兜底；当前门均在本层前置）→ 结构化结果；
    - ``functools.wraps`` 保全 SDK schema 生成（签名/注解/docstring 原样）。

    注意：SDK 对 ``strict_mode=False`` 的工具按**位置参数**传参（kwargs 为空），
    统一经 ``signature.bind`` 回填成名参，审批卡与审计才有人可读的参数。
    """
    @functools.wraps(func)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = inspect.signature(func).bind(*args, **kwargs)
            bound.apply_defaults()
            call_args: Dict[str, Any] = dict(bound.arguments)
        except Exception:
            call_args = dict(kwargs or {})
        ctx = current()
        if risk:
            refusal = _pre_gate(risk, tool_name, unit, call_args, ctx)
            if refusal is not None:
                return refusal_result(refusal)
        try:
            return func(*args, **kwargs)
        except PolicyRefusal as r:
            audit(r.rule, r.rule, tool=tool_name, unit=unit, risk=risk or "",
                  arguments=call_args)
            return refusal_result(r)
    return guarded


# ---------------------------------------------------------------------------
# 审计（门的干预才记）
# ---------------------------------------------------------------------------
def audit(decision: str, rule: str, tool: str = "", unit: str = "", risk: str = "",
          arguments: Optional[Dict[str, Any]] = None, wait_ms: int = 0) -> None:
    """落 ``~/.omniagent/audit/YYYY-MM.jsonl``。失败静默（绝不影响工具流程）。"""
    ctx = current()
    if ctx is not None and not ctx.audit_enabled:
        return
    try:
        entry = {
            "ts": time.time(),
            "task_id": ctx.task_id if ctx else "",
            "run_id": ctx.run_id if ctx else "",
            "tool": tool, "unit": unit, "risk": risk,
            "arguments": _clip_json(arguments),
            "decision": decision, "rule": rule,
            "cwd": str(Path.cwd()),
            "wait_ms": wait_ms,
        }
        d = audit_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / (time.strftime("%Y-%m") + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def audit_dir() -> Path:
    """审计落盘目录（供后端只读 API 消费）；与保护根同源（隔离夹具友好）。"""
    from omni_core.local import runtime_paths as RP
    return RP.global_omni() / "audit"


def _clip_json(arguments: Optional[Dict[str, Any]], limit: int = 2000) -> str:
    try:
        s = json.dumps(arguments or {}, ensure_ascii=False)
    except Exception:
        s = str(arguments)
    return s if len(s) <= limit else s[:limit] + "...[截断]"
