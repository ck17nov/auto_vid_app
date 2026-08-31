"""Procedural visual generator - the always-available, zero-cost, zero-key path.

This is NOT a grey placeholder.  It renders a designed abstract frame:
a deterministic palette derived from the scene keywords, additive light blobs,
a directional key light, particle/ray texture, geometric accents, film grain and
a vignette.  Everything is drawn at full render size, so it stays sharp under
Ken Burns zoom.

Compositing note: light is added with ImageChops.screen, never Image.blend.
Blending toward a dark layer flattens contrast and produces a muddy field;
screening preserves the base colour and makes highlights actually glow.
"""
from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..core.models import Asset
from .base import VisualRequest

# Palettes: (name, [deep, mid, bright, shadow]).  Deep/shadow dominate so white
# captions stay legible; `bright` is used only for light sources.
PALETTES: list[tuple[str, list[tuple[int, int, int]]]] = [
    ("deep_space",   [(5, 8, 22), (26, 34, 86), (92, 128, 255), (3, 4, 12)]),
    ("teal_tech",    [(3, 16, 22), (10, 58, 70), (54, 220, 220), (2, 10, 14)]),
    ("ember",        [(24, 8, 4), (86, 26, 8), (255, 140, 40), (14, 5, 3)]),
    ("indigo_pulse", [(9, 6, 28), (44, 24, 96), (150, 90, 255), (5, 3, 16)]),
    ("forest",       [(4, 16, 11), (16, 54, 34), (80, 220, 130), (2, 9, 6)]),
    ("crimson",      [(22, 4, 12), (78, 12, 40), (255, 60, 110), (12, 2, 7)]),
    ("slate_gold",   [(12, 13, 16), (40, 42, 50), (255, 196, 80), (7, 8, 10)]),
    ("ocean",        [(3, 11, 28), (10, 44, 88), (60, 170, 255), (2, 6, 16)]),
    ("violet_haze",  [(16, 6, 26), (58, 20, 78), (220, 90, 220), (9, 3, 15)]),
    ("mono_steel",   [(10, 12, 14), (38, 44, 52), (200, 220, 240), (5, 6, 8)]),
]

# Brighter, softer palettes for child-directed content.
KID_PALETTES: list[tuple[str, list[tuple[int, int, int]]]] = [
    ("sunny",  [(30, 58, 110), (62, 120, 190), (255, 226, 120), (20, 40, 80)]),
    ("meadow", [(26, 70, 50), (56, 130, 88), (190, 245, 140), (16, 46, 34)]),
    ("berry",  [(70, 36, 88), (124, 66, 130), (255, 170, 220), (46, 22, 58)]),
    ("peach",  [(96, 56, 44), (166, 100, 70), (255, 200, 150), (62, 34, 26)]),
]


def _seed_for(req: VisualRequest) -> int:
    basis = f"{req.scene_index}|{req.prompt}|{'|'.join(req.keywords)}"
    return int(hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()[:12], 16)


def _linear_gradient(size: tuple[int, int], a: tuple[int, int, int],
                     b: tuple[int, int, int], angle: float) -> Image.Image:
    """Linear gradient at an arbitrary angle (drawn small, upscaled smooth)."""
    w, h = size
    small = Image.new("RGB", (96, 96))
    px = small.load()
    rad = math.radians(angle)
    dx, dy = math.cos(rad), math.sin(rad)
    denom = abs(dx) + abs(dy) or 1.0
    for y in range(96):
        for x in range(96):
            t = ((x / 95) * dx + (y / 95) * dy) / denom
            t = min(max((t + 1) / 2, 0.0), 1.0)
            t = t * t * (3 - 2 * t)                      # smoothstep
            px[x, y] = (int(a[0] + (b[0] - a[0]) * t),
                        int(a[1] + (b[1] - a[1]) * t),
                        int(a[2] + (b[2] - a[2]) * t))
    return small.resize((w, h), Image.BICUBIC)


def _radial_light(size: tuple[int, int], color: tuple[int, int, int],
                  cx: float, cy: float, radius: float,
                  intensity: float) -> Image.Image:
    """A single soft light source as an additive layer."""
    w, h = size
    sw, sh = 160, int(160 * h / w) or 160
    layer = Image.new("L", (sw, sh), 0)
    d = ImageDraw.Draw(layer)
    px, py = cx * sw, cy * sh
    r = radius * sw
    # Nested ellipses build a smooth falloff before blurring.
    steps = 14
    for i in range(steps, 0, -1):
        frac = i / steps
        val = int(255 * intensity * (1 - frac) ** 1.7)
        rr = r * frac
        d.ellipse([px - rr, py - rr, px + rr, py + rr], fill=val)
    layer = layer.filter(ImageFilter.GaussianBlur(sw * 0.07))
    layer = layer.resize((w, h), Image.BICUBIC)
    tint = Image.new("RGB", (w, h), color)
    return ImageChops.multiply(tint, Image.merge("RGB", (layer, layer, layer)))


