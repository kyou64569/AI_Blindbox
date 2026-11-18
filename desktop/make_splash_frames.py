# -*- coding: utf-8 -*-
"""生成桌面版「屏幕中央原生浮层」启动动画帧（透明 RGBA PNG）。

为什么是预渲染帧而不是 CSS/JS：
  用户要求双击 exe 后**立刻**在屏幕中央看到动画，而 WebView2/.NET 初始化本身
  就要 1~3 秒。原生 Win32 分层窗口（native_splash.py）不依赖 Chromium，
  只要解出第一张 PNG 就能上屏；后续帧按「实际加载进度」逐张切换。

产物（desktop/splash_frames/）：
  open_000.png ... open_035.png   开盒进度帧，索引 = 归一化加载进度
  flash_000.png ... flash_017.png 加载完成后的「掀盖 → 光束 → 星芒 → 淡出」终场
  manifest.json                   帧尺寸与数量（native_splash.py 读取）

设计：深色半透明圆角卡片浮在桌面中央，卡片外完全透明（per-pixel alpha）；
盒盖随进度抬起、盒缝透光渐强、光锥与星点随进度生长，底部进度条与百分比
把「开盒速度 = 加载速度」直接可视化。

运行：
  backend\\.venv\\Scripts\\python.exe desktop\\make_splash_frames.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent / "splash_frames"
SIZE = 480
N_OPEN = 36
N_FLASH = 18

PURPLE = (127, 119, 221)
PURPLE_LIGHT = (176, 159, 240)
PURPLE_PALE = (234, 229, 255)
PURPLE_DEEP = (92, 63, 190)
GOLD = (255, 214, 110)
INK = (16, 18, 26)


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def ease(t: float) -> float:
    t = clamp(t)
    return t * t * (3 - 2 * t)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    names = (
        ["msyhbd.ttc", "msyh.ttc", "simhei.ttf"] if bold
        else ["msyh.ttc", "simhei.ttf", "segoeui.ttf"]
    )
    for name in names:
        p = Path("C:/Windows/Fonts") / name
        if p.is_file():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


def star(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
         fill: tuple[int, ...], rot: float = -math.pi / 2) -> None:
    pts = []
    for i in range(8):
        ang = rot + i * math.pi / 4
        rad = r if i % 2 == 0 else r * 0.30
        pts.append((cx + math.cos(ang) * rad, cy + math.sin(ang) * rad))
    d.polygon(pts, fill=fill)


def layer() -> Image.Image:
    return Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))


def soft_glow(box: tuple[int, int, int, int], color: tuple[int, ...],
              blur: int) -> Image.Image:
    im = layer()
    ImageDraw.Draw(im).ellipse(box, fill=color)
    return im.filter(ImageFilter.GaussianBlur(blur))


def card(base: Image.Image) -> None:
    """半透明深色圆角卡片：保证文字在任意壁纸上都可读，卡片外保持透明。"""
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((48, 32, 432, 460), radius=42, fill=(*INK, 214))
    d.rounded_rectangle((48, 32, 432, 460), radius=42,
                        outline=(*PURPLE_LIGHT, 92), width=2)


def text_block(base: Image.Image, status: str, progress: float,
               show_bar: bool = True) -> None:
    d = ImageDraw.Draw(base)
    d.text((240, 86), "AI 盲盒", font=font(30), fill=(240, 238, 255, 255),
           anchor="mm")
    d.text((240, 120), "B L I N D   B O X", font=font(11, bold=False),
           fill=(158, 152, 196, 210), anchor="mm")
    if show_bar:
        d.rounded_rectangle((140, 420, 340, 427), radius=4,
                            fill=(255, 255, 255, 42))
        w = round(200 * clamp(progress))
        if w > 0:
            d.rounded_rectangle((140, 420, 140 + w, 427), radius=4,
                                fill=(*PURPLE, 255))
        pct = f"{round(clamp(progress) * 100)}%"
        d.text((240, 443), pct, font=font(12, bold=False),
               fill=(170, 165, 205, 225), anchor="mm")
    d.text((240, 396), status, font=font(16),
           fill=(214, 210, 238, 255), anchor="mm")


def cone(base: Image.Image, p: float, boost: float = 1.0) -> None:
    """从盒口向上扩散的光锥：逐行画渐变再模糊，避免硬边。"""
    strength = clamp((p - 0.10) / 0.90) * boost
    if strength <= 0.001:
        return
    top, bottom = 126, 258
    half_top = 22 + 104 * strength
    half_bottom = 78
    im = layer()
    d = ImageDraw.Draw(im)
    for y in range(top, bottom):
        t = (y - top) / (bottom - top)
        half = lerp(half_top, half_bottom, t)
        a = int((70 + 150 * t) * strength)
        d.line([(240 - half, y), (240 + half, y)],
               fill=(205, 196, 255, min(255, a)), width=1)
    base.alpha_composite(im.filter(ImageFilter.GaussianBlur(10)))


def box_body(base: Image.Image, p: float) -> None:
    d = ImageDraw.Draw(base)
    # 盒底投影
    shadow = soft_glow((150, 356, 330, 398), (8, 5, 28, 150), 18)
    base.alpha_composite(shadow)
    d = ImageDraw.Draw(base)

    # 右侧面 / 正面
    d.polygon([(324, 252), (352, 226), (352, 336), (324, 362)],
              fill=(*PURPLE_LIGHT, 255))
    d.rounded_rectangle((156, 252, 324, 362), radius=14,
                        fill=(255, 255, 255, 255))
    # 盒口内部（盒盖抬起后露出）
    opening = int(120 + 110 * p)
    d.polygon([(156, 252), (184, 226), (352, 226), (324, 252)],
              fill=(58, 44, 128, 255))
    d.line([(156, 252), (184, 226), (352, 226), (324, 252)],
           fill=(*GOLD, min(255, opening)), width=3)
    # 正面问号
    d.text((240, 316), "?", font=font(78), fill=(*PURPLE_DEEP, 255),
           anchor="mm")
    # 盒盖缝高光
    d.line([(156, 288), (324, 288)], fill=(214, 205, 246, 235), width=3)

    # 盒缝透光
    if p > 0.02:
        seam = int(255 * clamp(p * 1.15))
        g = soft_glow((168, 236, 312, 268), (255, 232, 170, seam), 16)
        base.alpha_composite(g)
        d = ImageDraw.Draw(base)
        d.line([(160, 252), (322, 252)], fill=(255, 244, 206, seam), width=4)


def lid(base: Image.Image, p: float, flash_t: float = -1.0) -> None:
    """盒盖：随进度上抬 + 轻微倾斜；终场再向左上飞出。"""
    lift = 54 * clamp(p)
    tilt = 14 * clamp(p)
    fly = 0.0
    if flash_t >= 0:
        fly = ease(clamp(flash_t * 1.5)) * 150
        lift += ease(clamp(flash_t * 1.5)) * 60
    x0, x1 = 146 - fly, 334 - fly
    y_top, y_bot = 210 - lift, 258 - lift
    d = ImageDraw.Draw(base)
    # 顶面
    d.polygon([(x0, y_top), (x0 + 30, y_top - 24),
               (x1 + 30, y_top - 24 - tilt), (x1, y_top - tilt)],
              fill=(*PURPLE_PALE, 255))
    # 盖身
    d.polygon([(x0, y_top), (x1, y_top - tilt),
               (x1, y_bot - tilt), (x0, y_bot)],
              fill=(*PURPLE, 255))
    # 盖沿高光
    d.line([(x0, y_bot - 5), (x1, y_bot - 5 - tilt)],
           fill=(214, 206, 255, 235), width=4)
    d.line([(x0, y_top), (x1, y_top - tilt)],
           fill=(255, 255, 255, 150), width=3)


def sparkles(base: Image.Image, p: float) -> None:
    n = 3 + int(9 * p)
    d = ImageDraw.Draw(base)
    for i in range(n):
        ang = i * 2.39996 + p * 2.1
        rad = 96 + 62 * ((i * 0.37 + p) % 1.0)
        cx = 240 + math.cos(ang) * rad
        cy = 236 + math.sin(ang) * rad * 0.74
        alpha = int(150 + 105 * p)
        if i % 3 == 0:
            star(d, cx, cy, 5 + 4 * p, (255, 255, 255, alpha), rot=ang)
        else:
            d.ellipse((cx - 2, cy - 2, cx + 2, cy + 2),
                      fill=(*GOLD, alpha))


def render_open(p: float) -> Image.Image:
    p = clamp(p)
    base = layer()
    card(base)
    base.alpha_composite(soft_glow((112, 130, 368, 372),
                                  (*PURPLE, int(38 + 95 * p)), 42))
    cone(base, p)
    box_body(base, p)
    lid(base, p)
    sparkles(base, p)
    status = "正在开启盲盒" if p < 0.995 else "即将开启"
    text_block(base, status, p)
    return base


def render_flash(t: float) -> Image.Image:
    t = clamp(t)
    base = render_open(1.0)
    # 终场状态文字 + 满进度
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((150, 388, 330, 410), radius=8, fill=(*INK, 220))
    d.text((240, 396), "开启完成 · 正在进入", font=font(16),
           fill=(255, 236, 176, 255), anchor="mm")
    d.rounded_rectangle((140, 420, 340, 427), radius=4, fill=(*GOLD, 255))

    burst = ease(clamp(t * 1.7))
    glow = soft_glow((240 - 40 - 150 * burst, 230 - 40 - 150 * burst,
                      240 + 40 + 150 * burst, 230 + 40 + 150 * burst),
                     (215, 202, 255, int(150 * (1 - 0.55 * t))), 34)
    base.alpha_composite(glow)

    rays = layer()
    rd = ImageDraw.Draw(rays)
    length = 46 + 250 * burst
    for k in range(12):
        ang = k * math.pi / 6 + 0.12
        x = 240 + math.cos(ang) * length
        y = 230 + math.sin(ang) * length
        rd.line([(240, 230), (x, y)],
                fill=(255, 238, 190, int(210 * (1 - 0.65 * t))), width=4)
    base.alpha_composite(rays.filter(ImageFilter.GaussianBlur(2)))

    d = ImageDraw.Draw(base)
    for k in range(12):
        ang = k * math.pi / 6 - math.pi / 2 + 0.15
        rad = 38 + 205 * ease(t)
        cx = 240 + math.cos(ang) * rad
        cy = 230 + math.sin(ang) * rad
        star(d, cx, cy, 15 * (1 - 0.45 * t),
             (*GOLD, int(255 * (1 - 0.7 * t))), rot=ang)

    # 中心亮核
    core = soft_glow((204, 194, 276, 266), (255, 255, 255, int(210 * (1 - t))), 22)
    base.alpha_composite(core)

    # 最后 45% 时间整体淡出，让主窗口从浮层后面显现
    fade = 1.0 - ease(clamp((t - 0.55) / 0.45))
    if fade < 0.999:
        a = base.getchannel("A").point(lambda v: int(v * fade))
        base.putalpha(a)
    return base


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.png"):
        old.unlink()

    for i in range(N_OPEN):
        render_open(i / (N_OPEN - 1)).save(OUT / f"open_{i:03d}.png",
                                          optimize=True)
    for i in range(N_FLASH):
        render_flash(i / (N_FLASH - 1)).save(OUT / f"flash_{i:03d}.png",
                                            optimize=True)
    # 首帧额外存一份「预乘 alpha 的 BGRA 原始字节」：双击后只需一次文件读 +
    # memmove 就能上屏，连 Pillow 都不用导入，把首帧耗时压到最低。
    with Image.open(OUT / "open_000.png") as first:
        r, g, b, a = first.convert("RGBA").split()
        raw = Image.merge("RGBA", (ImageChops.multiply(b, a),
                                   ImageChops.multiply(g, a),
                                   ImageChops.multiply(r, a), a)).tobytes()
    (OUT / "first.bgra").write_bytes(raw)

    manifest = {"size": SIZE, "open": N_OPEN, "flash": N_FLASH,
                "fps": 30, "flash_seconds": 0.95, "first_raw": "first.bgra"}
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 交付预览：8 张关键帧拼图
    picks = [("open", 0), ("open", 8), ("open", 18), ("open", 30),
             ("flash", 2), ("flash", 5), ("flash", 9), ("flash", 13)]
    sheet = Image.new("RGB", (SIZE * 4, (SIZE + 26) * 2), (24, 24, 32))
    sd = ImageDraw.Draw(sheet)
    for idx, (kind, i) in enumerate(picks):
        im = Image.open(OUT / f"{kind}_{i:03d}.png").convert("RGBA")
        bg = Image.new("RGBA", (SIZE, SIZE), (34, 34, 44, 255))
        bg.alpha_composite(im)
        x, y = (idx % 4) * SIZE, (idx // 4) * (SIZE + 26)
        sheet.paste(bg.convert("RGB"), (x, y))
        sd.text((x + 10, y + SIZE + 4), f"{kind}_{i:03d}", fill=(210, 210, 225))
    preview = Path(__file__).resolve().parent / "splash_frames_preview.png"
    sheet.save(preview)

    total = sum(p.stat().st_size for p in OUT.glob("*.png"))
    print(f"帧数: open={N_OPEN} flash={N_FLASH} 尺寸={SIZE}x{SIZE}")
    print(f"PNG 合计: {total / 1024 / 1024:.2f} MB")
    print("manifest:", manifest)
    print("预览:", preview)


if __name__ == "__main__":
    main()
