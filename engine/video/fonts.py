"""Font resolution for captions and thumbnails.

libass resolves fonts through fontconfig, which is unreliable across platforms.
We therefore always render with an explicit font FILE inside assets/fonts/ and
pass that directory to ffmpeg via `fontsdir`, so the same project produces the
same frames on Windows, Linux and CI.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..core.logging import log_event

FONT_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"

# Free, redistributable display faces (SIL Open Font License 1.1).
DOWNLOADABLE = {
    "Anton": "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
    "Oswald": "https://github.com/google/fonts/raw/main/ofl/oswald/Oswald%5Bwght%5D.ttf",
}

# System fallbacks, in preference order per platform.
SYSTEM_CANDIDATES = [
    Path("C:/Windows/Fonts/ariblk.ttf"),      # Arial Black
    Path("C:/Windows/Fonts/impact.ttf"),
    Path("C:/Windows/Fonts/seguibl.ttf"),     # Segoe UI Black
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Impact.ttf"),
]

# Family name that libass should use, keyed by the file we ship.
FAMILY_FOR_FILE = {
    "Anton.ttf": "Anton",
    "Oswald.ttf": "Oswald",
    "ariblk.ttf": "Arial Black",
    "impact.ttf": "Impact",
    "seguibl.ttf": "Segoe UI Black",
    "DejaVuSans-Bold.ttf": "DejaVu Sans",
    "LiberationSans-Bold.ttf": "Liberation Sans",
    "MontserratBlack.ttf": "Montserrat",
}


def ensure_font_dir() -> Path:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    return FONT_DIR


def _try_download(name: str) -> Path | None:
    url = DOWNLOADABLE.get(name)
    if not url:
        return None
    try:
        import httpx
        resp = httpx.get(url, timeout=45, follow_redirects=True)
        if resp.status_code == 200 and len(resp.content) > 20000:
            target = ensure_font_dir() / f"{name}.ttf"
            target.write_bytes(resp.content)
            log_event("FONT", "downloaded display font", font=name,
                      license="SIL OFL 1.1")
            return target
    except Exception as exc:
        log_event("FONT", "font download failed", font=name, error=str(exc)[:120])
    return None


def display_font(preferred: str = "Anton") -> tuple[Path, str]:
    """Return (font_file, family_name) for caption rendering.

    Resolution order:
      1. `preferred` already present in assets/fonts/
      2. any font already present in assets/fonts/
      3. download a free OFL face
      4. copy a system font into assets/fonts/ (keeps `fontsdir` self-contained)
    """
    ensure_font_dir()

    direct = FONT_DIR / f"{preferred}.ttf"
    if direct.exists():
        return direct, FAMILY_FOR_FILE.get(direct.name, preferred)

    existing = sorted(list(FONT_DIR.glob("*.ttf")) + list(FONT_DIR.glob("*.otf")))
    preferred_order = ["Anton.ttf", "ariblk.ttf", "impact.ttf", "Oswald.ttf"]
    for wanted in preferred_order:
        for f in existing:
            if f.name == wanted:
                return f, FAMILY_FOR_FILE.get(f.name, f.stem)

    downloaded = _try_download(preferred) or _try_download("Anton")
    if downloaded:
        return downloaded, FAMILY_FOR_FILE.get(downloaded.name, preferred)

    for candidate in SYSTEM_CANDIDATES:
        if candidate.exists():
            target = FONT_DIR / candidate.name
            if not target.exists():
                shutil.copy2(candidate, target)
            return target, FAMILY_FOR_FILE.get(candidate.name, candidate.stem)

    if existing:
        return existing[0], FAMILY_FOR_FILE.get(existing[0].name, existing[0].stem)

    raise RuntimeError(
        "no usable font found. Put a .ttf in assets/fonts/ - see docs/SETUP.md")


def body_font() -> tuple[Path, str]:
    """A lighter face for thumbnail sub-text; falls back to the display font."""
    for name in ("MontserratBlack.ttf", "Oswald.ttf"):
        p = FONT_DIR / name
        if p.exists():
            return p, FAMILY_FOR_FILE.get(name, p.stem)
    return display_font()
