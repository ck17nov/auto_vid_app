"""Purpose-built animated flashcards for children's educational content.

Why this exists: the visual providers all return a photograph, and the renderer
then pans across it. For "science", "nature" or "history" that is exactly
right - reality IS the subject. For a kids alphabet video it is wrong in a way
no tuning fixes: a keyword search for "ant leaf bubble" returns a macro photo of
a real ant, which is a fine nature clip and nothing like children's television.

What the top kids channels actually have is animation, recurring characters and
songs. This does not attempt that - it is motion graphics, not character
animation, and pretending otherwise would be dishonest. What it does give you is
something that looks DESIGNED rather than scraped: an animated flashcard with a
big bouncing letter, the subject photograph inside a rounded card, chunky
kid-legible typography, and a gently drifting pastel background.

It costs nothing, needs no key, works offline and has no rate limit. It renders
frames with Pillow and encodes them with ffmpeg, so it returns an .mp4 exactly
like the stock-video provider - the compose stage already knows what to do with
that (see `SceneTiming.is_video`).
"""
from __future__ import annotations

import math
import random
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from ..core.logging import log_event
from ..core.models import Asset
from ..core.util import STOPWORDS, ffmpeg_bin, words
from ..video.fonts import display_font
from .base import VisualRequest

# Bright, soft, high-contrast. Children's content leans saturated but not
# harsh, and captions have to stay readable on top of all of it.
# (base, light, accent, ink). `ink` is a separate DARK colour for text and
# outlines: the first entry is a mid tint used for the background wash, and
# drawing type in it produced mint-on-white "APPLE" that was unreadable.
PALETTES: list[tuple[str, tuple[str, str, str, str]]] = [
    ("sunshine",  ("#FFD166", "#FFF8E1", "#EF476F", "#5C3A00")),
    ("mint",      ("#8AE9C1", "#E9FFF7", "#3D5A80", "#0B3B2E")),
    ("bubblegum", ("#FFAFCC", "#FFEFF4", "#7B2CBF", "#4A0D3F")),
    ("sky",       ("#8ECAE6", "#ECF8FD", "#FB8500", "#023047")),
    ("citrus",    ("#B7E4C7", "#F4FBF5", "#E76F51", "#1B4332")),
    ("lavender",  ("#CDB4DB", "#F7F1FF", "#F72585", "#332B47")),
]

# Objects we can draw well enough to be recognisable, keyed by the words a
# script is likely to use. Anything not here falls back to the scene photo,
# which is why the list does not need to be exhaustive.
_DRAWABLE = {
    "sun": "sun", "star": "star", "moon": "moon", "cloud": "cloud",
    "apple": "apple", "ball": "ball", "balloon": "balloon",
    "heart": "heart", "flower": "flower", "fish": "fish",
    "tree": "tree", "house": "house", "egg": "egg", "leaf": "leaf",
}


# Vertical layout, as fractions of frame height. The bottom ~30% is left
# clear on purpose: the compose stage burns captions there and knows nothing
# about this artwork, so a card reaching into that band gets narration printed
# across it.
LETTER_Y = 0.12          # centre of the bouncing letter (alphabet mode)
CARD_TOP = 0.24          # top of the picture card WHEN a letter is shown
CARD_TOP_NO_LETTER = 0.15  # ... and when it is not, so the card can be bigger
WORD_Y = 0.615           # top of the word pill under the card

# Widest the card may be, as a fraction of frame width and height.
#
# The first version used min(w * 0.62, h * 0.30), where on a 1080x1920 Short
# the height term wins and the card comes out 576px - just over half the width,
# floating in an otherwise empty frame. On a phone it reads as a small square
# box rather than a picture. The card is the entire subject of the shot, so it
# should dominate: it now fills most of the width, and the height allowance is
# raised to match rather than to constrain.
CARD_MAX_W = 0.84
CARD_MAX_H = 0.44


