"""Network visual providers: stock photography first, AI generation second.

Why this order (quality-driven):
  Pexels / Pixabay return real photographs at 3000-6000 px.  Cropped to
  1080x1920 they are genuinely sharp and hold up under Ken Burns zoom.
  Pollinations (free, keyless) caps output near 576x1024, which needs ~2x
  upscaling and looks soft by comparison - so it is used for concepts stock
  libraries cannot cover, not as the default.

Licensing: Pexels and Pixabay licences both permit commercial use including
monetised YouTube video, with no attribution required (attribution is still
recorded in the AssetManifest).  See docs/SERVICE_COSTS.md.
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

import httpx

from ..core.logging import log_event
from ..core.models import Asset
from ..core.util import STOPWORDS, words
from .base import VisualRequest, condition_image, is_valid_image

_UA = {"User-Agent": "AutoTubeAI/0.1 (+https://github.com/local/autotube-ai)"}


def _search_terms(req: VisualRequest, limit: int = 4) -> str:
    """Stock search works on nouns, not on a full cinematic prompt."""
    picked: list[str] = []
    for kw in req.keywords:
        for w in words(kw):
            if w not in STOPWORDS and len(w) > 2 and w not in picked:
                picked.append(w)
    if not picked:
        picked = [w for w in words(req.prompt)
                  if w not in STOPWORDS and len(w) > 3][:limit]
    return " ".join(picked[:limit]) or "abstract background"


class PexelsProvider:
    name = "pexels"
    license_note = "Pexels License - free commercial use, no attribution required"

    def __init__(self, api_key: str = "", timeout: int = 40):
        self.api_key = api_key
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def fetch(self, req: VisualRequest, out_path: Path) -> Asset:
        query = _search_terms(req)
        orientation = "portrait" if req.height > req.width else "landscape"
        with httpx.Client(timeout=self.timeout, headers={
                **_UA, "Authorization": self.api_key}) as client:
            resp = client.get("https://api.pexels.com/v1/search", params={
                "query": query, "orientation": orientation,
                "per_page": 15, "size": "large"})
            resp.raise_for_status()
            photos = (resp.json() or {}).get("photos", [])
            if not photos:
                raise RuntimeError(f"pexels: no results for '{query}'")
            # Deterministic pick so re-runs are reproducible.
            photo = photos[req.seed % len(photos)] if req.seed else photos[0]
            src = photo.get("src", {})
            url = src.get("original") or src.get("large2x") or src.get("large")
            if not url:
                raise RuntimeError("pexels: result had no usable image url")
            img = client.get(url, follow_redirects=True)
            img.raise_for_status()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(img.content)

        if not is_valid_image(out_path):
            raise RuntimeError("pexels: downloaded file is not a valid image")
        condition_image(out_path, req.width, req.height)
        return Asset(asset=out_path.name, source="provider:pexels",
                     license=self.license_note, prompt=query,
                     attribution=f"Photo by {photo.get('photographer', 'unknown')} on Pexels",
                     url=photo.get("url", ""), scene_index=req.scene_index)


class PixabayProvider:
    name = "pixabay"
    license_note = "Pixabay Content License - free commercial use, no attribution required"

    def __init__(self, api_key: str = "", timeout: int = 40):
        self.api_key = api_key
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def fetch(self, req: VisualRequest, out_path: Path) -> Asset:
        query = _search_terms(req)
        params = {
            "key": self.api_key, "q": query, "image_type": "photo",
            "orientation": "vertical" if req.height > req.width else "horizontal",
            "per_page": 20, "safesearch": "true", "order": "popular",
            "min_width": 1200,
        }
        with httpx.Client(timeout=self.timeout, headers=_UA) as client:
            resp = client.get("https://pixabay.com/api/", params=params)
            resp.raise_for_status()
            hits = (resp.json() or {}).get("hits", [])
            if not hits:
                raise RuntimeError(f"pixabay: no results for '{query}'")
            hit = hits[req.seed % len(hits)] if req.seed else hits[0]
            url = hit.get("largeImageURL") or hit.get("webformatURL")
            if not url:
                raise RuntimeError("pixabay: result had no usable image url")
            img = client.get(url, follow_redirects=True)
            img.raise_for_status()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(img.content)

        if not is_valid_image(out_path):
            raise RuntimeError("pixabay: downloaded file is not a valid image")
        condition_image(out_path, req.width, req.height)
        return Asset(asset=out_path.name, source="provider:pixabay",
                     license=self.license_note, prompt=query,
                     attribution=f"Image by {hit.get('user', 'unknown')} on Pixabay",
                     url=hit.get("pageURL", ""), scene_index=req.scene_index)


class PollinationsProvider:
    """Free, keyless text-to-image.

    Reality check (spec section 41): no account needed, but it is rate limited,
    sometimes slow (8-60 s), and caps resolution near 576x1024.  Treated as a
    concept generator, not a high-resolution source.
    """

    name = "pollinations"
    license_note = "AI-generated (Pollinations, flux) - no third-party rights claimed"

    def __init__(self, timeout: int = 110, model: str = "flux"):
        self.timeout = timeout
        self.model = model

    def available(self) -> bool:
        return True

    def fetch(self, req: VisualRequest, out_path: Path) -> Asset:
        prompt = req.prompt.strip()
        if req.style:
            prompt = f"{prompt}, {req.style}"
        # Steer away from artefacts that ruin a Short.
        prompt += (", no text, no watermark, no letters, no logo, "
                   "highly detailed, sharp focus, professional photography")
        if req.made_for_kids:
            prompt += ", friendly, gentle, child-safe, no scary elements"

        # Ask for the target aspect; the service picks its own capped size.
        url = ("https://image.pollinations.ai/prompt/"
               + urllib.parse.quote(prompt, safe="")
               + f"?width={req.width}&height={req.height}&nologo=true"
               + f"&model={self.model}&seed={req.seed or req.scene_index + 1}")
        with httpx.Client(timeout=self.timeout, headers=_UA,
                          follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            if "image" not in resp.headers.get("content-type", ""):
                raise RuntimeError("pollinations: response was not an image")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(resp.content)

        if not is_valid_image(out_path):
            raise RuntimeError("pollinations: downloaded file is not a valid image")
        condition_image(out_path, req.width, req.height, sharpen=True)
        log_event("VISUAL", "AI image generated", scene=req.scene_index,
                  model=self.model)
        return Asset(asset=out_path.name, source="generated:pollinations",
                     license=self.license_note, prompt=prompt[:400],
                     scene_index=req.scene_index)
