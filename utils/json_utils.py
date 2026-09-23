"""JSON 序列化工具"""
from typing import Any
import numpy as np


def json_default(obj: Any):
    """将 numpy 等类型转换为可 JSON 序列化的类型"""
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)
