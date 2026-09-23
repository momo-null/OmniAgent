"""视觉 / SoM 自研外层 tool 插件（平级，由 LLM 直接调用）。

设计（见 doc/plans/refactor-agent-core-framework-design-2026-09-12.md §5.4）：
- `vision_describe` / `som_ground` / `som_marks` / `tap_by_mark` 是平级 tool 插件，
  与设备、Python 执行、外部 MCP 同级，由 LLM 直接调用。
- 底层实现（VLM 客户端、SoM 解析、画框）在同层 L1 的 `vision_runtime.py`；
  本文件只做「工具声明 + 跨步状态自持」。

**α 决策（已落地）**：marks 的跨步状态由 **tool 内自持**——本模块持有
`_last_marks`（对外语义 `som://last_result`），内核零感知：
不进 WorldModel、不进 tool_loop、也不放在 VisionRuntime 里。
agent 需要重读时用 `som_last_result` 工具取回。

运行时注入：`bind_vision_runtime(vr)` 在 ToolLoop 构造期调用一次。
"""
from typing import Any, Dict, List, Optional

from omni_core.tools.base import function_tool
from omni_core.tools.vision_runtime import VisionRuntime


# 注入的视觉运行时（由 tool_loop 在 runtime.vision.enabled 时注入）
_VR: Optional[VisionRuntime] = None

#: SoM marks 跨步状态（`som://last_result`）。仅本插件持有，内核零感知。
_last_marks: List[Dict[str, Any]] = []


def bind_vision_runtime(vr: VisionRuntime) -> None:
    """注入视觉运行时（一次性，tool_loop 构造期调用）。"""
    global _VR
    _VR = vr


def _require_vr() -> VisionRuntime:
    if _VR is None:
        raise RuntimeError("vision 通道未启用（未注入 VisionRuntime）")
    return _VR


def _remember(marks: Any) -> None:
    """把本次标注产出的 marks 记入 tool 内状态。"""
    global _last_marks
    _last_marks = list(marks or [])


@function_tool(description="把当前屏幕截图送给本地视觉模型做理解，返回文本回答", group="vision")
def vision_describe(prompt: str) -> Dict[str, Any]:
    """对当前截图做视觉理解，返回 VLM 文本。

    Args:
        prompt: 对截图提出的问题，例如“当前在哪一步？”
    """
    return _require_vr().vision_describe(prompt)


@function_tool(description="对当前界面做 Set-of-Marks 标注：编号可交互元素，返回 marks 列表（mark id、resource-id、文本、坐标）", group="vision")
def som_ground() -> Dict[str, Any]:
    """对当前界面做 SoM 标注（基于 UI 树）。"""
    res = _require_vr().som_ground()
    if isinstance(res, dict) and res.get("ok"):
        _remember(res.get("marks"))
    return res


@function_tool(description="视觉 Set-of-Marks：送截图给视觉模型识别可交互区域并编号，返回 marks 列表（id、归一化坐标、标签），无结构化层级界面用此", group="vision")
def som_marks(ask: str = "") -> Dict[str, Any]:
    """视觉 SoM 标注（基于像素，无 UI 树界面）。

    Args:
        ask: 可选，提示视觉模型重点关注什么，例如“列出所有可点击的按钮”
    """
    res = _require_vr().visual_so_m(ask)
    if isinstance(res, dict) and res.get("ok"):
        _remember(res.get("marks"))
    return res


@function_tool(description="读取上一次 SoM 标注留存的 marks（som://last_result）；跨步骤仍可用，无需重新标注", group="vision")
def som_last_result() -> Dict[str, Any]:
    """取回本插件自持的 SoM marks 状态。"""
    return {"ok": True, "marks": list(_last_marks), "count": len(_last_marks)}


@function_tool(description="按 SoM mark id 点击对应控件：结构化 mark 映射 resource-id，视觉 mark 映射归一化坐标", group="vision")
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