def _hex(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _ease_out_back(t: float) -> float:
    """Overshoot-and-settle. The bounce is most of why this reads as 'kids'."""
    c1, c3 = 1.70158, 2.70158
    t = t - 1.0
    return 1.0 + c3 * t * t * t + c1 * t * t


def _ease_in_out(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def headline_letter(req: VisualRequest) -> str:
    """The letter or digit this scene is about, or "".

    Only unambiguous mentions count: "the letter A", "number 7", or a QUOTED
    single capital. A bare lone capital is not enough - that fallback matched
    the pronoun "I" in a scene about children laughing and put a giant I on
    screen, and it would match the article "A" just as happily. A wrong letter
    is worse than no letter, so anything uncertain returns "".
    """
    haystack = f"{req.prompt} {' '.join(req.keywords)}"
    quote = "\"'‘’“”"
    m = re.search(rf"\b(?:letter|alphabet)\s+[{quote}]?([A-Za-z])\b",
                  haystack, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(?:number|digit|numeral)\s+(\d{1,2})\b", haystack, re.I)
    if m:
        return m.group(1)
    m = re.search(rf"[{quote}]([A-Za-z])[{quote}]", req.prompt)
    return m.group(1).upper() if m else ""


def headline_word(req: VisualRequest) -> str:
    """A single short subject word to print under the card."""
    pool = [w for w in (req.keywords or []) if w and w.lower() not in STOPWORDS]
    if not pool:
        pool = [w for w in words(req.prompt) if w.lower() not in STOPWORDS]

    # Keywords arrive as phrases as well as single words ("letter b"), and a
    # two-word card reads badly - one scene printed "LETTER B" under the
    # picture. Split them, then drop the scaffolding words that describe the
    # lesson rather than the thing being shown.
    flat: list[str] = []
    for item in pool:
        flat.extend(str(item).split())
    skip = {"letter", "letters", "number", "numbers", "alphabet", "word",
            "words", "sound", "sounds", "learn", "learning", "kids", "child",
            "children"}
    pool = [w for w in flat
            if 2 <= len(w) <= 11 and w.lower() not in STOPWORDS
            and w.lower() not in skip]
    if not pool:
        return ""
    # Prefer something we can draw - a drawn apple beats a photo of a concept.
    for w in pool:
        if w.lower() in _DRAWABLE:
            return w.upper()
    return pool[0].upper()


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
def _rounded(draw: ImageDraw.ImageDraw, box, radius: int, **kw) -> None:
    draw.rounded_rectangle(box, radius=radius, **kw)


def _draw_object(draw: ImageDraw.ImageDraw, kind: str, box, accent, deep) -> None:
    """Simple recognisable shapes. Deliberately flat and bold."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx, cy = x0 + w / 2, y0 + h / 2

    if kind == "sun":
        for i in range(12):
            a = i * math.pi / 6
            draw.line([cx + math.cos(a) * w * 0.30, cy + math.sin(a) * h * 0.30,
                       cx + math.cos(a) * w * 0.46, cy + math.sin(a) * h * 0.46],
                      fill=accent, width=max(4, int(w // 26)))
        draw.ellipse([cx - w * 0.26, cy - h * 0.26, cx + w * 0.26, cy + h * 0.26],
                     fill=accent)
    elif kind == "star":
        pts = []
        for i in range(10):
            r = w * (0.46 if i % 2 == 0 else 0.20)
            a = -math.pi / 2 + i * math.pi / 5
            pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
        draw.polygon(pts, fill=accent)
    elif kind == "moon":
        draw.ellipse([cx - w * 0.40, cy - h * 0.40, cx + w * 0.40, cy + h * 0.40],
                     fill=accent)
        draw.ellipse([cx - w * 0.16, cy - h * 0.46, cx + w * 0.52, cy + h * 0.34],
                     fill=deep)
    elif kind == "cloud":
        for dx, dy, r in ((-0.22, 0.04, 0.20), (0.0, -0.06, 0.26), (0.22, 0.04, 0.20)):
            draw.ellipse([cx + dx * w - r * w, cy + dy * h - r * h,
                          cx + dx * w + r * w, cy + dy * h + r * h], fill=accent)
    elif kind in {"apple", "ball", "balloon", "egg"}:
        squash = 1.15 if kind == "egg" else 1.0
        draw.ellipse([cx - w * 0.34, cy - h * 0.34 * squash,
                      cx + w * 0.34, cy + h * 0.34 * squash], fill=accent)
        if kind == "apple":
            draw.line([cx, cy - h * 0.34, cx, cy - h * 0.46],
                      fill=deep, width=max(4, int(w // 24)))
            draw.ellipse([cx, cy - h * 0.50, cx + w * 0.18, cy - h * 0.34], fill=deep)
        if kind == "balloon":
            draw.line([cx, cy + h * 0.34, cx, cy + h * 0.50],
                      fill=deep, width=max(3, int(w // 34)))
    elif kind == "heart":
        draw.ellipse([cx - w * 0.34, cy - h * 0.30, cx, cy + h * 0.06], fill=accent)
        draw.ellipse([cx, cy - h * 0.30, cx + w * 0.34, cy + h * 0.06], fill=accent)
        draw.polygon([(cx - w * 0.33, cy - h * 0.04), (cx + w * 0.33, cy - h * 0.04),
                      (cx, cy + h * 0.40)], fill=accent)
    elif kind == "flower":
        for i in range(6):
            a = i * math.pi / 3
            px, py = cx + math.cos(a) * w * 0.22, cy + math.sin(a) * h * 0.22
            draw.ellipse([px - w * 0.15, py - h * 0.15, px + w * 0.15, py + h * 0.15],
                         fill=accent)
        draw.ellipse([cx - w * 0.12, cy - h * 0.12, cx + w * 0.12, cy + h * 0.12],
                     fill=deep)
    elif kind == "fish":
        draw.ellipse([cx - w * 0.36, cy - h * 0.20, cx + w * 0.20, cy + h * 0.20],
                     fill=accent)
        draw.polygon([(cx + w * 0.16, cy), (cx + w * 0.42, cy - h * 0.20),
                      (cx + w * 0.42, cy + h * 0.20)], fill=accent)
        draw.ellipse([cx - w * 0.24, cy - h * 0.06, cx - w * 0.16, cy + h * 0.02],
                     fill=deep)
    elif kind == "tree":
        draw.rectangle([cx - w * 0.06, cy, cx + w * 0.06, cy + h * 0.42], fill=deep)
        draw.ellipse([cx - w * 0.32, cy - h * 0.44, cx + w * 0.32, cy + h * 0.08],
                     fill=accent)
    elif kind == "house":
        draw.rectangle([cx - w * 0.28, cy - h * 0.04, cx + w * 0.28, cy + h * 0.36],
                       fill=accent)
        draw.polygon([(cx - w * 0.36, cy - h * 0.04), (cx + w * 0.36, cy - h * 0.04),
                      (cx, cy - h * 0.40)], fill=deep)
    elif kind == "leaf":
        draw.ellipse([cx - w * 0.30, cy - h * 0.18, cx + w * 0.30, cy + h * 0.18],
                     fill=accent)
        draw.line([cx - w * 0.28, cy, cx + w * 0.28, cy], fill=deep,
                  width=max(3, int(w // 40)))


def _background(size, pal, phase: float = 0.0) -> Image.Image:
    """A soft vertical wash with drifting blobs. Built ONCE per clip.

    Two costs were being paid per frame: a full-size LANCZOS upscale and a
    Gaussian blur of a 1080x1920 image. Rendering 4 seconds took 68 SECONDS,
    almost all of it here. Since the visible motion comes from the card, the
    letter and the word, a fixed background is indistinguishable in the result
    and roughly 20x faster overall.

    The wash itself is built at 1/8 scale and upsampled, which the blur makes
    visually identical to drawing it at full size.
    """
    w, h = size
    base, light, accent, _ink = (_hex(c) for c in pal)
    small = Image.new("RGB", (max(w // 8, 8), max(h // 8, 8)))
    sd = ImageDraw.Draw(small)
    sw, sh = small.size
    for y in range(sh):
        t = y / max(sh - 1, 1)
        sd.line([(0, y), (sw, y)], fill=tuple(
            int(light[i] + (base[i] - light[i]) * t * 0.55) for i in range(3)))
    for i in range(3):
        a = phase * 2 * math.pi + i * 2.1
        bx = sw * (0.5 + 0.30 * math.cos(a))
        by = sh * (0.5 + 0.34 * math.sin(a * 0.8))
        r = sw * (0.22 + 0.05 * math.sin(a * 1.3))
        sd.ellipse([bx - r, by - r, bx + r, by + r], fill=tuple(
            int(base[i2] * 0.35 + light[i2] * 0.65) for i2 in range(3)))
    return small.resize((w, h), Image.LANCZOS).filter(
        ImageFilter.GaussianBlur(radius=max(w // 90, 4)))


class KidsAnimationProvider:
    """Renders an animated flashcard clip instead of fetching a photo."""

    name = "kids_animation"
    license_note = "generated-locally (original motion graphics)"

    def __init__(self, fps: int = 30, subject_provider=None):
        self.fps = fps
        # Optional: something with .fetch() that supplies the subject photo.
        # Without it the card shows a drawn shape, which is fine and sometimes
        # better - a drawn apple is unambiguous where a photo may not be.
        self.subject_provider = subject_provider

    def available(self) -> bool:
        return True

    # ------------------------------------------------------------------
    def fetch(self, req: VisualRequest, out_path: Path) -> Asset:
        seconds = max(req.min_seconds or 3.0, 1.5)
        target = out_path.with_suffix(".mp4")
        rng = random.Random(req.seed or 7)
        pal_name, pal = PALETTES[rng.randrange(len(PALETTES))]

        letter = headline_letter(req)
        word = headline_word(req)
        drawable = _DRAWABLE.get(word.lower()) if word else None

        subject: Image.Image | None = None
        if self.subject_provider is not None and not drawable:
            subject = self._subject_photo(req, out_path)

        frames = max(int(round(seconds * self.fps)), 2)
        font_path, _family = display_font()

        # Frames are piped to ffmpeg as raw RGB rather than written out as
        # PNGs. Encoding 120 PNGs at 1080x1920 and reading them back was the
        # single largest remaining cost - the pixels are thrown away by the
        # H.264 encode anyway, so compressing them first is pure waste.
        target.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [ffmpeg_bin(), "-y", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{req.width}x{req.height}",
             "-framerate", str(self.fps), "-i", "-",
             "-frames:v", str(frames),
             "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-an", str(target)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        try:
            for frame in self._frames(frames, req, pal, letter, word, drawable,
                                      subject, font_path):
                proc.stdin.write(frame.tobytes())
            proc.stdin.close()
        except BrokenPipeError:
            pass                    # ffmpeg died; the returncode check reports it
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise RuntimeError(f"kids animation encode failed: {err[:200]}")

        log_event("VISUAL", "kids flashcard animated", scene=req.scene_index,
                  letter=letter or "-", word=word or "-", palette=pal_name,
                  drawn=bool(drawable), photo=subject is not None,
                  seconds=f"{seconds:.1f}")
        return Asset(asset=target.name, source="generated:kids_animation",
                     license=self.license_note,
                     prompt=f"{letter} {word}".strip(),
                     scene_index=req.scene_index)

    # ------------------------------------------------------------------
    def _subject_photo(self, req: VisualRequest, out_path: Path):
        """Fetch a still for the card. Never fatal - the card works without."""
        try:
            still = out_path.with_name(f"subject_{req.scene_index:02d}.jpg")
            self.subject_provider.fetch(req, still)
            img = Image.open(still).convert("RGB")
            return img
        except Exception as exc:
            log_event("VISUAL", "kids card falling back to a drawn shape",
                      scene=req.scene_index, reason=str(exc)[:110])
            return None

    # ------------------------------------------------------------------
    def _frames(self, frames: int, req: VisualRequest,
                pal, letter: str, word: str, drawable, subject,
                font_path: Path):
        """Yield each frame as an RGB image, for piping straight to ffmpeg."""
        w, h = req.width, req.height
        base, light, accent, ink = (_hex(c) for c in pal)
        letter_size = int(h * 0.20)
        word_size = int(h * 0.055)
        f_letter = ImageFont.truetype(str(font_path), letter_size)
        f_word = ImageFont.truetype(str(font_path), word_size)

        # Layout leaves the LOWER THIRD empty for the burnt-in captions.
        # First attempt centred the card at 40-74% of the height and the
        # narration caption landed inside the photograph - "feels bold and"
        # printed across a sunrise. Captions are added later by the compose
        # stage and know nothing about this artwork, so the artwork has to
        # yield the space.
        card = int(min(w * CARD_MAX_W, h * CARD_MAX_H))
        # Alphabet mode keeps the letter above the card, so the card starts
        # lower. A story has no letter, so it reclaims that space.
        card_top = int(h * (CARD_TOP if letter else CARD_TOP_NO_LETTER))
        card_box_base = (int(w / 2 - card / 2), card_top,
                         int(w / 2 + card / 2), card_top + card)

        subject_fitted = None
        if subject is not None:
            side = card - int(card * 0.10)
            sub = subject.copy()
            scale = max(side / sub.width, side / sub.height)
            sub = sub.resize((max(int(sub.width * scale), side),
                              max(int(sub.height * scale), side)), Image.LANCZOS)
            left = (sub.width - side) // 2
            top = (sub.height - side) // 2
            subject_fitted = sub.crop((left, top, left + side, top + side))
            mask = Image.new("L", (side, side), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                [0, 0, side - 1, side - 1], radius=int(side * 0.12), fill=255)
            subject_fitted.putalpha(mask)

        backdrop = _background((w, h), pal)
        for i in range(frames):
            t = i / max(frames - 1, 1)
            img = backdrop.copy()
            draw = ImageDraw.Draw(img)

            # Card scales in with an overshoot, then breathes very slightly.
            intro = min(t / 0.28, 1.0)
            # Starts at 0.90, not 0.55.
            #
            # A pop from just over half size means that for the first third of
            # a second of every scene the card really is a small box, and on a
            # four-second cut that is a lot of the shot. The overshoot still
            # reads as a pop without the card ever looking tiny.
            scale = 0.90 + 0.10 * _ease_out_back(intro)
            breathe = 1.0 + 0.012 * math.sin(t * 2 * math.pi * 1.4)
            s = scale * breathe
            cx = (card_box_base[0] + card_box_base[2]) / 2
            cy = (card_box_base[1] + card_box_base[3]) / 2
            half = card * s / 2
            box = (cx - half, cy - half, cx + half, cy + half)

            # Drop shadow, then the card, then the contents.
            _rounded(draw, (box[0] + 10, box[1] + 14, box[2] + 10, box[3] + 14),
                     radius=int(card * 0.12), fill=tuple(
                         int(light[c] * 0.55 + ink[c] * 0.45) for c in range(3)))
            _rounded(draw, box, radius=int(card * 0.12), fill=(255, 255, 255))

            inner = int(card * s * 0.05)
            content = (box[0] + inner, box[1] + inner, box[2] - inner, box[3] - inner)
            if subject_fitted is not None:
                side = int(content[2] - content[0])
                if side > 4:
                    thumb = subject_fitted.resize((side, side), Image.LANCZOS)
                    img.paste(thumb, (int(content[0]), int(content[1])), thumb)
            elif drawable:
                _draw_object(draw, drawable, content, accent, ink)
            else:
                _draw_object(draw, "star", content, accent, ink)

            # The letter bounces above the card on its own rhythm.
            if letter:
                bob = math.sin(t * 2 * math.pi * 1.15) * h * 0.012
                pop = 0.75 + 0.25 * _ease_out_back(min(t / 0.22, 1.0))
                size = max(int(letter_size * pop), 8)
                fl = (f_letter if size == letter_size
                      else ImageFont.truetype(str(font_path), size))
                bbox = draw.textbbox((0, 0), letter, font=fl)
                lx = w / 2 - (bbox[2] - bbox[0]) / 2 - bbox[0]
                ly = h * LETTER_Y - (bbox[3] - bbox[1]) / 2 - bbox[1] + bob
                # Thick outline: kids content is watched on small bright
                # screens, often in daylight.
                for ox in range(-6, 7, 3):
                    for oy in range(-6, 7, 3):
                        if ox or oy:
                            draw.text((lx + ox, ly + oy), letter, font=fl,
                                      fill=ink)
                draw.text((lx, ly), letter, font=fl, fill=accent)

            if word:
                appear = _ease_in_out(min(max((t - 0.18) / 0.25, 0.0), 1.0))
                if appear > 0.02:
                    bbox = draw.textbbox((0, 0), word, font=f_word)
                    wx = w / 2 - (bbox[2] - bbox[0]) / 2 - bbox[0]
                    wy = h * WORD_Y - bbox[1] + (1.0 - appear) * h * 0.03
                    pill = (wx - word_size * 0.5, wy - word_size * 0.22,
                            wx + (bbox[2] - bbox[0]) + word_size * 0.5,
                            wy + (bbox[3] - bbox[1]) + word_size * 0.40)
                    _rounded(draw, pill, radius=int(word_size * 0.55),
                             fill=(255, 255, 255))
                    draw.text((wx, wy), word, font=f_word, fill=ink)

            yield img
