# -*- coding: utf-8 -*-
"""生成桌面应用图标：desktop/icons/icon.ico（exe/安装包）+ desktop/resources/icon.png（托盘）。

用仓库 .venv 里现成的 Pillow（requirements.txt 已含），一次性脚本：
    .venv/Scripts/python.exe desktop/scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

DESKTOP = Path(__file__).resolve().parents[1]
ICON_DIR = DESKTOP / "icons"
RES_DIR = DESKTOP / "resources"

SIZES = [256, 128, 64, 48, 32, 16]
BG = (16, 20, 24, 255)        # 与 loading.html 底色一致
EDGE = (46, 160, 255, 255)    # 深青描边
RING = (46, 208, 255, 255)    # 亮青圆环（呼应 v3 兜底图标的青色圆圈）


def draw(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [0, 0, size - 1, size - 1],
        radius=max(2, size // 5),
        fill=BG,
        outline=EDGE,
        width=max(1, size // 24),
    )
    # 中央 "O" 圆环
    m = size * 0.30
    d.ellipse(
        [m, m, size - m, size - m],
        outline=RING,
        width=max(2, size // 9),
    )
    return img


def main() -> None:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    RES_DIR.mkdir(parents=True, exist_ok=True)
    base = draw(256)
    base.save(ICON_DIR / "icon.ico", sizes=[(s, s) for s in SIZES])
    base.save(RES_DIR / "icon.png")
    print(f"生成: {ICON_DIR / 'icon.ico'}")
    print(f"生成: {RES_DIR / 'icon.png'}")


if __name__ == "__main__":
    main()
