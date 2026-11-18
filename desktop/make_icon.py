# -*- coding: utf-8 -*-
"""生成 AI 盲盒桌面版图标 desktop/app.ico（+ app.png 预览）。

设计：暗紫渐变圆角底（对齐前端 --accent #7f77dd）+ 立体盲盒 + 问号 + 星芒。
所有形状按 1024 设计、2 倍超采样绘制后 LANCZOS 缩小，保证 16px 仍可辨识。

运行：
  backend\\.venv\\Scripts\\python.exe desktop\\make_icon.py

产物 app.ico 含 9 个尺寸，全部为 32bpp BMP(DIB) 条目（不使用 PNG 压缩条目），
以兼容 Windows 资源管理器、.NET System.Drawing.Icon（pywebview 窗口图标）与
PyInstaller 的 PE 图标资源写入。
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(__file__).resolve().parent
DESIGN = 1024
SS = 2                      # 超采样倍数
S = DESIGN * SS
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
             (48, 48), (64, 64), (128, 128), (256, 256)]


def sc(v: float) -> int:
    """设计坐标 -> 超采样画布坐标。"""
    return round(v * SS)


def lerp(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
         fill: tuple[int, ...], rot: float = -math.pi / 2) -> None:
    """四角星芒：4 个长角 + 4 个短角交替。"""
    pts = []
    for i in range(8):
        ang = rot + i * math.pi / 4
        rad = r if i % 2 == 0 else r * 0.32
        pts.append((sc(cx + math.cos(ang) * rad), sc(cy + math.sin(ang) * rad)))
    draw.polygon(pts, fill=fill)


def build() -> Image.Image:
    # --- 渐变底 ---
    c1 = (151, 133, 255)    # 左上：亮紫
    c2 = (70, 46, 165)      # 右下：深紫
    grad = Image.new("RGB", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        gd.line([(0, y), (S, y)], fill=lerp(c1, c2, y / (S - 1)))

    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (sc(48), sc(48), sc(976), sc(976)), radius=sc(216), fill=255
    )
    base.paste(grad, (0, 0), mask)

    # 左上柔光，让渐变更有体积感
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (sc(70), sc(40), sc(660), sc(560)), fill=(255, 255, 255, 54)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(sc(70)))
    base = Image.alpha_composite(base, glow)

    d = ImageDraw.Draw(base)
    # 细亮边
    d.rounded_rectangle(
        (sc(48), sc(48), sc(976), sc(976)), radius=sc(216),
        outline=(214, 204, 255, 130), width=sc(9),
    )

    # --- 盒底投影 ---
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (sc(228), sc(752), sc(812), sc(886)), fill=(22, 10, 62, 135)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(sc(26)))
    base = Image.alpha_composite(base, shadow)
    d = ImageDraw.Draw(base)

    # --- 立体盲盒（顶面 / 右侧面 / 正面）---
    d.polygon(
        [(sc(250), sc(470)), (sc(336), sc(384)), (sc(796), sc(384)), (sc(710), sc(470))],
        fill=(234, 229, 255, 255),
    )
    d.polygon(
        [(sc(710), sc(470)), (sc(796), sc(384)), (sc(796), sc(744)), (sc(710), sc(830))],
        fill=(176, 159, 240, 255),
    )
    d.rounded_rectangle(
        (sc(250), sc(470), sc(710), sc(830)), radius=sc(34), fill=(255, 255, 255, 255)
    )

    # 盒盖缝（正面 + 右侧面，透视连续）
    d.line([(sc(250), sc(558)), (sc(710), sc(558))], fill=(214, 205, 246, 255), width=sc(11))
    d.line([(sc(710), sc(558)), (sc(796), sc(472))], fill=(145, 126, 218, 255), width=sc(11))
    # 顶面高光边
    d.line([(sc(250), sc(470)), (sc(336), sc(384))], fill=(255, 255, 255, 200), width=sc(9))

    # --- 正面问号 ---
    font_path = Path("C:/Windows/Fonts/arialbd.ttf")
    if not font_path.is_file():
        font_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    font = ImageFont.truetype(str(font_path), sc(268))
    d.text((sc(480), sc(688)), "?", font=font, fill=(92, 63, 190, 255), anchor="mm")

    # --- 盒中飘出的星芒与光点 ---
    star(d, 762, 246, 66, (255, 214, 110, 255))
    star(d, 292, 286, 34, (255, 255, 255, 245))
    star(d, 838, 492, 26, (255, 255, 255, 225))
    star(d, 190, 528, 22, (255, 255, 255, 210))
    for cx, cy, r, a in ((586, 296, 11, 220), (652, 340, 8, 180), (370, 236, 8, 170)):
        d.ellipse((sc(cx - r), sc(cy - r), sc(cx + r), sc(cy + r)),
                  fill=(255, 255, 255, a))

    return base.resize((DESIGN, DESIGN), Image.LANCZOS)


def main() -> None:
    img = build()
    ico = OUT_DIR / "app.ico"
    png = OUT_DIR / "app.png"
    # 以 512 为源，Pillow 会为每个 size 做高质量缩放。
    # bitmap_format="bmp" 必须显式指定：Pillow 12 默认写成 PNG 压缩条目，
    # pywebview 在 Windows 用的是 .NET System.Drawing.Icon(path)，对纯 PNG 条目
    # 的 ICO 兼容性不稳；标准 32bpp DIB 条目可被资源管理器 / WinForms / PyInstaller 通吃。
    src = img.resize((512, 512), Image.LANCZOS)
    src.save(ico, format="ICO", sizes=ICO_SIZES, bitmap_format="bmp")
    src.save(png, format="PNG")

    with Image.open(ico) as probe:
        entries = sorted(probe.ico.sizes(), key=lambda t: t[0])
    print("ICO :", ico)
    print("PNG :", png)
    print("sizes:", entries)
    print("bytes:", ico.stat().st_size)


if __name__ == "__main__":
    main()
