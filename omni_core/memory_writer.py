"""记忆写链异步化（plan §3.3）：**收尾只入队，不进主链路**。

为什么必须异步：``memory_brain.flush_brain`` 会串行跑 3 次 LLM（提炼 / 价值判 /
一致性判），之后还有 L2 场景维护（agent 循环，墙钟上限 300s）。实测收尾段
0~7.4s、最坏 45s+。语音每说一句话都吃这个尾巴 → 必然卡顿。

形态（与 plan 一节一一对应）：

    收尾 → 入队 {msgs, persona, project, source_task, did_work}
         → 单 worker 串行消费：
              flush_brain(...)                    # L1 永远做（价值判是唯一守门人）
              → scene_executor.maybe_maintain(allow_write=did_work)   # 由 flush_brain 内部调
              → maybe_consolidate(...)                                 # 同上

不变量：
1. **单 worker 串行**——写链内部有多段 LLM + SQLite 写，并发只会带来锁竞争与
   语义竞态（同一句事实两次判重互相看不见）。串行比快更重要。
2. **入队永不阻塞、永不抛**：队列满就丢最旧并计数（写链是增强项，不是主链路依赖）。
3. ``capture`` 缓冲必须在**入队时**取走（``pop_capture``），否则 worker 醒来时会把
   下一轮的消息也算进来，轮次边界就糊了。
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from utils import get_logger

logger = get_logger("memory_writer")

_Q_MAX = 64
_q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=_Q_MAX)
_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()

_stats = {"enqueued": 0, "processed": 0, "failed": 0, "dropped": 0,
          "busy_ms": 0, "last_busy_ms": 0}
_last_result: Dict[str, Any] = {}


def _ensure_worker() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_worker, name="memory-writer", daemon=True)
        _thread.start()


def enqueue(project_id: str, brain_cfg: Optional[Dict[str, Any]] = None,
            source_task: str = "", persona: str = "",
            msgs: Optional[List[Dict[str, str]]] = None,
            did_work: bool = True,
            on_done: Optional[Callable[[Dict[str, Any]], None]] = None) -> bool:
    """把一轮收尾压成一条队列项（毫秒级返回）。返回是否入队成功。

    ``msgs`` 为 ``None`` 时在**本函数内**取走 capture 缓冲（入队线程 = 收尾线程，
    取走是纯内存操作），保证轮次边界清晰。
    """
    try:
        from omni_core import memory_brain

        if msgs is None:
            msgs = memory_brain.pop_capture(project_id)
        item = {"project_id": project_id, "brain_cfg": brain_cfg,
                "source_task": source_task, "persona": persona, "msgs": msgs,
                "did_work": bool(did_work),
                "on_done": on_done, "ts": time.time()}
        _ensure_worker()
        try:
            _q.put_nowait(item)
        except queue.Full:
            # 队列满 = 写链跟不上（模型慢/轮次太密）：丢最旧的，保住最新的语义
            try:
                _q.get_nowait()
                _q.task_done()
                _stats["dropped"] += 1
            except Exception:
                pass
            _q.put_nowait(item)
        _stats["enqueued"] += 1
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("记忆写链入队失败（忽略，不影响本轮）：%s: %s", type(e).__name__, e)
        return False


def _worker() -> None:
    while True:
        item = _q.get()
        try:
            if item is None:
                return
            t0 = time.time()
            stats = _process(item)
            busy = int((time.time() - t0) * 1000)
            _stats["busy_ms"] = int(_stats.get("busy_ms", 0)) + busy
            _stats["last_busy_ms"] = busy
            _stats["processed"] += 1
            _last_result.clear()
            _last_result.update({"task": item.get("source_task", ""),
                                 "stats": stats, "busy_ms": busy,
                                 "ts": time.time()})
            cb = item.get("on_done")
            if cb:
                try:
                    cb(stats)
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            _stats["failed"] += 1
            logger.warning("记忆写链处理失败（已跳过本轮）：%s: %s", type(e).__name__, e)
        finally:
            try:
                _q.task_done()
            except Exception:
                pass


def _process(item: Dict[str, Any]) -> Dict[str, int]:
    from omni_core import memory_brain

    stats = memory_brain.flush_brain(
        item.get("project_id", ""), item.get("brain_cfg"),
        source_task=item.get("source_task", ""), persona=item.get("persona", ""),
        msgs=item.get("msgs") or [],
        allow_write=bool(item.get("did_work", True)),
    )
    stats["did_work"] = 1 if item.get("did_work") else 0
    return stats


# ------------------------------------------------------------------ 观测 / 测试

def pending() -> int:
    """还没处理完的轮次数（**含正在处理的那一条**）。

    不能用 ``qsize()``：worker 取走最后一条后 qsize 立刻归零，但它其实还在跑
    （实测这会让 ``wait_idle`` 提前返回、统计错位）。``unfinished_tasks`` 由
    ``put`` / ``task_done`` 维护，语义正好是"入队但未完成"。
    """
    try:
        n = int(getattr(_q, "unfinished_tasks", 0) or 0)
        return max(n, int(_q.qsize()))
    except Exception:
        return 0


def stats() -> Dict[str, Any]:
    with _thread_lock:
        alive = bool(_thread and _thread.is_alive())
    return {**_stats, "pending": pending(), "worker_alive": alive,
            "last": dict(_last_result)}


def wait_idle(timeout: float = 30.0) -> bool:
    """等队列排空（测试 / 进程优雅退出用）。超时返回 False。"""
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if pending() == 0:
            return True
        time.sleep(0.02)
    return pending() == 0


def reset() -> None:
    """清空队列与统计（仅测试用；不杀 worker 线程）。"""
    while True:
        try:
            _q.get_nowait()
            _q.task_done()
        except queue.Empty:
            break
        except Exception:
            break
    for k in list(_stats):
        _stats[k] = 0
    _last_result.clear()
