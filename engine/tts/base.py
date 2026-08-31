"""TTS provider contract.

Per-scene synthesis is deliberate: each scene gets its own audio file, so the
visual cut is guaranteed to land exactly on the narration boundary instead of
being estimated from a words-per-minute guess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class WordMark:
    """Timing of one spoken word, relative to the start of its clip."""
    start: float
    duration: float
    text: str

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class SceneAudio:
    scene_index: int
    path: Path
    duration: float
    text: str
    words: list[WordMark] = field(default_factory=list)
    provider: str = ""
    # True when word timings came from the engine, False when estimated.
    exact_timing: bool = False


@dataclass
class VoiceSpec:
    language: str = "en"
    voice_id: str = ""
    gender: str = "female"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    style: str = "energetic"


class TTSProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def synthesize(self, text: str, out_path: Path, spec: VoiceSpec) -> SceneAudio: ...


def estimate_word_marks(text: str, total_duration: float) -> list[WordMark]:
    """Fallback timing: distribute duration by word length + punctuation pauses.

    Used only by providers that do not report real boundaries (piper, gTTS).
    Weighting by characters plus a pause after punctuation tracks natural speech
    far better than an even split.
    """
    raw = [w for w in text.split() if w.strip()]
    if not raw or total_duration <= 0:
        return []
    weights: list[float] = []
    for w in raw:
        stripped = w.strip(".,!?;:\"'()-")
        weight = max(len(stripped), 1) + 1.4          # per-word overhead
        if w.endswith((",", ";", ":")):
            weight += 2.2                              # short pause
        if w.endswith((".", "!", "?")):
            weight += 4.0                              # sentence pause
        weights.append(weight)
    total_w = sum(weights)
    marks: list[WordMark] = []
    cursor = 0.0
    for word, weight in zip(raw, weights):
        span = total_duration * (weight / total_w)
        marks.append(WordMark(start=cursor, duration=span * 0.86, text=word))
        cursor += span
    return marks
