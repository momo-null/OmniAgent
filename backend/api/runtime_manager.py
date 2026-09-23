"""阶段 1：运行时上下文与运行体管理（RuntimeContext + RuntimeManager）。

把阶段 0.5 的「模块级全局 dict（_task_runs / _task_outboxes / _global_lock）」
收敛为显式的运行体管理器：所有任务启动、取消、SSE、outbox 隔离都经
RuntimeManager 以复合键 (task_id, agent_id) 寻址（复合键在阶段 0.5 已确立，
本阶段把它从裸 dict 升级为受控对象）。

后续阶段会把工具 registry、设备后端、事件 sink、配置快照等也从模块级可变对象
迁移进 RuntimeContext（阶段 1 目标 3）；本模块先固化「寻址 + 生命周期 + outbox」
这一层，行为对 router_runtime 调用方保持等价。
"""
from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 全局唯一主 agent 名称（阶段 0.5：单任务单 agent 的默认身份；
# 未来常驻协作可扩展 researcher/executor）。
AGENT_MAIN = "main"

# task-scoped outbox 通道（与前端 SSE 事件类型一一对应）。
_OUTBOX_KEYS = ("chat", "debug", "thinking", "toolcall", "message")


@dataclass
class RuntimeContext:
    """一次运行体的完整上下文（阶段 1 收敛目标的最小可用形态）。

    字段语义对齐架构评审文档「阶段 0.5 / 阶段 1」：
    - task_id / agent_id / run_id：复合键 (task_id, agent_id)；run_id 表一次执行实例。
    - running / started_at / state：生命周期。
    - loop / cancellation_token：运行体句柄与取消令牌。
    - config_snapshot / config_hash：运行级不可变配置快照（阶段 0.5 注入）。
    - outbox：预留——task-scoped 事件队列当前由 RuntimeManager 按 task_id 单独托管，
      后续可并入此处（阶段 1 目标 3：事件 sink 不再依赖模块级可变对象）。
    - enabled_capability_groups：预留（阶段 2 能力模型）。
    """

    task_id: str = ""
    agent_id: str = AGENT_MAIN
    project_id: str = ""
    run_id: str = ""
    running: bool = False
    started_at: float = 0.0
    state: str = "INIT"
    loop: Any = None
    config_snapshot: Optional[Dict[str, Any]] = None
    config_hash: str = ""
    cancellation_token: Any = None
    enabled_capability_groups: List[str] = field(default_factory=list)
    # 阶段 1 目标 3：工具 registry 与设备后端实例由运行体携带，不再隐式依赖模块级全局对象。
    tool_registry: Any = None
    execution_backend: Any = None

    def export(self) -> Dict[str, Any]:
        """可完整导出运行配置（阶段 1 验收：每个 run 可完整导出运行配置）。"""
        _reg = self.tool_registry
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "running": self.running,
            "started_at": self.started_at,
            "state": self.state,
            "config_hash": self.config_hash,
            "enabled_capability_groups": list(self.enabled_capability_groups),
            "tool_names": sorted(_reg.keys()) if _reg else [],
            "execution_backend": getattr(self.execution_backend, "backend_kind", "") or "",
        }


class RuntimeManager:
    """运行体表（复合键 (task_id, agent_id) -> RuntimeContext）与 outbox 注册表。

    线程安全：所有读写经内部锁。当前保持单任务串行互斥（阶段 0.5 / 1 语义），
    未来多 Agent 常驻协作可直接复用此结构与 RuntimeContext 字段。
    """

    def __init__(self) -> None:
        self._runs: Dict[Tuple[str, str], RuntimeContext] = {}
        self._outboxes: Dict[str, Dict[str, "collections.deque"]] = {}
        self._lock = threading.Lock()

    # --- 生命周期（复合键寻址） ---
    def try_start(self, task_id: str, agent_id: str = AGENT_MAIN) -> bool:
        """原子地标记 (task_id, agent_id) 为 running。已有运行体在跑返回 False。"""
        with self._lock:
            if any(r.running for r in self._runs.values()):
                return False
            self._runs[(task_id, agent_id)] = RuntimeContext(
                task_id=task_id,
                agent_id=agent_id,
                run_id=f"r_{int(time.time())}",
                running=True,
                started_at=time.time(),
            )
            return True

    def finish(self, task_id: str, agent_id: str = AGENT_MAIN) -> None:
        """标记 (task_id, agent_id) 为非运行。"""
        with self._lock:
            rec = self._runs.get((task_id, agent_id))
            if rec is not None:
                rec.running = False

    def is_any_running(self) -> bool:
        with self._lock:
            return any(r.running for r in self._runs.values())

    def is_running(self, task_id: str, agent_id: str = AGENT_MAIN) -> bool:
        with self._lock:
            rec = self._runs.get((task_id, agent_id))
            return bool(rec and rec.running)

    def running_task_id(self) -> Optional[str]:
        """返回当前运行中的 task_id（单任务互斥，至多一个）。"""
        with self._lock:
            for r in self._runs.values():
                if r.running:
                    return r.task_id
            return None

    def get(self, task_id: str, agent_id: str = AGENT_MAIN) -> Optional[RuntimeContext]:
        with self._lock:
            return self._runs.get((task_id, agent_id))

    def pop(self, task_id: str, agent_id: str = AGENT_MAIN) -> None:
        with self._lock:
            self._runs.pop((task_id, agent_id), None)

    # --- outbox（task-scoped 事件队列，按 task_id 隔离） ---
    def ensure_outbox(self, task_id: str) -> Dict[str, "collections.deque"]:
        if not task_id:
            task_id = "_global"
        with self._lock:
            if task_id not in self._outboxes:
                self._outboxes[task_id] = {
                    k: collections.deque(maxlen=300) for k in _OUTBOX_KEYS
                }
            return self._outboxes[task_id]

    @property
    def runs(self) -> Dict[Tuple[str, str], RuntimeContext]:
        """只读视图：供测试与诊断按复合键遍历运行体。"""
        return self._runs


# 模块级单例（替代原 router_runtime 的全局 dict；保持行为等价）。
manager = RuntimeManager()
