"""read_scene —— 按需读取项目场景记忆(L2 渐进披露的读取端)。

L2 消费形态对齐 TAM(auto-recall / scene-navigation):场景块**全文不进上下文**——
system 只放 ``<scene-navigation>`` 摘要导航(多入口,heat 降序);模型按导航判断当前
任务需要某场景的完整环境事实时,调本工具读取。由此保证记忆内容不随每步请求重发、
不占尾部注意力(placement 三不变量见 doc/plans/memory-architecture.md §5)。

块存储与索引在 scene_executor(TAM scene-extractor 移植,多文件主题块)。
project 定位与 load_skill 同款:绑定当前 task_id → 所属项目库。
纯读工具:不声明 risk(S1 漏斗照常生效)。
"""
from __future__ import annotations

from typing import Any, Dict

from omni_core.tools.base import function_tool

#: 当前运行绑定的 task_id(read_scene 按其解析所属项目;空串 = 未绑定,读不到项目库)。
#: 由 sdk_bridge 每次 run 前注入,与 skill_tool.set_skill_task_context 同款机制。
_current_task_id: Dict[str, str] = {"value": ""}


def set_scene_task_context(task_id: str) -> None:
    """绑定当前运行的 task_id(read_scene 据此定位所属项目场景库)。"""
    _current_task_id["value"] = task_id or ""


def current_scene_task_id() -> str:
    return _current_task_id.get("value", "") or ""


@function_tool(
    name="read_scene",
    description=("读取项目场景记忆(可复用的环境事实 / App 怪癖 / UI 语义 / 可靠操作方式)"
                 "的完整内容。当 <scene-navigation> 中某场景的摘要与当前任务相关、且摘要不足以"
                 "指导执行时,带 scene_name 调用;不带参数则返回可用场景块清单。"),
    unit="core",
)
def read_scene(scene_name: str = "") -> Dict[str, Any]:
    """读取当前任务所属项目的场景块全文(scene_blocks/<文件名>,≤4000 字符)。

    全文在需要时读取一次即可,无需重复调用;与 <scene-navigation> 摘要冲突时,
    以工具返回的全文为准确。

    Args:
        scene_name: 场景块文件名(取自 <scene-navigation> 索引,可省略 .md 后缀);
            留空时返回可用场景块清单(文件名+摘要+热度),不读全文。
    """
    try:
        from omni_core.local import scene_executor
        from omni_core.local.task_store import TaskStore

        tid = current_scene_task_id()
        pid = TaskStore.project_of(tid) if tid else ""
        if not pid:
            return {"ok": False, "error": "当前运行未绑定项目,无法定位场景库"}
        name = (scene_name or "").strip()
        if not name:
            entries = scene_executor.list_scene_blocks(pid)
            if not entries:
                return {"ok": False, "error": "当前项目无场景记忆"}
            return {"ok": True, "scenes": [
                {"filename": e["filename"], "summary": e["summary"], "heat": e["heat"]}
                for e in entries]}
        text = scene_executor.read_scene_block(pid, name)
        if not text:
            return {"ok": False, "error": f"未找到场景块: {name}"}
        return {"ok": True, "chars": len(text), "scene": text}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
