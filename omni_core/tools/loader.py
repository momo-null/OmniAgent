"""工具插件加载器：从 plugin_dirs 发现并动态装载用户侧插件包。

设计：
- **插件 = 自包含目录**：``<plugin_dirs>/<name>/plugin.json`` + ``plugin.py``；删掉该目录，
  系统里不再有它一丝痕迹（内核从不点名任何插件）。
- **插件自持配置**：``~/.omniagent/plugins/<name>.yaml``（含 ``enabled`` 与插件自有参数）；
  ``enabled`` 缺省取 ``plugin.json`` 的 ``enabled``（缺省 ``true``）。关＝不注册其工具。
- 入口与 builtin 完全同构：模块级 ``@function_tool`` 装饰器在 import 时自动注册进
  ``TOOL_REGISTRY``（机制不变，loader 不碰派发）；装载后把工具的 ``unit`` 设为插件名、
  ``source`` 设为 ``"plugin"``。
- 可选生命周期钩子：``configure(cfg)``（收到本插件自有配置 dict）/ ``startup(ctx)`` / ``shutdown()``。
- **环境绑定（可选）**：``plugin.json`` 的 ``requires_env``（环境 kind 列表）；当前环境不匹配则不装载。
- 单包失败只回滚该包并记 ``failed``，绝不向外抛（C-P2）；本模块**不 import 任何具体插件**（C-P1）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.tools.base import TOOL_REGISTRY
from utils import get_logger

__all__ = [
    "RESERVED_TOOL_NAMES",
    "PluginContext",
    "LoadReport",
    "load_plugins",
    "shutdown_plugins",
    "last_report",
    "plugin_module",
    "plugin_config",
    "list_plugins",
]

logger = get_logger("tools.loader")

#: 注册表保留名：插件不得占用（内核能力 / 循环元工具语义）
RESERVED_TOOL_NAMES = frozenset(
    {
        "shell_exec",
        "task_done",
        "verify",
        "escalate",
        "record",
        "plan",
    }
)

#: 动态装载模块名前缀（便于测试清理 sys.modules 缓存）
_MODULE_PREFIX = "_omni_plugin_"

#: 进程内装载状态（幂等依据 / 取模块句柄）
_loaded_packages: Dict[str, str] = {}
_loaded_modules: Dict[str, Any] = {}
_last_report: Optional["LoadReport"] = None

#: 默认插件根目录（相对仓库根，不受 cwd 影响）
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PLUGIN_DIRS = ["plugins"]


@dataclass
class PluginContext:
    """内核装配后交给插件的运行时句柄（只读约定）。

    ``wired`` 区分两类装载：``True`` = 来自真实装配（ToolLoop，句柄已就绪）；
    ``False`` = 只读枚举等旁路（GET /api/runtime/tools、测试）。
    插件**不得**在 ``wired=False`` 时做破坏性动作（如注销工具）。

    ``env_kind``：当前环境标识（``"host"`` / ``"emulator"`` …）只读；插件可据此做
    环境分支，但**拿不到环境厚句柄**（键鼠 / 截图等原语一律走工具）。
    """

    config: Dict[str, Any] = field(default_factory=dict)
    wired: bool = False
    env_kind: str = ""


@dataclass
class LoadReport:
    """一次装载的结果（loaded / skipped / failed 三表）。"""

    loaded: List[str] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    failed: List[Dict[str, str]] = field(default_factory=list)

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append({"name": name, "reason": reason})

    def fail(self, name: str, error: str) -> None:
        self.failed.append({"name": name, "error": error})
        logger.warning("插件 %s 装载失败: %s", name, error)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "loaded": list(self.loaded),
            "skipped": list(self.skipped),
            "failed": list(self.failed),
        }


def load_plugins(cfg: Optional[Dict[str, Any]] = None, ctx: Optional[PluginContext] = None) -> LoadReport:
    """扫描 ``runtime.tools.plugin_dirs`` 并装载其中的插件包（幂等）。

    Args:
        cfg: 全局配置快照（缺省空 dict → 用 ``["plugins"]``）。
        ctx: 交给 ``startup(ctx)`` 的运行时句柄；缺省用 ``config=cfg`` 的空上下文。

    Returns:
        LoadReport：本次新增装载 / 跳过 / 失败三表。已装载的包再次调用记 skip。
    """
    global _last_report

    cfg = cfg if isinstance(cfg, dict) else {}
    if ctx is None:
        ctx = PluginContext(config=cfg)

    report = LoadReport()
    for root in _plugin_dirs(cfg):
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir(), key=lambda p: p.name):
            if not directory.is_dir():
                continue
            if directory.name.startswith("."):
                continue  # 点开头目录（.git / .omniagent 等）静默忽略
            name = directory.name
            if not name.isidentifier():
                report.skip(name, "目录名不是合法 Python 标识符")
                continue
            if not (directory / "plugin.py").is_file():
                continue  # 无入口文件的普通目录静默跳过
            plugin_cfg = plugin_config(name)
            if name in _loaded_packages:
                # 已装载：不再 import / 不再注册（幂等），但**仍要把新的运行时句柄
                # 交给插件**——bind 类钩子（视觉运行时等）必须跟随每个 ToolLoop 实例刷新。
                try:
                    _call_hook(_loaded_modules.get(name), "startup", ctx)
                except Exception as e:
                    report.fail(name, f"startup 钩子失败: {type(e).__name__}: {e}")
                    continue
                report.skip(name, "已装载（幂等跳过 import/注册）")
                continue
            manifest = _read_manifest(directory, name)
            if isinstance(manifest, str):
                report.fail(name, manifest)
                continue
            # 环境绑定：requires_env 非空且当前环境已知且不匹配 → 不装载
            requires = manifest.get("requires_env") or []
            if requires and ctx.env_kind and ctx.env_kind not in requires:
                report.skip(name, f"requires_env 不含当前环境 {ctx.env_kind}")
                continue
            # 开关：plugin.yaml 优先，缺省取 manifest.enabled（缺省 true）
            if not _enabled(name, plugin_cfg, manifest):
                report.skip(name, "插件已停用（enabled=false）")
                continue
            _load_one(directory, name, manifest, plugin_cfg, ctx, report)

    _last_report = report
    return report


def plugin_config(name: str) -> Dict[str, Any]:
    """读取插件自有配置 ``~/.omniagent/plugins/<name>.yaml``（缺失返回空 dict）。"""
    try:
        from omni_core.local.runtime_paths import plugin_config_file
        path = plugin_config_file(name)
    except Exception:
        return {}
    if not path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("插件 %s 配置读取失败（忽略）: %s", name, e)
        return {}
    return data if isinstance(data, dict) else {}


def list_plugins(cfg: Optional[Dict[str, Any]] = None, env_kind: str = "") -> List[Dict[str, Any]]:
    """枚举发现到的插件（含停用 / 环境不匹配的），供前端渲染「一个开关」。

    返回 ``[{name, title, description, enabled, requires_env, available}]``。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    out: List[Dict[str, Any]] = []
    for root in _plugin_dirs(cfg):
        if not root.is_dir():
            continue
        for directory in sorted(root.iterdir(), key=lambda p: p.name):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            name = directory.name
            if not name.isidentifier() or not (directory / "plugin.py").is_file():
                continue
            manifest = _read_manifest(directory, name)
            if isinstance(manifest, str):
                continue
            pcfg = plugin_config(name)
            requires = list(manifest.get("requires_env") or [])
            available = (not requires) or (not env_kind) or (env_kind in requires)
            out.append({
                "name": name,
                "title": manifest.get("title") or name,
                "description": manifest.get("description", ""),
                "enabled": _enabled(name, pcfg, manifest),
                "requires_env": requires,
                "available": available,
            })
    return out


