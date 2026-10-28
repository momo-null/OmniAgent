"""本地图片 → 压缩 data URL（供截图类工具把原图投递进多模态模型消息）。

为什么压缩：1920×1080 PNG base64 有数 MB，直塞上下文会顶爆 token。
这里统一压到最长边 ``max_side``（默认 1568，主流视觉模型的实用上限）并转
JPEG（``quality`` 默认 80）——文字类截图在此质量下仍可读。

纯 I/O 工具函数：不认识任何业务 / 工具名；内核不 import 本模块（只有插件用）。
"""
from __future__ import annotations

import base64
import os

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def image_to_data_url(image_path: str, max_side: int = 1568, quality: int = 80) -> str:
    """把本地图片读为压缩后的 base64 data URL。

    PIL 可用：最长边等比缩到 ``max_side`` 内、转 JPEG（``quality``）。
    PIL 缺失：退化为原样读文件（不缩不转码，保持可用的最低保障）。
    文件不存在 / 打不开：抛异常（调用方自行兜底）。
    """
    ext = os.path.splitext(image_path)[1].lower()
    mime = _MIME.get(ext, "image/png")
    try:
        from PIL import Image
        import io

        with Image.open(image_path) as img:
            w, h = img.size
            longest = max(w, h, 1)
            if longest > max_side:
                scale = max_side / float(longest)
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=int(quality))
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{b64}"
    except ImportError:
        pass  # PIL 缺失：原样读（不缩不转码）
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"
