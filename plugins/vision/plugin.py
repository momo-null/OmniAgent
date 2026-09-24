"""视觉 / SoM 官方插件（P3 自 omni_core/tools/vision_tool.py 迁出，函数体零改动）。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.4）：
- `vision_describe` / `som_ground` / `som_marks` / `tap_by_mark` / `som_last_result`
  是平级 tool 插件，与设备、Python 执行、外部 MCP 同级，由 LLM 直接调用。
- 底层实现（VLM 客户端、SoM 解析、画框）在本包同级的 `runtime.py`；
  本文件只做「工具声明 + 跨步状态自持」。

**α 决策（已落地）**：marks 的跨步状态由 **tool 内自持**——本模块持有
`_last_marks`（对外语义 `som://last_result`），内核零感知：
不进 WorldModel、不进 tool_loop、也不放在 VisionRuntime 里。
agent 需要重读时用 `som_last_result` 工具取回。

运行时自构造（M-后续：彻底去内核化）：`VisionRuntime` 由本插件 `startup(ctx)`
在每个 ToolLoop 装配时**自己**构造——配置读**插件自有**文件 `~/.omniagent/plugins/vision.yaml`
（经 `loader.plugin_config("vision")`；插件**不拿环境厚句柄**），不再由内核 `tool_loop` 构造并注入。
内核完全不 import 视觉运行时。
- 插件自有配置 `enabled` 为真且装配路径（wired）：构造并绑定运行时，工具保持注册；
- 装配路径且未启用：注销依赖 VLM 的三个工具，让大脑「知道」不可用；
- 只读枚举路径（wired=False，如 GET /api/runtime/tools）：不做破坏性动作，
  保留注册表（否则幂等装载不会补回来）。
"""
import importlib.util
import sys
from typing import Any, Dict, List, Optional

from omni_core.tools.base import TOOL_REGISTRY, function_tool, unregister_tool
from plugins.vision.runtime import VisionRuntime
from utils import get_logger

logger = get_logger("plugins.vision")

# 依赖 VLM 的视觉工具（vision 关闭时从注册表移除，让大脑「知道」不可用）
_VLM_TOOLS = ("vision_describe", "som_ground", "som_marks")

# 注入的视觉运行时（由本插件 startup 在每个 ToolLoop 装配期自构造并绑定）
_VR: Optional[VisionRuntime] = None

#: SoM marks 跨步状态（`som://last_result`）。仅本插件持有，内核零感知。
_last_marks: List[Dict[str, Any]] = []


def bind_vision_runtime(vr: VisionRuntime) -> None:
    """注入视觉运行时（每个 ToolLoop 装配期调用一次）。"""
    global _VR
    _VR = vr


def startup(ctx) -> None:
    """内核装配后自构造运行时 / 决定绑定或注销视觉工具。

    分支（``wired`` 是 P3 的关键修正——只读枚举路径不得做破坏性动作）：
    - ``wired=True`` 且插件自有配置 ``enabled`` 为真 → 用插件自有 yaml
      （``loader.plugin_config("vision")``）自构造 ``VisionRuntime`` 并绑定（每个 ToolLoop 实例持有自己的运行时）；
    - ``wired=True`` 且未启用 → 注销三个依赖 VLM 的工具并记日志，让大脑「知道」不可用；
    - ``wired=False``（只读枚举 / 测试旁路）→ 什么都不做（保留注册表，避免幂等装载不再补回）。

    Args:
        ctx: PluginContext（只读 ``wired``；视觉配置走插件自有 yaml，不经 ctx）。
    """
    # 先确保自己的工具在册：vision 曾被关闭的轮次把它们注销过，而全局注册表是
    # 进程级状态——「谁注册谁维护」，否则一次关闭会永久改变注册表（vision 再也回不来）。
    _ensure_registered()

    if not getattr(ctx, "wired", False):
        logger.debug("vision 插件：只读枚举路径，跳过运行时构造与注销")
        return

    # 插件自有配置：~/.omniagent/plugins/vision.yaml（不进 core config）。
    from omni_core.tools.loader import plugin_config
    vision_cfg = plugin_config("vision")

    if not vision_cfg.get("enabled"):
        for tool_name in _VLM_TOOLS:
            unregister_tool(tool_name)
        logger.info("视觉通道未启用：已注销 %s，大脑将不再调用", " / ".join(_VLM_TOOLS))
        return

    # ⏳ 待办：环境解耦——vision 现需环境截图能力，按新设计应改为工具收 `image_path`
    # （不再持有 env 句柄）。解耦前不构造运行时，工具调用会明确报「未启用」。
    logger.warning("vision 插件：环境解耦未完成（截图依赖待改为 image_path），暂不构造运行时")


