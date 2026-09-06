"""AI text-to-image scenes: illustrated storytelling, not stock photography.

This is the provider that changes what the videos look like. Stock libraries
can supply a photograph of a sugarcane field; they cannot supply "the same
three children walking past that field, in the same drawing style, for three
hundred consecutive shots". That is what an illustrated story channel is made
of, and it is the whole gap between a narrated slideshow and something people
watch for half an hour.

Three things here were learned by getting them wrong first:

1. STYLE IS NOT A DECORATION. The previous AI path appended ", highly detailed,
   sharp focus, professional photography" to every prompt. Asking for an anime
   cel-shaded illustration and professional photography in the same breath gets
   neither - the model splits the difference into the soft airbrushed look that
   reads as "AI slop". The style string now comes from the template and nothing
   is bolted on that could contradict it.

2. THE SCENE MUST LEAD. Putting a long style block and a full character sheet
   first, with the scene trailing behind, produced the SAME IMAGE for two
   completely different scenes: a large fixed preamble dominates and the model
   treats the tail as noise. Measured directly - two prompts differing only in
   their scene text returned byte-identical images. The scene leads and the
   style is a short suffix.

   Tested afterwards, and worth recording: with a per-scene seed, moving a
   SHORT style tag to the front changes very little on the current backend.
   Prompt order was never the whole story - see the note on backends below.

3. VARIETY HAS TO BE VERIFIED, NOT ASSUMED. Even with per-scene seeds, a
   generator asked for eleven variations of "a quiet village morning" will
   sometimes return the same picture twice. Duplicates are detected with a
   perceptual hash and regenerated with a different seed, because two identical
   shots in a row look like a broken render.

4. THE BACKEND SETS THE CEILING, NOT THE PROMPT. The keyless endpoint now
   offers exactly one model (it reports `["sana"]`; `flux` is accepted and
   silently mapped). Sana has a strong painterly house style, and no prompt
   phrasing tested here got clean cel-shaded anime line art out of it - three
   prompt shapes on a fixed seed all returned the same soft painted look. That
   is a large improvement on stock photography for illustrated storytelling and
   it is NOT the flat-colour anime of a Ghibli-style channel.

   Confirmed by swapping the backend: the same prompt through FLUX.1-schnell
   returns clean cel-shaded artwork with bold outlines and coherent faces. The
   prompt was never the problem. So the HTTP call sits behind a small backend
   interface that `visuals.ai_image_backend` selects - and the better one is
   metered, seven images on a free account before a 402, against the ~300 a
   half-hour video needs.

Licensing: images are generated from our own prompts. No third-party rights are
claimed and nothing is taken from another creator (spec section 13).
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from pathlib import Path

import httpx
from PIL import Image

from ..core.logging import log_event
from ..core.models import Asset
from .base import VisualRequest, condition_image, is_valid_image

_UA = {"User-Agent": "AutoTubeAI/0.1 (+https://github.com/local/autotube-ai)"}

# Artefacts that ruin a frame regardless of style. Deliberately short: every
# extra clause dilutes the scene description, which is the part that matters.
_CONSTRAINTS = "no text, no watermark, no captions, no signature"

_KIDS_CONSTRAINTS = "gentle, friendly, nothing frightening"

# Fallback when the template supplies no style. Illustration rather than
# photography: an unstyled AI photograph looks worse than real stock, whereas
# an unstyled AI illustration is at least coherent.
_DEFAULT_STYLE = ("2D illustration, clean line art, flat colours, "
                  "soft natural light")


def average_hash(path: Path, size: int = 8) -> int:
    """Perceptual hash: one bit per cell, set when the cell is above the mean.

    Cheap and good enough for the only question being asked - "is this the same
    picture as one we already have" - while tolerating the JPEG differences
    that make byte comparison useless.
    """
    with Image.open(path) as raw:
        small = raw.convert("L").resize((size, size), Image.LANCZOS)
        pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    bits = 0
    for i, value in enumerate(pixels):
        if value > mean:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


class PollinationsBackend:
    """Keyless and free. The default, and the reason this costs nothing.

    Reports one model (`sana`) and accepts others by silently mapping them, so
    the model name here is nearly cosmetic. Painterly house style; see note 4
    in the module docstring.
    """

    id = "pollinations"
    label = "Pollinations (keyless)"

    def __init__(self, model: str = "sana", timeout: int = 120):
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        return True

    def fetch(self, prompt: str, *, width: int, height: int, seed: int) -> bytes:
        url = ("https://image.pollinations.ai/prompt/"
               + urllib.parse.quote(prompt, safe="")
               + f"?width={width}&height={height}"
               + f"&nologo=true&model={self.model}&seed={seed}")
        with httpx.Client(timeout=self.timeout, headers=_UA,
                          follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            if "image" not in resp.headers.get("content-type", ""):
                raise RuntimeError("pollinations: response was not an image")
            return resp.content


class CreditExhausted(RuntimeError):
    """The paid quota is gone. Retrying costs time and changes nothing."""


class HuggingFaceBackend:
    """Runs a model that actually draws what you ask for.

    This is the path to the flat-colour illustration the keyless endpoint will
    not produce. Verified: FLUX.1-schnell returns clean cel-shaded artwork with
    bold outlines and coherent faces, in 3 to 12 seconds - the class of image an
    illustrated story channel is made of.

    Two hard-won details about the API:

      * `api-inference.huggingface.co` NO LONGER EXISTS. It does not even
        resolve in DNS. The first version of this class posted to it and would
        never have worked. Serving moved to Inference Providers, routed through
        router.huggingface.co, and the model is dispatched to a third party
        (nscale, fal-ai, replicate, wavespeed). Each provider has its own
        request and response shape, which is why this delegates routing to
        huggingface_hub rather than reimplementing it.

      * IT IS NOT FREE AT VOLUME. A free account gets a small monthly credit
        for Inference Providers - measured here as seven images before a 402,
        against the ~300 a thirty-minute video needs. So 402 is raised as
        CreditExhausted, which the provider treats as terminal for the whole
        job instead of retrying it three times per scene.

    huggingface_hub is imported lazily so a deployment without it degrades to
    keyless generation rather than failing to start.
    """

    id = "huggingface"
    label = "Hugging Face Inference Providers"

    def __init__(self, model: str, token: str, timeout: int = 180):
        self.model = model
        self.token = token
        self.timeout = timeout
        self._client = None

    def available(self) -> bool:
        if not (self.token and self.model):
            return False
        try:
            import huggingface_hub                       # noqa: F401
        except ImportError:
            log_event("VISUAL", "huggingface_hub not installed",
                      hint="pip install huggingface_hub")
            return False
        return True

    def _client_or_build(self):
        if self._client is None:
            from huggingface_hub import InferenceClient
            # provider="auto" picks whichever third party is currently serving
            # the model. Pinning one would break the day it goes offline, and
            # they do: FLUX.1-schnell reports `together` as errored right now.
            self._client = InferenceClient(api_key=self.token, provider="auto",
                                           timeout=self.timeout)
        return self._client

    def fetch(self, prompt: str, *, width: int, height: int, seed: int) -> bytes:
        import io
        try:
            image = self._client_or_build().text_to_image(
                prompt, model=self.model, width=width, height=height, seed=seed)
        except Exception as exc:
            text = str(exc)
            if "402" in text or "Payment Required" in text:
                raise CreditExhausted(
                    "huggingface: inference credit exhausted (402). Add credit, "
                    "subscribe to PRO, or set visuals.ai_image_backend back to "
                    "pollinations.") from exc
            raise
        buf = io.BytesIO()
        image.save(buf, "PNG")
        return buf.getvalue()


def build_backend(cfg) -> object:
    """Pick a backend from config, falling back to the keyless one.

    Falls back rather than failing: a missing HF token should degrade to free
    generation, not stop the job.
    """
    wanted = str(cfg.get("visuals.ai_image_backend", "pollinations")).lower()
    if wanted == "huggingface":
        backend = HuggingFaceBackend(
            model=str(cfg.get("visuals.ai_image_model",
                              "black-forest-labs/FLUX.1-schnell")),
            token=cfg.secret("HF_API_TOKEN"))
        if backend.available():
            log_event("VISUAL", "using paid image generation",
                      backend=backend.id, model=backend.model)
            return backend
        log_event("VISUAL", "huggingface unavailable, using keyless generation",
                  reason=("no HF_API_TOKEN" if not cfg.secret("HF_API_TOKEN")
                          else "huggingface_hub not installed"))
    return PollinationsBackend(
        model=str(cfg.get("visuals.pollinations_model", "sana")))


class AIImageProvider:
    """Free, keyless text-to-image with prompt discipline and dedup.

    Reality check (spec section 41): no account and no key, but rate limited
    and slow - measured 8 to 45 seconds per image, and slower under load. A
    thirty-minute video needs roughly three hundred images, so generation is
    measured in hours, not minutes. That is a scheduling fact, not a defect,
    and the caller renders overnight.
    """

    name = "ai_image"
    license_note = ("AI-generated from our own prompt - "
                    "no third-party rights claimed")

    # Two images closer than this are treated as the same picture. 8x8 hash,
    # so 64 bits total; 6 is tight enough to catch re-issues of the same
    # generation while ignoring compression noise.
    DUPLICATE_DISTANCE = 6

    def __init__(self, backend=None, max_attempts: int = 3,
                 retry_backoff: float = 4.0, fallback=None):
        self.backend = backend or PollinationsBackend()
        # Where to go when a paid backend runs out of credit. Injectable so it
        # can be exercised without reaching the network - hard-coding the
        # constructor meant the only way to test the swap was to make a real
        # request, which is exactly the kind of test that rots.
        self.fallback = fallback if fallback is not None else PollinationsBackend()
        self.max_attempts = max_attempts
        self.retry_backoff = retry_backoff
        self._seen: dict[int, int] = {}      # hash -> scene that produced it
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return getattr(self.backend, "model", "")

    def available(self) -> bool:
        return bool(self.backend.available())

    # ------------------------------------------------------------------
    def build_prompt(self, req: VisualRequest) -> str:
        """Scene first, then who is in it, then how it should look.

        The order is the point. See the module docstring: a leading style or
        character block makes the model ignore the scene entirely.
        """
        parts = [req.prompt.strip() or ", ".join(req.keywords[:6])]
        characters = (req.characters or "").strip()
        if characters:
            parts.append(characters)
        parts.append(req.style.strip() or _DEFAULT_STYLE)
        parts.append(_CONSTRAINTS)
        if req.made_for_kids:
            parts.append(_KIDS_CONSTRAINTS)
        return ". ".join(p.rstrip(" .,") for p in parts if p.strip())

    def _seed_for(self, req: VisualRequest, attempt: int) -> int:
        """Deterministic per scene, different on every retry.

        Deterministic matters for reruns: regenerating a job after a crash
        should not silently reshuffle every visual. The attempt offset is large
        and prime-ish so a retry lands somewhere unrelated rather than on a
        neighbouring scene's seed.
        """
        base = req.seed or (req.scene_index + 1) * 7919
        return (base + attempt * 104729) % 2_147_483_647

    def _download(self, prompt: str, req: VisualRequest, seed: int,
                  out_path: Path) -> None:
        data = self.backend.fetch(prompt, width=req.width, height=req.height,
                                  seed=seed)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)

    def _claim(self, digest: int, scene_index: int) -> int | None:
        """Register a hash. Returns the clashing scene index, or None if new."""
        with self._lock:
            for known, owner in self._seen.items():
                if owner != scene_index and hamming(known, digest) <= self.DUPLICATE_DISTANCE:
                    return owner
            self._seen[digest] = scene_index
            return None

    def fetch(self, req: VisualRequest, out_path: Path) -> Asset:
        prompt = self.build_prompt(req)
        last_error: Exception | None = None

        for attempt in range(self.max_attempts):
            seed = self._seed_for(req, attempt)
            if attempt:
                # Back off before asking again.
                #
                # The free endpoint limits by concurrency, and three requests
                # fired inside fourteen seconds are all refused - which is how
                # an illustrated scene ended up falling through to a stock
                # PHOTOGRAPH, the one substitution this provider exists to
                # avoid. Waiting is cheaper than the wrong medium.
                time.sleep(self.retry_backoff * attempt)
            try:
                self._download(prompt, req, seed, out_path)
            except CreditExhausted as exc:
                # Terminal for the paid backend, but NOT for the job.
                #
                # Dropping the provider here would send every remaining scene
                # to `procedural`, because a template that prefers illustration
                # has stock removed from its chain - so one 402 would turn the
                # rest of the video into abstract shapes. Falling back to
                # keyless generation keeps the MEDIUM (a drawn scene) and only
                # loses fidelity, which is the smaller loss by a wide margin.
                #
                # Swapped once, under the lock, so twenty concurrent scenes do
                # not each rebuild the backend and re-log the warning.
                with self._lock:
                    if self.backend is not self.fallback:
                        log_event("VISUAL", "paid image credit exhausted, "
                                  "falling back to keyless generation",
                                  was=self.backend.id,
                                  now=self.fallback.id, error=str(exc)[:120])
                        self.backend = self.fallback
                last_error = exc
                continue
            except Exception as exc:            # network, HTTP, wrong type
                last_error = exc
                continue

            if not is_valid_image(out_path):
                last_error = RuntimeError("ai_image: file is not a valid image")
                continue

            digest = average_hash(out_path)
            clash = self._claim(digest, req.scene_index)
            if clash is not None and attempt < self.max_attempts - 1:
                # Same picture as an earlier scene. Reseed and ask again.
                log_event("VISUAL", "duplicate image, regenerating",
                          scene=req.scene_index, matches_scene=clash,
                          attempt=attempt + 1)
                continue

            condition_image(out_path, req.width, req.height, sharpen=True)
            log_event("VISUAL", "AI image generated", scene=req.scene_index,
                      backend=self.backend.id, model=self.model, seed=seed,
                      characters=bool(req.characters))
            return Asset(asset=out_path.name,
                         source=f"generated:{self.backend.id}",
                         license=self.license_note, prompt=prompt[:400],
                         scene_index=req.scene_index)

        # Name the cause. "no usable image after 3 attempts" is unactionable:
        # a rate limit, a cold model and a malformed prompt all read the same,
        # and they need completely different responses.
        detail = f": {str(last_error)[:160]}" if last_error else ""
        raise RuntimeError(
            f"ai_image: no usable image after {self.max_attempts} attempts"
            f"{detail}") from last_error
