"""OCR 插件：本地文字识别（RapidOCR / ONNXRuntime，CPU）。

独立可插拔——内核（omni_core / agents）对 ``utils.ocr`` 零 import，OCR 只经两个
入口被触达：
1. **工具**：``ocr_image(path)``（模型按需调用，对任意图片 / 截图识字）；
2. **capability**：``register_capability("ocr", _ocr_image_lines)``——其他插件
   （如 host 的 observe）按名字符串查询消费，本插件不启用时查询返回 None、
   消费方自动跳过文字增强。

懒加载：ORT / 模型权重只在**首次识别调用**时构造（import 本包零开销），
随本插件生命周期存在——``enabled=false``（默认）时进程内无 ORT。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from omni_core.tools.base import function_tool
from utils import get_logger

logger = get_logger("plugins.ocr")

_READER = None


def _get_reader():
    """RapidOCR 单例（首次识别调用才构造；import 失败给可执行提示）。"""
    global _READER
    if _READER is None:
        from utils.ocr import get_reader as _get

        _READER = _get()
    return _READER


def _ocr_image_lines(path: str, min_conf: float = 0.6) -> List[str]:
    """对图片文件做 OCR，返回过滤后的文字行列表。文件不存在 / 失败 → 空列表。"""
    if not path or not os.path.isfile(str(path)):
        return []
    try:
        import cv2

        img = cv2.imread(str(path))
        if img is None:
            return []
        reader = _get_reader()
        out = reader.readtext(img[:, :, ::-1])  # BGR -> RGB（reader 约定 RGB）
        return [str(line[1]) for line in out if float(line[2]) >= min_conf]
    except Exception as e:
        logger.warning(f"OCR 失败: {type(e).__name__}: {e}")
        return []


@function_tool(description="对指定图片 / 截图文件做本地 OCR 文字识别，返回文字行列表",
               unit="ocr")
def ocr_image(path: str) -> Dict[str, Any]:
    """识别图片中的文字。

    Args:
        path: 图片文件路径（png / jpg）
    """
    if not path or not os.path.isfile(str(path)):
        return {"ok": False, "error": f"图片不存在: {path}"}
    lines = _ocr_image_lines(str(path))
    return {
        "ok": True,
        "path": str(path),
        "text": lines,
        "state_text": ("[文字] " + " / ".join(lines)) if lines else "",
    }


def startup(ctx) -> None:
    """注册 capability（供其他插件按名消费，如 host observe 的文字增强）。"""
    from omni_core.tools.capability import register_capability

    register_capability("ocr", _ocr_image_lines)
    logger.info("OCR 插件已启用（ORT 将在首次识别时懒加载）")


def shutdown() -> None:
    from omni_core.tools.capability import unregister_capability

    unregister_capability("ocr")
