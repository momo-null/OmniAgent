"""测试用：注入**假能力插件后端**（两个 seam）。

env 概念已退场：操作电脑 = 普通插件（``plugins/host`` …），内核没有
``activate_environment`` 缝可 patch。给跑「设备类」流程的用例注入假后端只剩：

1. **工具面后端**：`plugins/<kind>/tools.py` 的模块级 `_BACKEND`
   ——工具（`observe` / `click` / …）经它调原语，须用 `bind()` 注入。
   只 import 工具模块（注册工具）而不绑后端 → 工具一调立即报「未绑定后端」，
   而不是静默打到真实屏幕（真实键鼠既慢又有副作用）。
2. **verify 判定**：内核 `_verify` 走 LLM 判定器（``llm_judge``）。测试把它
   委托给假后端自己的 `verify_done(condition, percept)`（对象自己优先，缺则
   委派 `self.backend`——沿用旧式 fake 的形状），保留既有 fakes 的命中语义；
   连 verify_done 都没有的 fake 恒判达成。

注意：内核不再有 ``loop.exec`` 句柄——要摆拍观测数据（如 ``fb.ocr = [...]``），
直接持有 :func:`install_fake_env` 返回的**原实例**。
"""
from __future__ import annotations

import importlib
from typing import Any


def install_fake_env(monkeypatch: Any, backend: Any, kind: str = "host") -> Any:
    """把假后端装到工具面 + verify 委托缝，返回**原实例**（断言 `fake.calls` 用它）。

    Args:
        monkeypatch: pytest 的 monkeypatch。
        backend: 假后端——类 / 无参可调用（如 `lambda *a, **k: fb`）/ 实例均可。
        kind: 插件目录名（默认 "host"；决定绑定哪个插件包的工具面）。
    """
    if isinstance(backend, type) or (callable(backend) and not hasattr(backend, "kind")):
        obj = backend()
    else:
        obj = backend
    mod = importlib.import_module(f"plugins.{kind}.tools")
    mod.bind(obj)  # seam 1：工具面的后端（工具用自己的原语实现）

    def _fake_verify(self, spec, world, condition=""):  # noqa: ANN001  # seam 2：verify 判定
        cond = condition or getattr(spec, "expected", "") or getattr(spec, "done_when", "")
        vd = getattr(obj, "verify_done", None)
        if not callable(vd):
            vd = getattr(getattr(obj, "backend", None), "verify_done", None)
        if callable(vd):
            return vd(cond, getattr(world, "current_percept", {}))
        return True, "fake"

    monkeypatch.setattr("omni_core.local.loop.core.ToolLoop._verify", _fake_verify)
    return obj
