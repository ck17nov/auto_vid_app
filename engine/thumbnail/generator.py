"""Thumbnail generation (spec section 16).

Generates 3 variants, scores each, picks the best. Principles enforced in code:
one clear subject, high contrast, very little text, no fake claims.

For Shorts a thumbnail matters far less than the first frame, so for SHORT the
generator uses the video's own opening frame as the base (which is what viewers
actually see in feeds and on the channel grid) and keeps text minimal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from ..core.config import Config
from ..core.logging import log_event
from ..core.util import clamp, ffmpeg_bin, run, words
from ..video.fonts import display_font

# YouTube thumbnail spec: 1280x720, under 2 MB, JPG/PNG.
THUMB_W, THUMB_H = 1280, 720
MAX_BYTES = 2 * 1024 * 1024

# Filler that never earns thumbnail space. Deliberately KEEPS the curiosity
# words (why / what / how / who) - those carry the hook.
SKIP_WORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "with", "and", "or",
    "is", "are", "was", "were", "that", "this", "it", "its", "at", "by",
    "from", "you", "your", "we", "our", "they", "their", "as", "but",
    "do", "does", "did", "so", "be", "been", "being", "will", "would",
    "can", "could", "should", "just", "very", "really", "get", "got",
    "than", "then", "when", "while", "into", "about", "over", "after",
    "before", "if", "no", "not", "all", "more", "most", "some", "any",
    "there", "here", "up", "out", "down", "off", "has", "have", "had",
}


@dataclass
class ThumbnailVariant:
    path: Path
    style: str
    score: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    text: str = ""


def _headline(title: str, max_words: int = 4) -> str:
    """Reduce a title to a few words that still read as a phrase.

    Picking the "most specific" words independently produces word salad
    ("The First Light Of A Dying Star Was Finally Caught" -> "FIRST FINALLY
    CAUGHT").  Taking the longest CONTIGUOUS run of content words preserves
    meaning: the same title yields "FIRST LIGHT".
    """
    raw = re.sub(r"[^\w\s'-]", " ", title or "").split()
    if not raw:
        return ""

    # Split into runs of consecutive content words.
    runs: list[list[str]] = []
    current: list[str] = []
    for word in raw:
        if word.lower() in SKIP_WORDS:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(word)
    if current:
        runs.append(current)
    if not runs:
        return " ".join(raw[:max_words]).upper()

    # Longest run wins; earliest run breaks ties (front-loaded titles read best).
    best_index = max(range(len(runs)), key=lambda i: (len(runs[i]), -i))
    best = runs[best_index]

    # A single-word run cannot be extended with the NEXT run: those words are
    # not adjacent in the title, so joining them invents a phrase that was never
    # written ("The Truth About Animals" -> "TRUTH ANIMALS"). Use the single
    # most specific word instead - a one-word thumbnail is normal.
    if len(best) < 2:
        best = [max((w for run in runs for w in run), key=len)]

    best = best[:max_words]
    # Never end on a dangling qualifier. "What Space Actually Does" truncated to
    # three words gave "WHAT SPACE ACTUALLY", which reads as a cut-off sentence;
    # dropping the trailing adverb gives the cleaner "WHAT SPACE".
    while len(best) > 1 and (_is_qualifier(best[-1]) or _is_weak_ending(best[-1])):
        best = best[:-1]

    return " ".join(best).upper()


# Adverbs and intensifiers that must not be the last word of a headline.
_QUALIFIERS = {
    "actually", "really", "truly", "very", "quite", "rather", "almost",
    "nearly", "simply", "merely", "hardly", "barely", "totally", "utterly",
    "completely", "absolutely", "definitely", "probably", "possibly",
    "apparently", "basically", "essentially", "literally", "seriously",
    "finally", "eventually", "suddenly", "recently", "currently",
}


# Determiners and pronouns that leave a headline hanging mid-thought.
_WEAK_ENDINGS = {
    "nobody", "everyone", "someone", "anyone", "everybody", "anybody",
    "most", "every", "each", "both", "either", "neither", "another",
    "such", "same", "own", "other", "others",
}


def _is_weak_ending(word: str) -> bool:
    return word.lower().strip(".,!?'\"") in _WEAK_ENDINGS


def _is_qualifier(word: str) -> bool:
    lowered = word.lower().strip(".,!?'\"")
    if lowered in _QUALIFIERS:
        return True
    # Most -ly words are adverbs; keep short exceptions like "only"/"early".
    return lowered.endswith("ly") and len(lowered) > 6


def _fit_font(font_path: Path, text: str, max_w: int, max_h: int,
              start: int = 150) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest font size that fits `text` in at most 2 lines."""
    wordlist = text.split()
    for size in range(start, 34, -4):
        font = ImageFont.truetype(str(font_path), size)
        for split in range(len(wordlist), 0, -1):
            lines = [" ".join(wordlist[:split]), " ".join(wordlist[split:])]
            lines = [ln for ln in lines if ln]
            widest = max(font.getbbox(ln)[2] - font.getbbox(ln)[0] for ln in lines)
            height = sum(font.getbbox(ln)[3] - font.getbbox(ln)[1] + size * 0.22
                         for ln in lines)
            if widest <= max_w and height <= max_h:
                return font, lines
    font = ImageFont.truetype(str(font_path), 38)
    return font, [text]


def _draw_text_block(img: Image.Image, lines: list[str],
                     font: ImageFont.FreeTypeFont, *, anchor: str = "bottom",
                     accent: tuple[int, int, int] = (255, 210, 40),
                     accent_word: int = -1) -> None:
    draw = ImageDraw.Draw(img)
    w, h = img.size
    line_heights = [font.getbbox(ln)[3] - font.getbbox(ln)[1] for ln in lines]
    gap = int(font.size * 0.20)
    block_h = sum(line_heights) + gap * (len(lines) - 1)

    if anchor == "bottom":
        y = h - int(h * 0.09) - block_h
    elif anchor == "center":
        y = (h - block_h) // 2
    else:
        y = int(h * 0.09)

    word_index = 0
    for line, lh in zip(lines, line_heights):
        tokens = line.split()
        widths = [font.getbbox(t + " ")[2] - font.getbbox(t + " ")[0] for t in tokens]
        total_w = sum(widths)
        x = (w - total_w) // 2
        for token, tw in zip(tokens, widths):
            color = accent if word_index == accent_word else (255, 255, 255)
            # Heavy outline keeps text readable over any image.
            stroke = max(4, int(font.size * 0.075))
            draw.text((x, y), token, font=font, fill=color,
                      stroke_width=stroke, stroke_fill=(8, 8, 10))
            x += tw
            word_index += 1
        y += lh + gap


def _base_from_video(video: Path, out: Path, at_seconds: float = 0.6) -> Path | None:
    """Grab a frame from the finished video as the thumbnail base."""
    try:
        run([ffmpeg_bin(), "-y", "-loglevel", "error", "-ss", f"{at_seconds:.2f}",
             "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)],
            timeout=180)
        return out if out.exists() and out.stat().st_size > 2000 else None
    except Exception as exc:
        log_event("THUMBNAIL", "frame grab failed", error=str(exc)[:140])
        return None


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    src_ratio = img.width / img.height
    dst_ratio = w / h
    if src_ratio > dst_ratio:
        new_w = int(img.height * dst_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    elif src_ratio < dst_ratio:
        new_h = int(img.width / dst_ratio)
        top = int((img.height - new_h) * 0.32)
        img = img.crop((0, top, img.width, top + new_h))
    return img.resize((w, h), Image.LANCZOS)


class ThumbnailGenerator:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------------
    def generate(self, *, title: str, out_dir: Path,
                 source_image: Path | None = None,
                 video: Path | None = None,
                 video_format: str = "SHORT",
                 made_for_kids: bool = False,
                 variants: int = 3) -> tuple[Path, list[ThumbnailVariant]]:
        out_dir.mkdir(parents=True, exist_ok=True)
        font_path, _ = display_font()
        headline = _headline(title, max_words=3 if video_format == "SHORT" else 4)

        base_path: Path | None = None
        if video is not None and video.exists():
            base_path = _base_from_video(video, out_dir / "thumb_base.jpg")
        if base_path is None and source_image is not None and source_image.exists():
            base_path = source_image
        if base_path is None:
            raise RuntimeError("thumbnail needs either a rendered video or a "
                               "source image")

        styles = ["bold_bottom", "split_focus", "minimal_center"][:max(1, variants)]
        built: list[ThumbnailVariant] = []
        for i, style in enumerate(styles):
            target = out_dir / f"thumbnail_{i + 1}.jpg"
            img = self._render(base_path, headline, style, font_path,
                               made_for_kids=made_for_kids)
            self._save(img, target)
            variant = ThumbnailVariant(path=target, style=style, text=headline)
            variant.score, variant.metrics = self.score(img, headline, title)
            built.append(variant)

        built.sort(key=lambda v: v.score, reverse=True)
        best = built[0]
        final = out_dir / "thumbnail.jpg"
        final.write_bytes(best.path.read_bytes())
        log_event("THUMBNAIL", "variants generated", count=len(built),
                  best=best.style, score=f"{best.score:.0f}/100")
        return final, built

    # ------------------------------------------------------------------
    def _render(self, base_path: Path, headline: str, style: str,
                font_path: Path, *, made_for_kids: bool) -> Image.Image:
        with Image.open(base_path) as raw:
            img = _cover(raw.convert("RGB"), THUMB_W, THUMB_H)

        # Global punch: contrast + saturation, gentler for kids content.
        img = ImageEnhance.Contrast(img).enhance(1.10 if made_for_kids else 1.20)
        img = ImageEnhance.Color(img).enhance(1.10 if made_for_kids else 1.22)
        img = img.filter(ImageFilter.UnsharpMask(radius=2.0, percent=85, threshold=3))

        draw = ImageDraw.Draw(img, "RGBA")
        accent = (255, 226, 120) if made_for_kids else (255, 209, 46)

        if style == "bold_bottom":
            # Gradient scrim so text is legible over any image.
            for i in range(int(THUMB_H * 0.46)):
                y = THUMB_H - i
                alpha = int(215 * (i / (THUMB_H * 0.46)) ** 0.85)
                draw.line([(0, y), (THUMB_W, y)], fill=(0, 0, 0, alpha))
            font, lines = _fit_font(font_path, headline,
                                    int(THUMB_W * 0.90), int(THUMB_H * 0.34))
            _draw_text_block(img, lines, font, anchor="bottom", accent=accent,
                             accent_word=len(headline.split()) - 1)

        elif style == "split_focus":
            # Darken the left third, text stacked there, subject stays visible.
            draw.rectangle([0, 0, int(THUMB_W * 0.52), THUMB_H],
                           fill=(0, 0, 0, 150))
            font, lines = _fit_font(font_path, headline,
                                    int(THUMB_W * 0.46), int(THUMB_H * 0.62))
            sub = Image.new("RGB", (int(THUMB_W * 0.52), THUMB_H))
            sub.paste(img.crop((0, 0, int(THUMB_W * 0.52), THUMB_H)))
            _draw_text_block(sub, lines, font, anchor="center", accent=accent,
                             accent_word=0)
            img.paste(sub, (0, 0))
            draw = ImageDraw.Draw(img, "RGBA")
            draw.rectangle([int(THUMB_W * 0.52) - 6, 0,
                            int(THUMB_W * 0.52), THUMB_H], fill=(*accent, 220))

        else:  # minimal_center
            draw.rectangle([0, 0, THUMB_W, THUMB_H], fill=(0, 0, 0, 88))
            font, lines = _fit_font(font_path, headline,
                                    int(THUMB_W * 0.80), int(THUMB_H * 0.40))
            _draw_text_block(img, lines, font, anchor="center", accent=accent,
                             accent_word=-1)

        return img

    def _save(self, img: Image.Image, target: Path) -> Path:
        quality = 92
        while quality >= 60:
            img.save(target, "JPEG", quality=quality, optimize=True,
                     subsampling=1, progressive=True)
            if target.stat().st_size <= MAX_BYTES:
                return target
            quality -= 8
        return target

    # ------------------------------------------------------------------
    def score(self, img: Image.Image, headline: str,
              title: str) -> tuple[float, dict[str, Any]]:
        """Score a variant on the spec's thumbnail principles."""
        from PIL import ImageStat

        grey = img.convert("L")
        stat = ImageStat.Stat(grey)
        mean, stddev = stat.mean[0], stat.stddev[0]

        # Contrast: want strong local variation, mid-ish exposure.
        contrast = clamp(stddev / 62.0)
        exposure = clamp(1.0 - abs(mean - 118) / 118.0)

        # Text economy: 1-4 words is ideal, more is clutter.
        word_count = len(headline.split())
        text_economy = clamp(1.0 - abs(word_count - 3) / 4.0)

        # Subject focus: is there a clear high-detail region (edge energy
        # concentrated rather than uniform)?
        edges = grey.filter(ImageFilter.FIND_EDGES)
        thirds = []
        w, h = edges.size
        for gx in range(3):
            for gy in range(3):
                cell = edges.crop((gx * w // 3, gy * h // 3,
                                   (gx + 1) * w // 3, (gy + 1) * h // 3))
                thirds.append(ImageStat.Stat(cell).mean[0])
        peak = max(thirds) or 1.0
        avg = sum(thirds) / len(thirds)
        subject_focus = clamp((peak - avg) / max(peak, 1.0) * 2.2)

        # Honesty: penalise overpromising words even if they came from the title.
        risky = {"aliens", "proof", "cure", "miracle", "guaranteed", "shocking",
                 "unbelievable", "insane"}
        overlap = set(words(title)) & risky
        honesty = clamp(1.0 - len(overlap) * 0.4)

        parts = {"contrast": contrast, "exposure": exposure,
                 "text_economy": text_economy, "subject_focus": subject_focus,
                 "honesty": honesty}
        weights = {"contrast": 0.26, "exposure": 0.16, "text_economy": 0.20,
                   "subject_focus": 0.22, "honesty": 0.16}
        score = sum(parts[k] * weights[k] for k in parts) * 100
        return round(score, 1), {**{k: round(v, 3) for k, v in parts.items()},
                                 "mean_luma": round(mean, 1),
                                 "stddev_luma": round(stddev, 1),
                                 "headline_words": word_count}
