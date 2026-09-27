"""安全与权限策略层（S0/S1/S2 门本体）。

分层（内核不碰 SSE）：
- **S0 权限模式**：``standard | read_only``（config ``security.mode``，随 run 快照进入
  SecurityContext）⊕ per-task ``full_access``。read_only 下危险动作自动拒绝（fail-closed）。
- **S1 PathPolicy**：绝对拒绝区（``guard_path``，接在 ``workspace.resolve_path`` 路径漏斗）
  + 允许根（``ensure_writable``，写类工具入口在任何副作用前调用）。
- **S2 审批**：``ApprovalSink`` 协议由后端注册实现（SSE 推卡 + REST 决议）；未注册
  （单测 / 脚本直调）→ ``AutoDenySink`` 立即拒绝，fail-closed 且不挂测试。
- **审计**：门的干预落 ``~/.omniagent/audit/YYYY-MM.jsonl``（月轮转），根内静默放行不记。

红线：内核零工具名字面量（风险类别由工具经 ``risk=`` 自声明）；纯标准库，可上机复用。
约定：``ensure_writable`` 必须在工具首个副作用之前调用（阻塞式审批因此无需重入函数）。
"""
from __future__ import annotations

import fnmatch
import functools
import inspect
import json
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from omni_core.local.runtime_paths import task_tmp


# ---------------------------------------------------------------------------
# 拒绝异常（统一出口：base.py 包裹层捕获 → 结构化结果 + 审计）
# ---------------------------------------------------------------------------
class PolicyRefusal(Exception):
    """S1/S2 拒绝。包裹层据此产出 ``{"ok": False, "denied": True, ...}`` 回给模型。"""

    def __init__(self, policy: str, rule: str, error: str, hint: str):
        super().__init__(error)
        self.policy = policy
        self.rule = rule
        self.error = error
        self.hint = hint


_REFUSAL_HINT = "不要重试或绕过；改用任务目录，或向用户说明需要人工处理"


