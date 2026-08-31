"""Voice generation orchestrator.

Synthesises each scene separately, conditions the audio, then reports exact
per-scene durations back to the caller so the video timeline is driven by the
real narration length rather than an estimate.
"""
from __future__ import annotations

from pathlib import Path

from ..core.config import Config
from ..core.logging import log_event
from ..core.models import Scene
from ..core.util import ensure_dir, ffmpeg_bin, probe_duration, retry, run
from .base import SceneAudio, VoiceSpec, WordMark
from .providers import build_providers, resolve_voice, trim_trailing_silence


class VoiceEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        order = cfg.get("tts.provider_order", ["edge", "piper", "gtts"])
        self.providers = build_providers(list(order))

    # ------------------------------------------------------------------
    def voice_spec(self, language: str, style: str = "energetic",
                   gender: str | None = None) -> VoiceSpec:
        voice_map = self.cfg.get("tts.voice_map", {}) or {}
        return VoiceSpec(
            language=language,
            voice_id=voice_map.get(language, ""),
            gender=gender or self.cfg.get("tts.voice_gender", "female"),
            rate=self.cfg.get("tts.rate", "+0%"),
            pitch=self.cfg.get("tts.pitch", "+0Hz"),
            style=style,
        )

    def synthesize_scenes(self, scenes: list[Scene], out_dir: Path,
                          spec: VoiceSpec) -> list[SceneAudio]:
        """One clip per scene. Falls back down the provider chain per scene."""
        ensure_dir(out_dir)
        results: list[SceneAudio] = []
        attempts = int(self.cfg.get("automation.max_retries", 3))

        for scene in scenes:
            text = (scene.narration or "").strip()
            if not text:
                continue
            target = out_dir / f"scene_{scene.index:02d}.wav"
            audio = self._synthesize_one(text, target, spec, attempts)
            audio.scene_index = scene.index
            audio.duration = trim_trailing_silence(audio.path)
            # Re-clamp any word mark that now sits past the trimmed end.
            audio.words = [
                WordMark(w.start, min(w.duration, max(audio.duration - w.start, 0.05)), w.text)
                for w in audio.words if w.start < audio.duration
            ]
            results.append(audio)
            log_event("TTS", f"scene {scene.index} voiced",
                      provider=audio.provider, seconds=f"{audio.duration:.2f}",
                      words=len(audio.words), exact=audio.exact_timing)
        if not results:
            raise RuntimeError("no narration produced - every scene was empty")
        return results

    def _synthesize_one(self, text: str, target: Path, spec: VoiceSpec,
                        attempts: int) -> SceneAudio:
        errors: list[str] = []
        for provider in self.providers:
            try:
                if not provider.available():
                    errors.append(f"{provider.name}: unavailable")
                    continue
                return retry(
                    lambda p=provider: p.synthesize(text, target, spec),
                    attempts=attempts,
                    backoff=float(self.cfg.get("automation.retry_backoff_seconds", 5)) / 4,
                    tag="TTS", what=f"{provider.name} synthesis")
            except Exception as exc:
                errors.append(f"{provider.name}: {str(exc)[:160]}")
                log_event("TTS", "provider failed, falling back",
                          provider=provider.name, error=str(exc)[:160])
        raise RuntimeError("all TTS providers failed -> " + " | ".join(errors))

    # ------------------------------------------------------------------
    def concat(self, clips: list[SceneAudio], out_path: Path,
               gap: float = 0.16) -> tuple[float, list[tuple[float, SceneAudio]]]:
        """Join scene clips with a short breath gap.

        Returns the total duration and the absolute start offset of each clip,
        which the caption and timeline builders use directly.
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        offsets: list[tuple[float, SceneAudio]] = []
        cursor = 0.0
        inputs: list[str] = []
        filters: list[str] = []

        for i, clip in enumerate(clips):
            offsets.append((cursor, clip))
            inputs += ["-i", str(clip.path)]
            # Pad every clip except the last with `gap` seconds of silence.
            pad = gap if i < len(clips) - 1 else 0.0
            if pad > 0:
                filters.append(f"[{i}:a]apad=pad_dur={pad}[a{i}]")
            else:
                filters.append(f"[{i}:a]anull[a{i}]")
            cursor += clip.duration + pad

        concat_inputs = "".join(f"[a{i}]" for i in range(len(clips)))
        filter_complex = (";".join(filters) +
                          f";{concat_inputs}concat=n={len(clips)}:v=0:a=1[out]")
        run([ffmpeg_bin(), "-y", "-loglevel", "error", *inputs,
             "-filter_complex", filter_complex, "-map", "[out]",
             "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out_path)],
            timeout=900)
        total = probe_duration(out_path)
        log_event("TTS", "voice track assembled", seconds=f"{total:.2f}",
                  scenes=len(clips))
        return total, offsets

    def describe(self, spec: VoiceSpec) -> str:
        return resolve_voice(spec)