def shutdown_plugins() -> None:
    """进程退出前依次调用各插件 ``shutdown()``（按包名序，异常吞掉）。"""
    for name in sorted(_loaded_packages):
        module = _loaded_modules.get(name)
        hook = getattr(module, "shutdown", None) if module is not None else None
        if callable(hook):
            try:
                hook()
            except Exception as e:  # 退出路径不因插件异常而中断
                logger.warning("插件 %s shutdown 失败（已忽略）: %s", name, e)


def last_report() -> Optional[LoadReport]:
    """最近一次装载报告（未装载过时为 None）。"""
    return _last_report


def plugin_module(name: str) -> Any:
    """取已装载插件模块句柄（测试注入 / 直接调用用）；未装载返回 None。"""
    return _loaded_modules.get(name)


# ── 内部实现 ──────────────────────────────────────────────────────────────

def _plugin_dirs(cfg: Dict[str, Any]) -> List[Path]:
    """解析 ``runtime.tools.plugin_dirs``（缺省 ``["plugins"]``，相对仓库根）。"""
    runtime = (cfg.get("runtime") or {})
    tools = (runtime.get("tools") or {})
    dirs = tools.get("plugin_dirs") or _DEFAULT_PLUGIN_DIRS
    if isinstance(dirs, str):
        dirs = [dirs]
    out: List[Path] = []
    for item in dirs:
        p = Path(str(item)).expanduser()
        out.append(p if p.is_absolute() else _PROJECT_ROOT / p)
    return out


