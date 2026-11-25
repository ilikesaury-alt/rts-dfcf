"""生成 rts-desktop 应用图标（多尺寸 .ico + 预览 PNG）。

为什么**程序化绘制**而不是用 AI 生成图：
  1. AI 出图带平台水印（实测右下角有「MiniMax AI 生成」），要去掉就得裁切，
     而裁切会连带切掉构图余量、圆角对不齐；
  2. 图标要在 **16×16** 下仍可辨认 —— 这要求几何形状可控（线宽、留白按像素对齐），
     生成式模型给不了这种确定性；
  3. 可复现：改配色/比例重跑本脚本即可，不依赖某次抽样的结果。

图纸（1024 基准，全部按比例缩放，小尺寸用 4x 超采样 + LANCZOS 保证边缘干净）：
  - 圆角方底：深蓝渐变（左上偏亮、右下压深），是应用图标的通行容器；
  - 主体：一条粗白折线（两段上扬）+ 上升箭头，呼应「飙升」但不编码涨跌语义；
  - 落点：一个青色圆点，作为视觉锚点；
  - 无任何文字、字母、数字。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# ── 配色（与前端 index.css 的品牌色同源，浅/深两套容器色）───────────────
BRAND = (37, 99, 235)      # --brand  #2563eb
BRAND_SOFT = (14, 143, 168)  # --brand-soft  #0e8fa8
DEEP = (11, 32, 74)        # 深底（右下）
LIGHT = (29, 78, 216)      # 亮底（左上）

SUPERSAMPLE = 4            # 超采样倍率：1024 实际按 4096 画，再缩到 1024


def _lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2, strict=True))


def _bg_gradient(size: int) -> Image.Image:
    """斜向线性渐变（左上 → 右下）。"""
    g = Image.new("RGB", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1))
            px[x, y] = _lerp(LIGHT, DEEP, t)
    return g


def _rounded_mask(size: int, radius_ratio: float = 0.225) -> Image.Image:
    """圆角方块遮罩（iOS/Windows 图标常用的圆角比例约 0.22~0.25）。"""
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    r = round(size * radius_ratio)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=255)
    return m


def _draw_mark(size: int, variant: str) -> Image.Image:
    """在透明底上画主体图形（折线 + 箭头 + 落点）。"""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    S = size

    if variant == "line":
        # 折线：左下 → 中下 → 右上，收在箭头尖
        pts = [
            (0.26 * S, 0.70 * S),
            (0.44 * S, 0.54 * S),
            (0.58 * S, 0.63 * S),
            (0.76 * S, 0.30 * S),
        ]
        w = round(0.072 * S)
        d.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")
        # 圆头端点（line 的 joint 不处理首尾）
        for p in (pts[0], pts[-1]):
            r = w / 2
            d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=(255, 255, 255, 255))
        # 箭头尖：在末点右上方画三角
        tip = (0.76 * S, 0.30 * S)
        ah = 0.13 * S          # 箭头长度
        aw = 0.105 * S         # 箭头半宽
        d.polygon(
            [
                (tip[0] + ah * 0.62, tip[1] - ah * 0.78),
                (tip[0] - aw * 0.55, tip[1] - ah * 0.10),
                (tip[0] + ah * 0.10, tip[1] + aw * 0.62),
            ],
            fill=(255, 255, 255, 255),
        )
        # 青色落点
        cr = 0.068 * S
        c = (0.62 * S, 0.80 * S)
        d.ellipse([c[0] - cr, c[1] - cr, c[0] + cr, c[1] + cr], fill=BRAND_SOFT + (255,))
    elif variant == "bars":
        # 三根递增柱 + 斜向箭头
        bw = 0.125 * S
        gap = 0.075 * S
        base = 0.74 * S
        heights = [0.20 * S, 0.33 * S, 0.47 * S]
        total = 3 * bw + 2 * gap
        x0 = (S - total) / 2
        for i, h in enumerate(heights):
            x = x0 + i * (bw + gap)
            d.rounded_rectangle(
                [x, base - h, x + bw, base], radius=bw * 0.32, fill=(255, 255, 255, 235)
            )
        # 斜箭头
        p0 = (x0 + bw * 0.5, base - heights[0] - 0.05 * S)
        p1 = (x0 + total - bw * 0.5, base - heights[2] - 0.11 * S)
        w = round(0.058 * S)
        d.line([p0, p1], fill=BRAND_SOFT + (255,), width=w)
        ah, aw = 0.115 * S, 0.095 * S
        d.polygon(
            [
                (p1[0] + ah * 0.60, p1[1] - ah * 0.76),
                (p1[0] - aw * 0.60, p1[1] - ah * 0.06),
                (p1[0] + ah * 0.08, p1[1] + aw * 0.60),
            ],
            fill=BRAND_SOFT + (255,),
        )
    else:
        raise ValueError(f"unknown variant: {variant}")
    return layer


def render(size: int, variant: str) -> Image.Image:
    """渲染一张 size×size 的 RGBA 图标。"""
    S = size * SUPERSAMPLE
    bg = _bg_gradient(S).convert("RGBA")
    bg.putalpha(_rounded_mask(S))
    mark = _draw_mark(S, variant)
    out = Image.alpha_composite(bg, mark)
    return out.resize((size, size), Image.LANCZOS)


def build(variant: str, out_ico: Path, out_preview: Path) -> None:
    preview = render(512, variant)
    preview.convert("RGB").save(out_preview, "PNG")

    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [render(s, variant).convert("RGBA") for s in sizes]
    # 保存多尺寸 .ico：以最大尺寸为主图，附带全部尺寸
    imgs[-1].save(out_ico, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    print(f"[{variant}] -> {out_ico}  sizes={sizes}")
    print(f"[{variant}] -> {out_preview}")


def main() -> int:
    here = Path(__file__).resolve().parent
    which = sys.argv[1] if len(sys.argv) > 1 else "line"
    build(which, here / f"icon-{which}.ico", here / f"icon-{which}-preview.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
