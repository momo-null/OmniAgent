"""OCR 读取器（EasyOCR 兼容接口，底层 RapidOCR / ONNXRuntime）。

为什么换掉 EasyOCR：EasyOCR 依赖 torch（本机安装的是 cu128 构建），
一旦被 import 就会把整套 CUDA 运行时 + 模型权重钉进后端进程，
实测 RSS 从 ~130MB 涨到 4.5GB 且永不释放（GPU 显存实际并未使用）。
RapidOCR 用 ONNXRuntime 跑 PP-OCR 模型：CPU 推理、常驻约 200MB、
中英文识别良好、pip 直装。

对外保持 EasyOCR 的调用形状 `reader.readtext(img) -> [(bbox, text, conf), ...]`，
因此 host / emulator 两侧调用点无需改动：
- img 约定为 **RGB**（与 EasyOCR 一致），内部转成 BGR 交给 RapidOCR
  （RapidOCR 对 ndarray 输入按 BGR 处理，见其 load_image.convert_img）。
- bbox 为 4 点坐标，支持 `p[0] / p[1]` 索引。

依赖缺失时不抛原始 ImportError，而是给出可执行的安装提示。
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple

_READER: Optional[Any] = None


class _RapidOCRReader:
    """RapidOCR 的 EasyOCR 兼容包装。"""

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR
        self._engine = RapidOCR()

    def readtext(self, img, **_kwargs) -> List[Tuple[Any, str, float]]:
        # RGB -> BGR（RapidOCR 对 ndarray 输入按 BGR 处理）
        try:
            bgr = img[:, :, ::-1]
        except Exception:
            bgr = img
        result, _elapse = self._engine(bgr)
        if not result:
            return []
        out: List[Tuple[Any, str, float]] = []
        for item in result:
            box, text, score = item[0], item[1], item[2]
            out.append((box, text, float(score)))
        return out


def get_reader():
    """懒加载单例：首次 OCR 调用时才构造（避免 import 期开销）。"""
    global _READER
    if _READER is None:
        try:
            _READER = _RapidOCRReader()
        except ImportError as e:  # 依赖缺失：给可执行提示，别抛原始栈
            raise RuntimeError(
                "OCR 依赖缺失：请安装 rapidocr-onnxruntime"
                "（pip install rapidocr-onnxruntime）"
            ) from e
    return _READER
