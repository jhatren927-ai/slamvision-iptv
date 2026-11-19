#!/usr/bin/env python3
"""Make a simple name tile per channel (static/logos/<id>.png).

Run by hand when channels are added: python3 tools/make_logos.py channels.json
(channels.json = a saved copy of /api/channels). Channels without a tile
fall back to static/logo.png in generate.py.
"""
import json, sys, textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from generate import display_name  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 320, 180
OUT = Path(__file__).resolve().parent.parent / "static" / "logos"


def wrap(text, width):
    return textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)


def fit(draw, text):
    for size in range(44, 13, -2):
        font = ImageFont.truetype(FONT, size)
        lh = int(size * 1.08)
        for width in range(max(6, len(text)), 3, -1):
            lines = wrap(text, width)
            if len(lines) > 3:
                break
            tw = max(draw.textlength(ln, font=font) for ln in lines)
            if tw <= W - 28 and lh * len(lines) <= H - 50:
                return font, lines, lh
    font = ImageFont.truetype(FONT, 14)
    return font, wrap(text, 30)[:3], 16


def tile(name, number):
    im = Image.new("RGB", (W, H), (14, 16, 28))
    d = ImageDraw.Draw(im)
    d.rectangle([0, H - 10, W, H], fill=(250, 204, 21))
    small = ImageFont.truetype(FONT, 18)
    d.text((14, 10), str(number), font=small, fill=(250, 204, 21))
    font, lines, lh = fit(d, name.upper())
    y = (H - 10 - lh * len(lines)) // 2 + lh // 2 + 6
    for ln in lines:
        d.text((W // 2, y), ln, font=font, fill=(255, 255, 255), anchor="mm")
        y += lh
    return im


def main():
    chans = json.load(open(sys.argv[1]))
    OUT.mkdir(parents=True, exist_ok=True)
    for c in chans:
        if not c.get("id"):
            continue
        tile(display_name(c), int(c.get("number") or 0)).quantize(32).save(OUT / f"{c["id"]}.png", optimize=True)
    print(f"wrote {len(list(OUT.glob('*.png')))} tiles to {OUT}")


if __name__ == "__main__":
    main()
