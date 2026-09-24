"""测试用：注入**假环境**（两个 seam 必须一起换）。

环境有两个独立的注入点，只换一个会「假一半」：

1. **内核句柄**：`core.py` 里 `from omni_core.tools import activate_environment`
   ——要 patch **使用方**那个名字（`omni_core.local.loop.core.activate_environment`）；
2. **环境工具面的后端**：`environments/<kind>/tools.py` 的模块级 `_BACKEND`
   ——工具（`observe` / `click` / …）经它调原语，须用 `bind()` 注入。

只做第 1 步，假大脑一调 `observe`/`click` 仍会打到**真实后端**（真实屏幕 + EasyOCR，
既慢又有副作用——曾把测试拖到 15s+ 甚至看似"卡死"）。

另外：内核只认环境的**三件契约**（`kind` / `text_of` / `verify_done`）。仓库里多数
既有 fake 只实现了「工具面」（`observe` / `execute_*`）并把 `text_of` / `verify_done`
放在 `self.backend` 上（旧 `ExecutionModule` 形状）。故这里用 `_ContractProxy` 把三件
契约补齐（**优先用对象自己的实现，缺则委派 `self.backend`**），既不改 9 个 fake，
也不隐藏它们的实现。新写的 fake 应直接实现三件契约。
"""
from __future__ import annotations

import importlib
from typing import Any

#: core 里那个**已绑定**的名字才是调用点（不是定义处模块）。
_CORE_SEAM = "omni_core.local.loop.core.activate_environment"


class _ContractProxy:
    """给旧式 fake 补上内核要的三件契约；其余属性一律透传。"""

    def __init__(self, target: Any):
        object.__setattr__(self, "_target", target)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """写也转发到原对象。

        测试常用 `loop.exec.ocr = [...]` 直接摆拍假后端；若不转发，
        值会落在代理上、原对象读不到 → 现象是「verify 永不过 → 循环不终止」。
        """
        if name == "_target":
            object.__setattr__(self, name, value)
        else:
            setattr(self._target, name, value)

    @property
    def kind(self) -> str:
        if hasattr(self._target, "kind"):
            return self._target.kind
        return getattr(self._target.backend, "kind", "host")

    def text_of(self, percept: Any) -> str:
        own = getattr(self._target, "text_of", None)
        if callable(own):
            return own(percept)
        inner = getattr(self._target, "backend", None)
        fn = getattr(inner, "text_of", None)
        if callable(fn):
            return fn(percept)
        return str(percept)

    def verify_done(self, condition: str, percept: Any) -> tuple:
        own = getattr(self._target, "verify_done", None)
        if callable(own):
            return own(condition, percept)
        inner = getattr(self._target, "backend", None)
        fn = getattr(inner, "verify_done", None)
        if callable(fn):
            return fn(condition, percept)
        return False, "fake 未实现 verify_done"


def install_fake_env(monkeypatch: Any, backend: Any, kind: str = "host") -> Any:
    """把假环境同时装到两个 seam，返回**原实例**（断言 `fake.calls` 用它）。

    Args:
        monkeypatch: pytest 的 monkeypatch。
        backend: 假后端——类 / 无参可调用（如 `lambda *a, **k: fb`）/ 实例均可。
        kind: 环境标识（默认 host）；决定绑定哪个环境的工具面。
    """
    if isinstance(backend, type) or (callable(backend) and not hasattr(backend, "kind")):
        obj = backend()
    else:
        obj = backend
    mod = importlib.import_module(f"environments.{kind}.tools")
    mod.bind(obj)  # seam 2：环境工具面的后端（工具用自己的原语实现）
    monkeypatch.setattr(_CORE_SEAM, lambda *a, **k: _ContractProxy(obj))  # seam 1：内核句柄
    return obj