def _ensure_registered() -> None:
    """确保本插件声明的视觉工具在册（被注销过则重新注册）。

    实现：在**既有模块命名空间**内重跑一次模块体 → 模块级 ``@function_tool``
    装饰器重新执行 → 重新登记。刻意不用 ``importlib.reload``：插件是按文件路径
    动态装载的，reload 会按名字重新 find_spec 而失败（ModuleNotFoundError）。
    """
    if all(name in TOOL_REGISTRY for name in _VLM_TOOLS):
        return
    module = sys.modules.get(__name__)
    if module is None or not getattr(module, "__file__", None):
        return
    spec = importlib.util.spec_from_file_location(__name__, module.__file__)
    if spec is None or spec.loader is None:
        return
    spec.loader.exec_module(module)


def _require_vr() -> VisionRuntime:
    if _VR is None:
        raise RuntimeError("vision 通道未启用（未构造 VisionRuntime）")
    return _VR


def _remember(marks: Any) -> None:
    """把本次标注产出的 marks 记入 tool 内状态。"""
    global _last_marks
    _last_marks = list(marks or [])


@function_tool(description="把当前屏幕截图送给本地视觉模型做理解，返回文本回答", unit="vision")
def vision_describe(prompt: str) -> Dict[str, Any]:
    """对当前截图做视觉理解，返回 VLM 文本。

    Args:
        prompt: 对截图提出的问题，例如“当前在哪一步？”
    """
    return _require_vr().vision_describe(prompt)


@function_tool(description="对当前界面做 Set-of-Marks 标注：编号可交互元素，返回 marks 列表（mark id、resource-id、文本、坐标）", unit="vision")
def som_ground() -> Dict[str, Any]:
    """对当前界面做 SoM 标注（基于 UI 树）。"""
    res = _require_vr().som_ground()
    if isinstance(res, dict) and res.get("ok"):
        _remember(res.get("marks"))
    return res


@function_tool(description="视觉 Set-of-Marks：送截图给视觉模型识别可交互区域并编号，返回 marks 列表（id、归一化坐标、标签），无结构化层级界面用此", unit="vision")
def som_marks(ask: str = "") -> Dict[str, Any]:
    """视觉 SoM 标注（基于像素，无 UI 树界面）。

    Args:
        ask: 可选，提示视觉模型重点关注什么，例如“列出所有可点击的按钮”
    """
    res = _require_vr().visual_so_m(ask)
    if isinstance(res, dict) and res.get("ok"):
        _remember(res.get("marks"))
    return res


@function_tool(description="读取上一次 SoM 标注留存的 marks（som://last_result）；跨步骤仍可用，无需重新标注", unit="vision")
def som_last_result() -> Dict[str, Any]:
    """取回本插件自持的 SoM marks 状态。"""
    return {"ok": True, "marks": list(_last_marks), "count": len(_last_marks)}


@function_tool(description="按 SoM mark id 点击对应控件：结构化 mark 映射 resource-id，视觉 mark 映射归一化坐标", unit="vision")
def tap_by_mark(mark_id: int) -> Dict[str, Any]:
    """按 SoM mark id 点击。

    Args:
        mark_id: som_ground / som_marks 返回的 marks 中的 id
    """
    mark = next((m for m in _last_marks if m.get("id") == int(mark_id)), None)
    if mark is None:
        return {
            "ok": False,
            "error": f"未知 mark id: {mark_id}（先调 som_ground / som_marks，或 som_last_result 查看）",
        }
    return _require_vr().tap_mark(mark)
