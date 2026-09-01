"""Visual engine: resolves one image per scene and writes the AssetManifest."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..core.config import Config
from ..core.logging import log_event
from ..core.models import Asset, Scene
from ..core.util import ensure_dir, safe_write_json, sha1
from .base import VisualRequest
from .kids_animation import KidsAnimationProvider
from .procedural import ProceduralProvider
from .providers import (PexelsProvider, PexelsVideoProvider, PixabayProvider,
                        PollinationsProvider)


class VisualEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.width = int(cfg.get("visuals.image_width", 1080))
        self.height = int(cfg.get("visuals.image_height", 1920))
        self.transient_retries = int(cfg.get("visuals.transient_retries", 3))
        self.retry_backoff = float(cfg.get("visuals.retry_backoff_seconds", 5.0))
        self.providers = self._build(cfg)

    def _build(self, cfg: Config) -> list:
        order = list(cfg.get("visuals.provider_order", ["pollinations", "procedural"]))
        allow_stock = bool(cfg.get("visuals.allow_stock_apis", True))
        registry = {
            "pexels_video": lambda: PexelsVideoProvider(cfg.secret("PEXELS_API_KEY")),
            "pexels": lambda: PexelsProvider(cfg.secret("PEXELS_API_KEY")),
            "pixabay": lambda: PixabayProvider(cfg.secret("PIXABAY_API_KEY")),
            "pollinations": lambda: PollinationsProvider(
                model=str(cfg.get("visuals.pollinations_model", "flux"))),
            "procedural": ProceduralProvider,
        }
        # Stock photos are sharper -> try them first when keys exist. Stock
        # VIDEO goes ahead of everything: real footage is the difference
        # between a video and a narrated slideshow, and it costs the same key.
        if allow_stock:
            for stock in ("pixabay", "pexels", "pexels_video"):
                if stock not in order:
                    order.insert(0, stock)
        chain = [registry[name]() for name in order if name in registry]
        if not any(p.name == "procedural" for p in chain):
            chain.append(ProceduralProvider())

        # Child-directed scenes get purpose-built animated flashcards instead
        # of a photo search. Keyword-searching stock footage for "ant leaf
        # bubble" returns a macro shot of a real ant - a good nature clip and
        # nothing like children's television. Built here rather than added to
        # `order` because it is selected per REQUEST (made_for_kids), not per
        # job configuration. It borrows the first photo provider so a word it
        # cannot draw still gets a real picture inside the card.
        self.kids_provider = None
        if bool(cfg.get("visuals.kids_animation", True)):
            photo = next((p for p in chain
                          if p.name in ("pexels", "pixabay", "pollinations")
                          and p.available()), None)
            self.kids_provider = KidsAnimationProvider(
                fps=int(cfg.get("video.default_fps", 30)),
                subject_provider=photo)
        return chain

    # ------------------------------------------------------------------
    def generate(self, scenes: list[Scene], out_dir: Path, *, style: str = "",
                 made_for_kids: bool = False, width: int | None = None,
                 height: int | None = None,
                 parallel: int | None = None,
                 durations: list[float] | None = None) -> list[Asset]:
        """Fetch/generate one conditioned image per scene.

        Scenes are processed concurrently because the network providers are
        latency-bound; the fallback chain still applies per scene, so one slow
        or failing provider never blocks the render.
        """
        ensure_dir(out_dir)
        w = width or self.width
        h = height or self.height
        # Free image endpoints rate-limit on concurrency, so keep this low;
        # the wall-clock cost is small next to the video encode.
        if parallel is None:
            parallel = int(self.cfg.get("visuals.parallel", 2))

        # Rate limiting is a property of the PROVIDER, not of a scene. Retrying
        # a 429 per scene, three times each with exponential backoff, is
        # reasonable for a 12-scene Short and ruinous for a 71-scene long-form
        # video: measured at ~15s of pure backoff per scene, i.e. 18 minutes
        # spent waiting to be refused. Once a provider has refused
        # `breaker_limit` times in a row, stop offering it this job.
        # The breaker is RATE based, not consecutive-failure based. Measured
        # against Pollinations on a 71-scene job: it refused about half the
        # requests, so a consecutive counter never reached three and the job
        # paid ~15s of backoff on every second scene regardless.
        breaker_limit = max(1, int(self.cfg.get("visuals.breaker_limit", 3)))
        breaker_rate = float(self.cfg.get("visuals.breaker_failure_rate", 0.5))
        attempted: dict[str, int] = {}
        failed: dict[str, int] = {}
        tripped: set[str] = set()
        lock = threading.Lock()

        def work(scene: Scene) -> Asset:
            req = VisualRequest(
                scene_index=scene.index,
                prompt=scene.visual_prompt or scene.narration,
                keywords=scene.visual_keywords or [],
                width=w, height=h, style=style,
                seed=int(sha1(f"{scene.index}{scene.visual_prompt}")[:6], 16),
                made_for_kids=made_for_kids,
                # Lets the video provider avoid a 2-second clip under an
                # 8-second scene, which loops visibly.
                min_seconds=(durations[scene.index]
                             if durations and scene.index < len(durations) else 0.0),
            )
            target = out_dir / f"image_{scene.index:02d}.jpg"
            errors: list[str] = []
            chain = list(self.providers)
            if made_for_kids and self.kids_provider is not None:
                chain.insert(0, self.kids_provider)
            for provider in chain:
                if provider.name in tripped:
                    errors.append(f"{provider.name}: skipped (rate limited)")
                    continue
                if not provider.available():
                    errors.append(f"{provider.name}: no credentials")
                    continue
                # Retry transient failures before dropping to the next provider.
                # Visual consistency matters: one scene rendering as an abstract
                # field while its neighbours are photographs looks like a bug,
                # and free image endpoints rate-limit constantly.
                attempts = self.transient_retries if provider.name != "procedural" else 1
                with lock:
                    attempted[provider.name] = attempted.get(provider.name, 0) + 1
                for attempt in range(1, attempts + 1):
                    try:
                        asset = provider.fetch(req, target)
                        # NOT `target`: the video provider writes an .mp4
                        # beside the .jpg path it was handed, so the asset's
                        # own filename is the authoritative one.
                        scene.asset_path = str(out_dir / asset.asset)
                        return asset
                    except Exception as exc:
                        transient = _is_transient(exc)
                        last = attempt >= attempts or not transient
                        if last:
                            errors.append(f"{provider.name}: {str(exc)[:120]}")
                            log_event("VISUAL", "provider failed, falling back",
                                      scene=scene.index, provider=provider.name,
                                      error=str(exc)[:140])
                            # `procedural` is the floor of the chain: dropping
                            # it would strand a scene with no image at all.
                            if provider.name != "procedural":
                                with lock:
                                    failed[provider.name] = \
                                        failed.get(provider.name, 0) + 1
                                    fails = failed[provider.name]
                                    tries = max(attempted[provider.name], 1)
                                    if (fails >= breaker_limit
                                            and fails / tries >= breaker_rate
                                            and provider.name not in tripped):
                                        tripped.add(provider.name)
                                        log_event(
                                            "VISUAL",
                                            "provider dropped for this job",
                                            provider=provider.name,
                                            failed=f"{fails}/{tries} scenes",
                                            reason="a rate limit is global, so "
                                                   "retrying it per scene only "
                                                   "buys backoff")
                            break
                        wait = self.retry_backoff * (2 ** (attempt - 1))
                        log_event("VISUAL", "transient failure, retrying",
                                  scene=scene.index, provider=provider.name,
                                  wait=f"{wait:.0f}s", error=str(exc)[:100])
                        time.sleep(wait)
            # ProceduralProvider cannot fail, so reaching here means a bug.
            raise RuntimeError(f"scene {scene.index}: " + " | ".join(errors))

        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            assets = list(pool.map(work, scenes))

        for scene, asset in zip(scenes, assets):
            # The video provider writes an .mp4 next to the .jpg path it was
            # handed, so the asset's own filename is authoritative here.
            scene.asset_path = str(out_dir / asset.asset)

        self.write_manifest(assets, out_dir.parent / "asset_manifest.json")
        counts: dict[str, int] = {}
        for a in assets:
            counts[a.source] = counts.get(a.source, 0) + 1
        log_event("VISUAL", "visuals ready", scenes=len(assets),
                  sources=",".join(f"{k}x{v}" for k, v in counts.items()))
        return assets

    @staticmethod
    def write_manifest(assets: list[Asset], path: Path) -> Path:
        return safe_write_json(path, {
            "count": len(assets),
            "note": ("Every asset is either generated locally, AI-generated, or "
                     "licensed for commercial reuse. No third-party video "
                     "footage is used anywhere in this pipeline."),
            "assets": [a.to_dict() for a in assets],
        })


def _is_transient(exc: Exception) -> bool:
    """Rate limits, timeouts and 5xx are worth retrying; 401/404 are not."""
    text = str(exc).lower()
    if any(tok in text for tok in ("429", "too many requests", "rate limit",
                                   "timeout", "timed out", "connection",
                                   "temporarily", "503", "502", "504",
                                   "read error", "reset by peer")):
        return True
    if any(tok in text for tok in ("401", "403", "404", "no results",
                                   "no credentials", "not a valid image")):
        return False
    return False
