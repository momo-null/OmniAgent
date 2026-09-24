"""环境注册表：按 ``kind`` 注册 / 构造环境（内核**不点名任何具体环境**）。

设计（见 doc/plans/capability-unit-refactor-2026-09-24.md §5.5）：
- 各环境在 ``environments/<kind>/`` 里**自注册**：``register_environment(kind, create, bind_tools)``；
- 本模块**不 import 任何具体环境**；首次使用懒发现（``import environments`` 触发自注册）；
- 删除某个环境包 = 该 kind 消失，本模块行为不受影响。

新增环境 = 加一个 ``environments/<kind>/`` 目录并在 ``environments/__init__.py`` import 一行。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

_factories: Dict[str, Callable[[Dict[str, Any]], Any]] = {}
_binders: Dict[str, Callable[[Any], None]] = {}
_titles: Dict[str, str] = {}
_discovered = False


def register_environment(
    kind: str,
    create: Callable[[Dict[str, Any]], Any],
    bind_tools: Optional[Callable[[Any], None]] = None,
    title: str = "",
) -> None:
    """注册一个环境。

    Args:
        kind: 环境标识（如 "host" / "emulator"）。
        create: ``create(config) -> backend`` 构造该环境实例。
        bind_tools: 可选，``bind_tools(backend)`` 把环境实例交给其工具模块。
        title: 显示名（供前端 radio 渲染；环境自报，前端不硬编码）。
    """
    _factories[str(kind)] = create
    _titles[str(kind)] = title or str(kind)
    if bind_tools is not None:
        _binders[str(kind)] = bind_tools


def list_environments() -> List[Dict[str, str]]:
    """已注册环境清单 ``[{kind, title}]``（供前端 radio；不硬编码任何环境名）。"""
    discover()
    return [{"kind": k, "title": _titles.get(k, k)} for k in sorted(_factories)]


def discover() -> None:
    """触发各环境自注册（import ``environments`` 包）。幂等。"""
    global _discovered
    if _discovered:
        return
    _discovered = True
    try:
        import environments  # noqa: F401  （触发各环境包的自注册）
    except Exception:
        pass


def registered_kinds() -> List[str]:
    """已注册的环境 kind 列表。"""
    discover()
    return sorted(_factories)


def create_backend(kind: Optional[str], config: Dict[str, Any]) -> Any:
    """按 kind 构造环境实例（config = 该环境自有配置）。未注册则抛 KeyError。"""
    discover()
    factory = _factories.get(str(kind or ""))
    if factory is None:
        raise KeyError(f"未注册的环境: {kind!r}（可用: {registered_kinds()}）")
    return factory(config or {})


def bind_tools(kind: str, backend: Any) -> None:
    """把环境实例交给其工具模块（若该环境注册了 binder）。"""
    discover()
    binder = _binders.get(str(kind or ""))
    if callable(binder):
        binder(backend)
