"""Build the launcher icons from the channel poster.

Run: python scripts/make_icons.py [path-to-poster]

Two decisions worth recording:

  * The icon is a FULL-BLEED square crop of the poster, not the character
    pasted onto a flat colour. The first attempt did the latter and the result
    showed an obvious portrait rectangle floating inside the icon, because the
    poster's curtain is a gradient with lit arches down the sides and no flat
    colour matches its edges.

  * The title text is left out. At 48dp "TECHNICAL JAADUGAR" is an unreadable
    smear; the launcher already prints the app name underneath.

The crop is sized so the magician occupies roughly the inner 68% of the frame,
which is the part an adaptive icon guarantees to show - the launcher masks the
rest to whatever shape it likes.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

DEFAULT_SRC = Path(r"F:\Youtube\Tools\VideoCapture_20220119-233209.jpg")
RES = Path(__file__).resolve().parents[1] / "android/app/src/main/res"
BG = (93, 20, 39)  # curtain red, averaged from the poster's edges

# Where the magician sits in the poster, as fractions of width and height.
FIGURE = (0.30, 0.22, 0.72, 0.765)
# How much of the icon the figure should occupy. The adaptive-icon safe zone is
# 72/108 = 0.667, so this keeps the hat and feet inside it.
FIGURE_FRACTION = 0.68
# Top of the poster's title block. The square must stop above it: a crop that
# clips through "TECHNICAL" leaves half a word of letterforms along the bottom
# of the icon, which reads as damage rather than design.
TITLE_TOP = 0.775

DENSITIES = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def build_tile(src: Image.Image) -> Image.Image:
    w, h = src.size
    fx0, fy0, fx1, fy1 = FIGURE
    fig_h = (fy1 - fy0) * h
    ceiling = TITLE_TOP * h          # the square may not cross this line
    side = min(fig_h / FIGURE_FRACTION, float(w), ceiling)
    cx = (fx0 + fx1) / 2 * w
    cy = (fy0 + fy1) / 2 * h
    left = max(0, min(w - side, cx - side / 2))
    top = max(0, min(ceiling - side, cy - side / 2))
    return src.crop((int(left), int(top), int(left + side), int(top + side)))


def masked(tile: Image.Image, px: int, shape: str) -> Image.Image:
    art = tile.resize((px, px), Image.LANCZOS).convert("RGBA")
    # Antialias the mask by building it at 4x and downsampling.
    big = Image.new("L", (px * 4, px * 4), 0)
    draw = ImageDraw.Draw(big)
    if shape == "round":
        draw.ellipse([0, 0, px * 4 - 1, px * 4 - 1], fill=255)
    else:
        draw.rounded_rectangle([0, 0, px * 4 - 1, px * 4 - 1],
                               radius=int(px * 4 * 0.18), fill=255)
    art.putalpha(big.resize((px, px), Image.LANCZOS))
    return art


def main() -> int:
    src_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    if not src_path.exists():
        print(f"poster not found: {src_path}")
        return 1
    tile = build_tile(Image.open(src_path).convert("RGB"))
    print(f"tile {tile.size} from {src_path.name}")

    for name, factor in DENSITIES.items():
        out = RES / f"mipmap-{name}"
        out.mkdir(parents=True, exist_ok=True)
        # Adaptive foreground: full-bleed, no mask - the launcher applies one.
        fg = int(108 * factor)
        tile.resize((fg, fg), Image.LANCZOS).save(out / "ic_launcher_foreground.png")
        legacy = int(48 * factor)
        masked(tile, legacy, "square").save(out / "ic_launcher.png")
        masked(tile, legacy, "round").save(out / "ic_launcher_round.png")
        print(f"  {name}: {fg}px foreground, {legacy}px legacy")

    store = RES.parents[3] / "logo-512.png"
    masked(tile, 512, "square").save(store)
    print(f"store asset: {store}")
    print(f"background colour: #{BG[0]:02X}{BG[1]:02X}{BG[2]:02X}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
