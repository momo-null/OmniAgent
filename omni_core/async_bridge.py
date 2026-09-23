"""同步内核 <-> 异步 SDK 的线程桥（框架集成基础设施）。

OpenAI Agents SDK 是 async-first（`Model.get_response` / `Runner.run` /
`FunctionTool.on_invoke_tool` 全是协程），而 OmniAgent 内核（tool_loop、
后端 FastAPI 同步调用点）是同步的。本模块提供**一个进程级常驻后台 loop**，
把同步调用方桥到 SDK 协程上——每次调用新建 loop 会重建底层连接池，代价过高。

这不是"自研协议/自研循环"，只是异步运行时的适配层。
"""
import asyncio
import threading
import time
from typing import Any, Optional


class AsyncBridge:
    """进程级常驻 asyncio loop（后台守护线程）。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        with self._lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()

            def _serve() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                loop.run_forever()

            self._thread = threading.Thread(target=_serve, name="omni-async-bridge", daemon=True)
            self._thread.start()
            ready.wait(10.0)
        if self._loop is None:  # 理论不可达，防御
            raise RuntimeError("async bridge 未能启动")
        return self._loop

    def run(self, coro, timeout: Optional[float] = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop()).result(timeout)

    def create_task(self, coro) -> Any:
        """把协程挂到常驻 loop 上并立即返回 Task 句柄。

        M8 硬停止需要：只有拿到 Task 才能真正 cancel 一个正在跑的图
        （concurrent.futures.Future.cancel() 对已开始的任务无效）。
        """
        loop = self.loop()
        holder: Any = {}

        def _spawn() -> None:
            holder["task"] = loop.create_task(coro)

        loop.call_soon_threadsafe(_spawn)
        for _ in range(200):            # 等 task 建好（通常立即可用）
            if "task" in holder:
                return holder["task"]
            time.sleep(0.005)
        raise RuntimeError("async bridge 未能创建 task")

    def wait(self, task: Any, timeout: Optional[float] = None) -> Any:
        """等待一个已在 loop 上的 Task 完成并取回结果。

        `run_coroutine_threadsafe` 只收协程、不收 Task，所以这里用一层
        ``await task`` 的协程包一下；被取消时 CancelledError 会如实抛回调用方。
        """
        async def _await_task() -> Any:
            return await task

        return self.run(_await_task(), timeout)

    def cancel(self, task: Any) -> None:
        """从任意线程取消常驻 loop 上的 Task（硬停止）。"""
        if task is None:
            return
        try:
            self.loop().call_soon_threadsafe(task.cancel)
        except Exception:
            pass


_BRIDGE: Optional[AsyncBridge] = None


def bridge() -> AsyncBridge:
    """进程级单例桥。"""
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = AsyncBridge()
    return _BRIDGE


def run_async(coro, timeout: Optional[float] = None) -> Any:
    """在常驻 loop 上执行协程并同步取回结果。"""
    return bridge().run(coro, timeout)


def create_task(coro) -> Any:
    """提交协程到常驻 loop，返回 asyncio.Task 句柄（可传给 :func:`cancel_task`）。"""
    return bridge().create_task(coro)


def wait_task(task: Any, timeout: Optional[float] = None) -> Any:
    """等待 :func:`create_task` 产出的 Task 完成并取回结果。"""
    return bridge().wait(task, timeout)


def cancel_task(task: Any) -> None:
    """硬停止：取消常驻 loop 上正在跑的 Task。"""
    bridge().cancel(task)
