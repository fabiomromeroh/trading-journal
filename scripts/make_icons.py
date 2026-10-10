"""Generate the app icon set (original artwork, drawn procedurally; needs Pillow: requirements-dev.txt).

    python scripts/make_icons.py

Writes app/static/icons/: icon.svg (source), favicon-16/32/48.png, favicon.ico, apple-touch-icon.png (180),
icon-192.png, icon-512.png and icon-maskable-512.png. Same geometry feeds the SVG and the PNGs.
Design: dark rounded square, three rising candlesticks in the app's indigo -> cyan accent.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG_TOP, BG_BOT = (22, 30, 48), (9, 13, 22)          # #161e30 -> #090d16
ACC_TOP, ACC_BOT = (34, 211, 238), (99, 102, 241)   # cyan #22d3ee -> indigo #6366f1
# Candles on a 512 canvas: (centre x, wick top, wick bottom, body top, body bottom)
CANDLES = [(150, 270, 412, 302, 384), (256, 190, 372, 232, 332), (362, 104, 300, 142, 268)]
GRAD_Y = (100, 416)  # accent gradient spans the candles
BODY_W, WICK_W, BODY_R = 58, 12, 12
RADIUS = 112  # corner radius of the rounded square (22%)


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vgrad(size, top, bot, y0=0.0, y1=None):
    """Vertical gradient top->bot between rows y0..y1 (clamped outside)."""
    y1 = size - 1 if y1 is None else y1
    col = Image.new("RGB", (1, size))
    for y in range(size):
        col.putpixel((0, y), _lerp(top, bot, min(1, max(0, (y - y0) / max(1, y1 - y0)))))
    return col.resize((size, size))


def render(px: int, *, maskable=False, opaque=False, scale=1.0) -> Image.Image:
    """px x px icon. maskable/opaque = full-bleed square background (no transparent corners)."""
    S = 8                                     # supersampling
    n = px * S
    k = n / 512
    # mark, scaled about the canvas centre (smaller for maskable so it stays in the safe zone)
    def X(v): return (256 + (v - 256) * scale) * k
    def Y(v): return (256 + (v - 256) * scale) * k
    mark = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(mark)
    ww = max(WICK_W, 18 if px <= 48 else WICK_W) * scale * k  # chunkier wicks at tiny sizes
    for cx, wt, wb, bt, bb in CANDLES:
        d.rounded_rectangle([X(cx) - ww / 2, Y(wt), X(cx) + ww / 2, Y(wb)], radius=ww / 2, fill=255)
        d.rounded_rectangle([X(cx) - BODY_W * scale * k / 2, Y(bt), X(cx) + BODY_W * scale * k / 2, Y(bb)],
                            radius=BODY_R * scale * k, fill=255)
    img = _vgrad(n, BG_TOP, BG_BOT).convert("RGBA")
    # soft accent glow behind the mark
    glow = Image.new("L", (n, n), 0)
    ImageDraw.Draw(glow).ellipse([n * .2, n * .2, n * .8, n * .8], fill=70)
    glow = glow.filter(ImageFilter.GaussianBlur(n * .09))
    img.paste(Image.new("RGBA", (n, n), ACC_BOT + (255,)), (0, 0), glow)
    accent = _vgrad(n, ACC_TOP, ACC_BOT, Y(GRAD_Y[0]), Y(GRAD_Y[1])).convert("RGBA")
    img.paste(accent, (0, 0), mark)
    if not (maskable or opaque):
        m = Image.new("L", (n, n), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, n - 1, n - 1], radius=RADIUS * k, fill=255)
        img.putalpha(m)
    return img.resize((px, px), Image.LANCZOS)


def svg() -> str:
    def h(c): return "#%02x%02x%02x" % c
    parts = []
    for cx, wt, wb, bt, bb in CANDLES:
        parts.append(f'<rect x="{cx - WICK_W / 2}" y="{wt}" width="{WICK_W}" height="{wb - wt}" rx="{WICK_W / 2}"/>')
        parts.append(f'<rect x="{cx - BODY_W / 2}" y="{bt}" width="{BODY_W}" height="{bb - bt}" rx="{BODY_R}"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <title>Trading Journal</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{h(BG_TOP)}"/><stop offset="1" stop-color="{h(BG_BOT)}"/></linearGradient>
    <linearGradient id="ac" gradientUnits="userSpaceOnUse" x1="0" y1="{GRAD_Y[0]}" x2="0" y2="{GRAD_Y[1]}"><stop offset="0" stop-color="{h(ACC_TOP)}"/><stop offset="1" stop-color="{h(ACC_BOT)}"/></linearGradient>
    <filter id="blur" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="46"/></filter>
  </defs>
  <rect width="512" height="512" rx="{RADIUS}" fill="url(#bg)"/>
  <ellipse cx="256" cy="256" rx="170" ry="170" fill="{h(ACC_BOT)}" opacity=".22" filter="url(#blur)"/>
  <g fill="url(#ac)">
    {chr(10).join("    " + p for p in parts).strip()}
  </g>
</svg>
'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "icon.svg").write_text(svg())
    for s in (16, 32, 48):
        render(s).save(OUT / f"favicon-{s}.png", optimize=True)
    render(180, opaque=True).save(OUT / "apple-touch-icon.png", optimize=True)
    render(192).save(OUT / "icon-192.png", optimize=True)
    render(512).save(OUT / "icon-512.png", optimize=True)
    render(512, maskable=True, scale=0.82).save(OUT / "icon-maskable-512.png", optimize=True)
    render(256).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print("wrote", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
