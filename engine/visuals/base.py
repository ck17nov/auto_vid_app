"""Visual provider contract + image conditioning.

Every asset that enters the pipeline carries source and licence information
(spec section 13).  Nothing is ever taken from another creator's video.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageFilter

from ..core.models import Asset


@dataclass
class VisualRequest:
    scene_index: int
    prompt: str
    keywords: list[str]
    width: int = 1080
    height: int = 1920
    style: str = ""
    seed: int = 0
    made_for_kids: bool = False


class VisualProvider(Protocol):
    name: str
    license_note: str

    def available(self) -> bool: ...

    def fetch(self, req: VisualRequest, out_path: Path) -> Asset: ...


# --------------------------------------------------------------------------
# Conditioning: cover-crop to the exact frame, then sharpen.
# --------------------------------------------------------------------------
def condition_image(path: Path, width: int, height: int, *,
                    sharpen: bool = True) -> Path:
    """Make any downloaded/generated image render-ready.

    Providers return arbitrary sizes (Pollinations caps around 576x1024, stock
    photos can be 6000px wide).  We cover-crop to the target aspect so nothing
    is letterboxed, upscale with LANCZOS, then unsharp-mask.  The sharpening
    matters: an AI image upscaled ~2x looks soft once Ken Burns zoom is applied
    on top of it.
    """
    with Image.open(path) as raw:
        img = raw.convert("RGB")
        src_w, src_h = img.size
        target_ratio = width / height
        src_ratio = src_w / src_h

        # Cover crop
        if src_ratio > target_ratio:
            new_w = int(src_h * target_ratio)
            left = (src_w - new_w) // 2
            img = img.crop((left, 0, left + new_w, src_h))
        elif src_ratio < target_ratio:
            new_h = int(src_w / target_ratio)
            # Bias slightly above centre: subjects usually sit in the upper half.
            top = int((src_h - new_h) * 0.38)
            img = img.crop((0, top, src_w, top + new_h))

        upscale_factor = width / img.size[0]
        # Render at 1.18x the frame so Ken Burns zoom has real pixels to pan into.
        render_w, render_h = int(width * 1.18), int(height * 1.18)
        img = img.resize((render_w, render_h), Image.LANCZOS)

        if sharpen and upscale_factor > 1.05:
            strength = min(180, int(90 * upscale_factor))
            img = img.filter(ImageFilter.UnsharpMask(radius=1.6, percent=strength, threshold=3))
        elif sharpen:
            img = img.filter(ImageFilter.UnsharpMask(radius=1.1, percent=60, threshold=3))

        img.save(path, "JPEG", quality=94, subsampling=1, optimize=True)
    return path


def is_valid_image(path: Path, min_bytes: int = 4000) -> bool:
    try:
        if not path.exists() or path.stat().st_size < min_bytes:
            return False
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False