def _enabled(name: str, plugin_cfg: Dict[str, Any], manifest: Dict[str, Any]) -> bool:
    """有效启用状态：plugin.yaml 的 enabled 优先，缺省取 manifest.enabled，缺省 True。"""
    if "enabled" in plugin_cfg:
        return bool(plugin_cfg.get("enabled"))
    if "enabled" in manifest:
        return bool(manifest.get("enabled"))
    return True


def _read_manifest(directory: Path, name: str) -> Any:
    """读 plugin.json；返回 manifest dict，或错误信息字符串（fail 用）。"""
    path = directory / "plugin.json"
    if not path.is_file():
        return "缺少 plugin.json（manifest 必需）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"plugin.json 非法: {type(e).__name__}: {e}"
    if not isinstance(data, dict):
        return "plugin.json 顶层必须是对象"
    if data.get("name") != name:
        return f"manifest.name 与目录名不一致: {data.get('name')!r} != {name!r}"
    return data


def _load_one(
    directory: Path,
    name: str,
    manifest: Dict[str, Any],
    plugin_cfg: Dict[str, Any],
    ctx: PluginContext,
    report: LoadReport,
) -> None:
    """装载单个插件包：import → 命名校验 → configure/startup；失败整包回滚。"""
    module_name = f"{_MODULE_PREFIX}{name}"
    snapshot = dict(TOOL_REGISTRY)  # 回滚基线（含对象身份，用于识别"覆盖已注册"）
    try:
        module = _import_module(directory / "plugin.py", module_name)
    except Exception as e:
        _rollback(snapshot, module_name)
        report.fail(name, f"import plugin.py 失败: {type(e).__name__}: {e}")
        return

    added = [n for n in TOOL_REGISTRY if n not in snapshot]
    overridden = [n for n, p in TOOL_REGISTRY.items() if n in snapshot and snapshot[n] is not p]
    touched = set(added) | set(overridden)

    reserved_hit = sorted(n for n in touched if n in RESERVED_TOOL_NAMES)
    if reserved_hit:
        _rollback(snapshot, module_name)
        report.fail(name, f"占用保留名: {', '.join(reserved_hit)}")
        return
    if overridden:
        _rollback(snapshot, module_name)
        report.fail(name, f"覆盖已注册工具: {', '.join(sorted(overridden))}")
        return

    # 工具归属：unit = 插件名，source = "plugin"（覆盖 @function_tool 的缺省）
    for tool_name in added:
        plugin = TOOL_REGISTRY.get(tool_name)
        if plugin is not None:
            plugin.unit = name
            plugin.source = "plugin"

    try:
        _call_hook(module, "configure", plugin_cfg)
        _call_hook(module, "startup", ctx)
    except Exception as e:
        _rollback(snapshot, module_name)
        report.fail(name, f"生命周期钩子失败: {type(e).__name__}: {e}")
        return

    _loaded_packages[name] = module_name
    _loaded_modules[name] = module
    report.loaded.append(name)


def _import_module(entry: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {entry} 构造模块 spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _call_hook(module: Any, hook_name: str, arg: Any) -> None:
    hook = getattr(module, hook_name, None)
    if callable(hook):
        hook(arg)


def _rollback(snapshot: Dict[str, Any], module_name: str) -> None:
    """把注册表恢复成装载前的快照（含对象身份），让失败包零残留。"""
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(snapshot)
    sys.modules.pop(module_name, None)
