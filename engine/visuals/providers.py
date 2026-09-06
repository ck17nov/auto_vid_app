"""Network visual providers: stock photography first, AI generation second.

Why this order (quality-driven):
  Pexels / Pixabay return real photographs at 3000-6000 px.  Cropped to
  1080x1920 they are genuinely sharp and hold up under Ken Burns zoom, so they
  lead for factual content.  For illustrated storytelling the trade reverses -
  see engine/visuals/ai_image.py, which leads when `visuals.prefer_ai` is set,
  because no stock library can draw the same character twice.

Licensing: Pexels and Pixabay licences both permit commercial use including
monetised YouTube video, with no attribution required (attribution is still
recorded in the AssetManifest).  See docs/SERVICE_COSTS.md.
"""
from __future__ import annotations

from pathlib import Path

import httpx

from ..core.models import Asset
from ..core.util import STOPWORDS, probe_duration, words
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


class PexelsVideoProvider:
    """Real stock FOOTAGE, not a still.

    Everything else here returns an image that the renderer then pans across.
    Ken Burns on a good photo is a legitimate and widely used look, but it is
    not the same as motion, and "why is it only stock images" is a fair thing
    to ask of something calling itself a video generator.

    Pexels' video library is the same licence as their photos - free for
    commercial use, no attribution required - so this costs nothing extra and
    needs no new key. Clips are downloaded at a size close to the target frame
    rather than the largest available: a 4K file is 50-100 MB, takes far longer
    to fetch than the render saves, and is thrown away by the crop anyway.
    """

    name = "pexels_video"
    license_note = "Pexels License - free commercial use, no attribution required"

    def __init__(self, api_key: str = "", timeout: int = 90):
        self.api_key = api_key
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def fetch(self, req: VisualRequest, out_path: Path) -> Asset:
        query = _search_terms(req)
        portrait = req.height > req.width
        target = out_path.with_suffix(".mp4")
        with httpx.Client(timeout=self.timeout, headers={
                **_UA, "Authorization": self.api_key}) as client:
            resp = client.get("https://api.pexels.com/videos/search", params={
                "query": query,
                "orientation": "portrait" if portrait else "landscape",
                "per_page": 15, "size": "medium"})
            resp.raise_for_status()
            videos = (resp.json() or {}).get("videos", [])
            if not videos:
                raise RuntimeError(f"pexels_video: no results for '{query}'")

            # Prefer clips at least as long as the scene needs, so the renderer
            # is not looping a two-second fragment under eight seconds of
            # narration - a visible, distracting repeat.
            wanted = max(req.min_seconds, 1.0)
            long_enough = [v for v in videos if float(v.get("duration") or 0) >= wanted]
            pool = long_enough or videos
            video = pool[req.seed % len(pool)] if req.seed else pool[0]

            files = [f for f in (video.get("video_files") or [])
                     if (f.get("file_type") or "").endswith("mp4") and f.get("link")]
            if not files:
                raise RuntimeError("pexels_video: result had no mp4 rendition")

            # Smallest rendition that still covers the frame; fall back to the
            # largest available if none does.
            def area(f: dict) -> int:
                return int(f.get("width") or 0) * int(f.get("height") or 0)

            covering = [f for f in files
                        if int(f.get("width") or 0) >= req.width
                        and int(f.get("height") or 0) >= req.height]
            pick = min(covering, key=area) if covering else max(files, key=area)

            target.parent.mkdir(parents=True, exist_ok=True)
            with client.stream("GET", pick["link"], follow_redirects=True) as body:
                body.raise_for_status()
                with target.open("wb") as fh:
                    for chunk in body.iter_bytes(1 << 16):
                        fh.write(chunk)

        if target.stat().st_size < 20_000:
            target.unlink(missing_ok=True)
            raise RuntimeError("pexels_video: download too small to be a clip")
        # Prove it is decodable here rather than discovering it mid-render.
        if probe_duration(target) < 0.5:
            target.unlink(missing_ok=True)
            raise RuntimeError("pexels_video: clip is not decodable")

        return Asset(asset=target.name, source="provider:pexels_video",
                     license=self.license_note, prompt=query,
                     attribution=f"Video by {(video.get('user') or {}).get('name', 'unknown')} on Pexels",
                     url=video.get("url", ""), scene_index=req.scene_index)
