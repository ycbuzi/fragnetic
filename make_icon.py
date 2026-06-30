#!/usr/bin/env python3
"""
make_icon.py -- generates the FRAGROUTE app icon.

Produces:
    assets/fragroute.ico   (multi-size 16..256, used for the .exe + window)
    assets/fragroute.png   (256px, used for the system-tray icon)

Pure Pillow, no font files needed -- the mark is a neon "route target":
a hex frame + concentric rings + crosshair + a routing chevron, in the
FRAGROUTE pink/cyan palette with a soft glow. Safe to re-run anytime.
"""
import math
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    raise SystemExit("Pillow is required:  pip install pillow")

PINK = (255, 47, 146)
CYAN = (47, 240, 212)
BG0  = (12, 12, 20)
BG1  = (22, 16, 32)

S = 256  # master canvas size
C = S / 2


def _rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def _glow_layer():
    """Transparent layer we draw neon onto, later blurred for the glow pass."""
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def _draw_ring(draw, cx, cy, r, color, width, alpha=255):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 outline=color + (alpha,), width=width)


def _draw_hex(draw, cx, cy, r, color, width, alpha=255, rot=math.pi / 6):
    pts = []
    for i in range(6):
        a = rot + i * math.pi / 3
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    draw.line(pts + [pts[0]], fill=color + (alpha,), width=width, joint="curve")


def build():
    # --- background tile (vertical-ish gradient + rounded corners) ---
    base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    gd = grad.load()
    for y in range(S):
        t = y / (S - 1)
        # blend BG0 -> BG1 with a slight diagonal
        r = int(BG0[0] * (1 - t) + BG1[0] * t)
        g = int(BG0[1] * (1 - t) + BG1[1] * t)
        b = int(BG0[2] * (1 - t) + BG1[2] * t)
        for x in range(S):
            d = ((x / S) - 0.5) * 8  # tiny horizontal tint
            gd[x, y] = (max(0, min(255, r + int(d))),
                        max(0, min(255, g)),
                        max(0, min(255, b + int(d))), 255)
    base = Image.composite(grad, base, _rounded_mask(S, 52))

    # subtle inner border
    bd = ImageDraw.Draw(base)
    bd.rounded_rectangle([6, 6, S - 7, S - 7], radius=46,
                         outline=(255, 255, 255, 22), width=2)

    # --- neon mark on its own layer (so we can glow it) ---
    fg = _glow_layer()
    d = ImageDraw.Draw(fg)

    # faint hex frame so the mark still reads as a "tech badge"
    _draw_hex(d, C, C, 96, CYAN, 5, alpha=70)

    # --- the Fragnetic "F": a bold monogram leaned forward for kinetic motion,
    #     two-tone (pink stem/arms + a cyan tip + an electric spark) to echo the
    #     FRAG(pink)/NETIC(cyan) wordmark. Reads cleanly down to 16px. ----------
    sw = 28                       # stroke width
    top_y, bot_y, mid_y = 72, 190, 128
    base_x = C - 30               # base of the stem (the lean grows toward the top)
    lean = 30

    def _ix(y):                   # x of the stem at height y, given the forward lean
        return base_x + lean * (bot_y - y) / (bot_y - top_y)

    # vertical stem
    d.line([(_ix(bot_y), bot_y), (_ix(top_y), top_y)], fill=PINK + (255,), width=sw, joint="curve")
    # top arm (long)
    ay = top_y + sw / 2 - 2
    d.line([(_ix(ay), ay), (_ix(ay) + 96, ay)], fill=PINK + (255,), width=sw, joint="curve")
    # middle arm (short) -- its outer third turns cyan (the two-tone nod)
    d.line([(_ix(mid_y), mid_y), (_ix(mid_y) + 60, mid_y)], fill=PINK + (255,), width=sw, joint="curve")
    d.line([(_ix(mid_y) + 40, mid_y), (_ix(mid_y) + 74, mid_y)], fill=CYAN + (255,), width=sw, joint="curve")

    # electric spark off the top-right (the "-netic" energy): a small lightning zigzag
    sx, sy = _ix(ay) + 96 + 16, ay - 4
    d.line([(sx - 2, sy - 26), (sx + 12, sy - 4), (sx - 4, sy + 2), (sx + 10, sy + 26)],
           fill=CYAN + (255,), width=9, joint="curve")

    # crosshair ticks bottom-left/right corners -- keeps the "aim" identity, subtle
    for dx, dy in ((1, 1), (-1, 1)):
        x1, y1 = C + dx * 92, C + dy * 86
        d.line([(x1, y1), (x1 - dx * 26, y1)], fill=CYAN + (180,), width=6)

    # glow: blurred copy of the neon layer under the crisp layer
    glow = fg.filter(ImageFilter.GaussianBlur(7))
    out = Image.alpha_composite(base, glow)
    out = Image.alpha_composite(out, fg)

    # clip everything back to the rounded tile
    out.putalpha(Image.composite(out.getchannel("A"),
                                 Image.new("L", (S, S), 0),
                                 _rounded_mask(S, 52)))

    assets = Path(__file__).resolve().parent / "assets"
    assets.mkdir(exist_ok=True)
    png = assets / "fragroute.png"
    ico = assets / "fragroute.ico"
    out.save(png)
    out.save(ico, sizes=[(256, 256), (128, 128), (64, 64),
                         (48, 48), (32, 32), (16, 16)])
    print("wrote", png)
    print("wrote", ico)
    return png, ico


if __name__ == "__main__":
    build()