def _particles(size: tuple[int, int], rng: random.Random,
               color: tuple[int, int, int], count: int) -> Image.Image:
    """Fine bright specks - reads as dust/stars and gives the eye detail."""
    w, h = size
    layer = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        x, y = rng.randrange(w), rng.randrange(h)
        r = rng.choice([1, 1, 1, 2, 2, 3])
        a = rng.uniform(0.25, 1.0)
        c = tuple(int(v * a) for v in color)
        d.ellipse([x - r, y - r, x + r, y + r], fill=c)
    return layer.filter(ImageFilter.GaussianBlur(0.6))


def _rays(size: tuple[int, int], rng: random.Random,
          color: tuple[int, int, int]) -> Image.Image:
    """Wide, very soft light shafts from one edge."""
    w, h = size
    layer = Image.new("RGB", (w // 3, h // 3), (0, 0, 0))
    d = ImageDraw.Draw(layer)
    lw, lh = layer.size
    ox = rng.uniform(-0.2, 1.2) * lw
    dim = tuple(max(0, int(v * 0.5)) for v in color)
    for _ in range(rng.randint(3, 5)):
        spread = rng.uniform(0.08, 0.24) * lw
        tip = ox + rng.uniform(-0.35, 0.35) * lw
        d.polygon([(tip, -lh * 0.1),
                   (tip - spread, lh * 1.2),
                   (tip + spread, lh * 1.2)], fill=dim)
    layer = layer.filter(ImageFilter.GaussianBlur(lw * 0.06))
    return layer.resize((w, h), Image.BICUBIC)


def _accent_geometry(size: tuple[int, int], rng: random.Random,
                     color: tuple[int, int, int]) -> Image.Image:
    """Thin geometric structure, additive so it glows against the field."""
    w, h = size
    layer = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(layer)
    line = max(2, w // 400)
    dim = tuple(int(v * 0.42) for v in color)
    faint = tuple(int(v * 0.20) for v in color)
    style = rng.randrange(4)

    if style == 0:                                    # concentric arcs
        cx, cy = int(w * rng.uniform(0.3, 0.7)), int(h * rng.uniform(0.26, 0.5))
        for i in range(5):
            r = int(min(w, h) * (0.16 + i * 0.10))
            d.ellipse([cx - r, cy - r, cx + r, cy + r],
                      outline=dim if i % 2 == 0 else faint, width=line)
    elif style == 1:                                  # diagonal rule lines
        for i in range(7):
            off = int(w * (-0.5 + i * 0.26))
            d.line([(off, 0), (off + int(w * 0.6), h)],
                   fill=faint if i % 2 else dim, width=line)
    elif style == 2:                                  # horizon bands
        y = int(h * rng.uniform(0.40, 0.60))
        d.rectangle([0, y, w, y + line * 2], fill=dim)
        d.rectangle([0, y + line * 9, w, y + line * 10], fill=faint)
        d.rectangle([0, y - line * 7, w, y - line * 6], fill=faint)
    else:                                             # sparse grid
        step = int(w * 0.16)
        for x in range(step, w, step):
            d.line([(x, 0), (x, h)], fill=faint, width=line)
        for y in range(step, h, step):
            d.line([(0, y), (w, y)], fill=faint, width=line)
    return layer.filter(ImageFilter.GaussianBlur(0.8))


def _grain(img: Image.Image, rng: random.Random, amount: float) -> Image.Image:
    """Symmetric grain: brightens and darkens equally, so contrast survives.

    Done in numpy - the equivalent PIL point()/ImageChops chain at 1080p costs
    roughly 7x more time for an identical result.
    """
    import numpy as np

    w, h = img.size
    arr = np.asarray(img, dtype=np.int16)
    # Half-res noise, upscaled: coarser grain looks like film, not sensor noise.
    small = rng.randrange(1 << 30)
    gen = np.random.default_rng(small)
    noise_small = gen.normal(0.0, 26.0 * amount, (max(h // 2, 8), max(w // 2, 8)))
    noise = np.asarray(
        Image.fromarray((noise_small + 128).clip(0, 255).astype("uint8"))
        .resize((w, h), Image.BILINEAR), dtype=np.int16) - 128
    out = np.clip(arr + noise[:, :, None], 0, 255).astype("uint8")
    return Image.fromarray(out, "RGB")


def _ensure_exposure(img: Image.Image, floor: float = 30.0,
                     ceiling: float = 96.0) -> Image.Image:
    """Lift frames that came out almost black.

    The light positions are randomised, so some seeds put the key light near an
    edge and leave most of the frame near zero. A near-black frame reads as an
    encoding fault rather than a design choice, so pull the mean luma up to a
    floor (and pull very bright frames down, which would fight white captions).
    """
    from PIL import ImageStat

    mean = ImageStat.Stat(img.convert("L")).mean[0]
    if mean <= 0.5:
        factor = 4.0
    elif mean < floor:
        factor = floor / mean
    elif mean > ceiling:
        factor = ceiling / mean
    else:
        return img
    factor = min(max(factor, 0.55), 3.2)
    # Gamma-style lift preserves highlight roll-off better than a linear gain.
    lut = [min(255, int(((i / 255.0) ** (1.0 / factor)) * 255)) for i in range(256)]
    return img.point(lut * 3)


def _vignette(img: Image.Image, strength: float) -> Image.Image:
    w, h = img.size
    mask = Image.new("L", (110, 110), 0)
    ImageDraw.Draw(mask).ellipse([-14, -22, 124, 132], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(26)).resize((w, h), Image.BICUBIC)
    dark = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(img, Image.blend(img, dark, strength), mask)


class ProceduralProvider:
    """Always available. No network, no key, no quota, no licence risk."""

    name = "procedural"
    license_note = "generated-locally"

    def available(self) -> bool:
        return True

    def fetch(self, req: VisualRequest, out_path: Path) -> Asset:
        rng = random.Random(_seed_for(req))
        palettes = KID_PALETTES if req.made_for_kids else PALETTES
        pal_name, (deep, mid, bright, shadow) = palettes[rng.randrange(len(palettes))]

        # Render 18% larger than the frame so Ken Burns has room to move.
        w, h = int(req.width * 1.18), int(req.height * 1.18)
        size = (w, h)
        # Every smooth layer (gradient, lights, rays) is inherently blurry, so
        # it is composited at 1/3 scale and upscaled once. Visually identical,
        # roughly 4x faster than compositing each one at full resolution.
        small = (max(w // 3, 64), max(h // 3, 64))

        # 1. Base field: shadow -> mid, angled.
        img = _linear_gradient(small, shadow, mid, angle=rng.uniform(20, 160))

        # 2. Key light + one or two fill lights, screened on (additive).
        key = _radial_light(small, bright,
                            cx=rng.uniform(0.18, 0.82), cy=rng.uniform(0.16, 0.46),
                            radius=rng.uniform(0.42, 0.72),
                            intensity=rng.uniform(0.62, 0.92))
        img = ImageChops.screen(img, key)
        for _ in range(rng.randint(1, 2)):
            fill_color = mid if rng.random() < 0.5 else bright
            fill = _radial_light(small, fill_color,
                                 cx=rng.uniform(-0.1, 1.1), cy=rng.uniform(0.35, 1.05),
                                 radius=rng.uniform(0.3, 0.6),
                                 intensity=rng.uniform(0.3, 0.55))
            img = ImageChops.screen(img, fill)

        # 3. Soft light shafts still belong to the smooth pass.
        if rng.random() < 0.55:
            img = ImageChops.screen(img, _rays(small, rng, bright))

        img = img.resize(size, Image.BICUBIC)

        # 4. Detail layers must be full resolution or they turn to mush.
        if rng.random() < 0.7 or pal_name == "deep_space":
            density = int(w * h / (2600 if pal_name == "deep_space" else 7000))
            img = ImageChops.screen(img, _particles(size, rng, bright, density))

        # 5. Structure, grain, vignette, final sharpen.
        img = ImageChops.screen(img, _accent_geometry(size, rng, bright))
        # Grain is aesthetic, but it is also random noise: x264 cannot compress
        # it, so a heavy setting inflates both bitrate and encode time
        # dramatically (measured: 0.30 produced ~6.4 Mbps and a 9-minute encode
        # for a 45s Short). 0.12 keeps the texture at a fraction of the cost.
        img = _ensure_exposure(img)
        img = _grain(img, rng, amount=0.12 if not req.made_for_kids else 0.08)
        img = _vignette(img, strength=0.60 if not req.made_for_kids else 0.40)
        img = img.filter(ImageFilter.UnsharpMask(radius=1.4, percent=60, threshold=3))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "JPEG", quality=94, subsampling=1, optimize=True)
        return Asset(asset=out_path.name, source="procedural",
                     license="generated-locally (no third-party rights)",
                     prompt=f"{pal_name} abstract field", scene_index=req.scene_index)