def refusal_result(r: PolicyRefusal) -> Dict[str, Any]:
    """PolicyRefusal → 回给模型的结构化拒绝结果（S1/S2 共用契约）。"""
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
    mode: str = "standard"                       # standard | read_only
    allow_roots: List[str] = field(default_factory=list)
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
    roots = cfg.get("allow_write_roots") or []
    ctx = SecurityContext(
        task_id=task_id or "",
        run_id=run_id or f"r_{int(time.time())}",
        full_access=bool(full_access),
        mode=str(cfg.get("mode") or "standard"),
        allow_roots=[str(r) for r in roots if str(r).strip()],
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
# S1：PathPolicy 路径围栏
# ---------------------------------------------------------------------------
_HOME = Path.home()
# 保护根 / 审计目录以 runtime_paths._GLOBAL 为单一事实源（生产 = ~/.omniagent；
# 测试经 conftest 隔离夹具重定向到 tmp——判定必须随隔离走，不得 import 时固化）。
# 预解析防 `..`/符号链接词法穿透：判定一律基于 normalize 后的真实路径。
_CARRIER_GLOBS = ("config*.yaml", "mcp.json")
_DISCIPLINE_NAME = "agents.md"


def _protected_root() -> Optional[Path]:
    """受保护的全局数据根（normalize 后）；解析失败按拒绝处理。"""
    from omni_core.local import runtime_paths as RP
    return _resolve(RP.global_omni())


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve(p: Path) -> Optional[Path]:
    """normalize 真实路径（含 ``..`` / 符号链接）；失败返回 None（调用方按拒绝处理）。"""
    try:
        return Path(p).expanduser().resolve()
    except Exception:
        return None


def _task_tmp() -> Optional[Path]:
    """当前任务 tmp 目录；未绑定任务时 None（此时无例外区）。

    以 workspace 的 ContextVar 为**单一事实源**（与各工具的路径解析同源，
    避免双绑定不一致）；``bind`` 存的 task_id 供卡片 / 审计用，不参与围栏例外判定。
    """
    from omni_core.tools.workspace import current_task
    tid = current_task()
    if tid:
        return task_tmp(tid)
    return None


def guard_path(p: Path) -> None:
    """S1 绝对拒绝区（读写都拒）：``~/.omniagent/**``，唯一例外=当前任务 ``tmp/``。

    接在 ``workspace.resolve_path`` 漏斗——一切经它解析路径的工具零改动被覆盖。
    命中抛 ``PolicyRefusal``（rule=self_carrier），包裹层产出结构化拒绝 + 审计。
    """
    rp = _resolve(p)
    if rp is None:
        raise PolicyRefusal("S1", "self_carrier",
                            f"路径无法解析，已拒绝: {p}", _REFUSAL_HINT)
    root = _protected_root()
    if root is None or not _is_under(rp, root):
        return
    tmp = _task_tmp()
    if tmp:
        tmp_r = _resolve(tmp)
        if tmp_r and _is_under(rp, tmp_r):
            return
    raise PolicyRefusal(
        "S1", "self_carrier",
        f"路径受保护（OmniAgent 自身配置/记忆），已拒绝: {p}",
        _REFUSAL_HINT,
    )


def _is_write_carrier(p: Path) -> Optional[str]:
    """全盘写拒绝的载体（不问地点）。命中返回 rule，否则 None。"""
    name = p.name.lower()
    if name == _DISCIPLINE_NAME:
        return "discipline_file"
    for g in _CARRIER_GLOBS:
        if fnmatch.fnmatch(name, g):
            return "carrier_write"
    return None


def _in_allow_root(p: Path, ctx: Optional[SecurityContext]) -> bool:
    rp = _resolve(p)
    if rp is None:
        return False
    tmp = _task_tmp()
    if tmp:
        tmp_r = _resolve(tmp)
        if tmp_r and _is_under(rp, tmp_r):
            return True
    if ctx:
        for root in ctx.allow_roots:
            root_r = _resolve(Path(root))
            if root_r and _is_under(rp, root_r):
                return True
    return False


def ensure_writable(p: Path) -> None:
    """S1 写入口（写类工具在任何副作用前调用）。

    - 绝对拒绝区 → 拒（rule=self_carrier）；
    - 载体文件（config*.yaml / mcp.json / AGENTS.md，全盘）→ 拒；
    - 允许根内（当前任务 tmp + ``security.allow_write_roots``）→ 放行；
    - 根外 → 阻塞走 S2 审批：批准后继续执行，拒绝抛 ``PolicyRefusal``。
    """
    p = Path(p)
    guard_path(p)
    rule = _is_write_carrier(p)
    if rule:
        raise PolicyRefusal(
            "S1", rule,
            f"受保护文件，禁止写入: {p}",
            _REFUSAL_HINT,
        )
    ctx = current()
    if _in_allow_root(p, ctx):
        return
    # read_only 档：根外写入自动拒绝（不弹卡，fail-closed；§4）
    if ctx is not None and ctx.mode == "read_only" and not ctx.full_access:
        audit("mode_read_only", "mode_read_only", tool=_current_invocation().get("tool", ""),
              unit=_current_invocation().get("unit", ""), risk="write",
              arguments={"path": str(p)})
        raise PolicyRefusal(
            "S0", "mode_read_only",
            f"当前为只读模式，拒绝写入允许根之外的路径: {p}",
            "只读模式仅允许任务目录内的写入；如需写入该路径请用户切换权限模式",
        )
    # 根外写入 → S2 审批（人点头即放行；个人助手整理用户文件是正当需求）
    inv = _current_invocation()
    card = ApprovalCard(
        tool=inv.get("tool", ""), unit=inv.get("unit", ""), risk="write",
        arguments={"path": str(p)},
    )
    decision = _request_approval(card)
    if decision.approved:
        audit("approved", "allow_root", tool=card.tool, unit=card.unit,
              risk="write", arguments=card.arguments, wait_ms=decision.wait_ms)
        return
    raise PolicyRefusal(
        "S2", decision.rule or "user_deny",
        f"用户未允许写入该路径: {p}",
        _REFUSAL_HINT,
    )


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


# 当前工具调用上下文（wrap_tool 设置，供 ensure_writable 组卡 / 审计用）
_invocation: ContextVar[Dict[str, str]] = ContextVar("omni_invocation", default={})


def _current_invocation() -> Dict[str, str]:
    return _invocation.get() or {}


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
    - 捕获工具内部抛出的 ``PolicyRefusal``（ensure_writable / guard_path）→ 结构化结果；
    - ``functools.wraps`` 保全 SDK schema 生成（签名/注解/docstring 原样）。

    注意：SDK 对 ``strict_mode=False`` 的工具按**位置参数**传参（kwargs 为空），
    统一经 ``signature.bind`` 回填成名参，审批卡与审计才有人可读的参数。
    """
    @functools.wraps(func)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        token = _invocation.set({"tool": tool_name, "unit": unit, "risk": risk or ""})
        try:
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
                decision = {
                    "self_carrier": "denied_s1", "carrier_write": "denied_s1",
                    "discipline_file": "denied_s1",
                }.get(r.rule, r.rule)
                audit(decision, r.rule, tool=tool_name, unit=unit, risk=risk or "",
                      arguments=call_args)
                return refusal_result(r)
        finally:
            _invocation.reset(token)
    return guarded


# ---------------------------------------------------------------------------
# 审计（门的干预才记；根内静默放行不记）
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
