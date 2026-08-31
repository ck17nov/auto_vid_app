"""Retention engine (spec section 15).

Analyses the script + the real (measured) scene timings and produces a
RetentionScore with actionable, specific notes such as:
    "Scene 3 is 7.8s with no visual change."
    "Hook takes 3.4s before the curiosity statement."

`auto_improve` then applies the fixes it can apply safely: splitting overlong
scenes so the visual changes, trimming dead weight, and tightening the hook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.logging import log_event
from ..core.models import Scene, Script
from ..core.niche import NicheProfile
from ..core.util import clamp, count_words, sentences

# Phrases that add no information (spec section 15).
FILLER_PATTERNS = [
    r"\b(basically|essentially|literally|actually just|kind of|sort of)\b",
    r"\b(as (you|we) (all )?know|needless to say|it goes without saying)\b",
    r"\b(in other words|that is to say|to be honest|at the end of the day)\b",
    r"\b(let('| a)?s (talk about|discuss|dive into))\b",
]

CURIOSITY_MARKERS = {
    "why", "how", "what", "who", "but", "until", "except", "nobody", "no one",
    "never", "actually", "strange", "wrong", "hidden", "secret", "impossible",
    "unless", "almost", "yet",
}


@dataclass
class RetentionReport:
    score: float = 0.0
    notes: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "notes": self.notes,
                "suggestions": self.suggestions, "metrics": self.metrics}


def _hook_delay(text: str) -> tuple[float, int]:
    """Words before the first curiosity marker, and an approximate delay."""
    tokens = re.findall(r"[A-Za-z']+", (text or "").lower())
    for i, tok in enumerate(tokens):
        if tok in CURIOSITY_MARKERS:
            return i / 2.6, i          # ~2.6 words/second
    return len(tokens) / 2.6, len(tokens)


def analyze(script: Script, profile: NicheProfile, *,
            target_duration: float = 45.0,
            measured: bool = False) -> RetentionReport:
    """Score retention risk. `measured=True` when scene durations are real."""
    scenes = script.scene_objects()
    report = RetentionReport()
    if not scenes:
        report.notes.append("No scenes to analyse.")
        return report

    total_words = count_words(script.script)
    durations = [s.duration for s in scenes if s.duration > 0]
    total_duration = sum(durations) if durations else (
        total_words / max(profile.words_per_second, 1.2))

    # --- 1. Hook strength -------------------------------------------
    hook_text = script.hook or scenes[0].narration
    delay, delay_words = _hook_delay(hook_text)
    hook_words = count_words(hook_text)
    hook_score = clamp(1.0
                       - clamp(delay / 3.0) * 0.55
                       - clamp(max(hook_words - 14, 0) / 12.0) * 0.45)
    if delay > 1.6:
        report.notes.append(
            f"Hook takes {delay:.1f}s ({delay_words} words) before the "
            f"curiosity statement.")
        report.suggestions.append(
            "Move the surprising element into the first six words.")
    if hook_words > 16:
        report.notes.append(f"Hook is {hook_words} words - too long to land in "
                            f"the first two seconds.")

    # --- 2. Scene pacing / visual change ----------------------------
    long_scenes: list[int] = []
    ceiling = profile.scene_seconds * 1.9
    for s in scenes:
        span = s.duration if s.duration > 0 else (
            count_words(s.narration) / max(profile.words_per_second, 1.2))
        if span > ceiling:
            long_scenes.append(s.index)
            report.notes.append(
                f"Scene {s.index} is {span:.1f}s with no visual change "
                f"(target under {ceiling:.1f}s).")
    pacing_score = clamp(1.0 - len(long_scenes) / max(len(scenes), 1) * 1.4)
    if long_scenes:
        report.suggestions.append(
            f"Split scene(s) {long_scenes} so the image changes at least every "
            f"{profile.scene_seconds:.1f}s.")

    # --- 3. Sentence length / information density -------------------
    sents = sentences(script.script)
    long_sents = [s for s in sents if count_words(s) > 20]
    avg_words = (total_words / len(sents)) if sents else 0.0
    density = total_words / max(total_duration, 0.5)
    density_score = clamp(1.0 - abs(density - profile.words_per_second) / 1.4)
    sentence_score = clamp(1.0 - len(long_sents) / max(len(sents), 1) * 1.5)
    if long_sents:
        report.notes.append(
            f"{len(long_sents)} sentence(s) exceed 20 words - hard to follow at "
            f"pace (avg {avg_words:.0f} words/sentence).")
        report.suggestions.append("Break long sentences into two beats.")

    # --- 4. Dead time ------------------------------------------------
    filler_hits: list[str] = []
    for pattern in FILLER_PATTERNS:
        filler_hits += re.findall(pattern, script.script, re.I)
    filler_score = clamp(1.0 - len(filler_hits) * 0.16)
    if filler_hits:
        report.notes.append(f"{len(filler_hits)} filler phrase(s) detected.")
        report.suggestions.append("Delete filler phrases; they cost watch time.")

    # --- 5. Pattern interrupts --------------------------------------
    # A pattern interrupt = a question, a number, or a short punch sentence.
    interrupts = sum(1 for s in sents
                     if s.strip().endswith("?")
                     or any(c.isdigit() for c in s)
                     or count_words(s) <= 5)
    expected = max(2, int(total_duration / 12))
    interrupt_score = clamp(interrupts / expected)
    if interrupts < expected:
        report.notes.append(
            f"Only {interrupts} pattern interrupt(s) in {total_duration:.0f}s "
            f"(want about {expected}).")
        report.suggestions.append(
            "Add a short question or a concrete number mid-video.")

    # --- 6. CTA placement -------------------------------------------
    cta_scenes = [s for s in scenes if s.role == "cta"]
    cta_score = 1.0
    if cta_scenes:
        first_cta = cta_scenes[0]
        position = (first_cta.index + 1) / len(scenes)
        cta_words = count_words(first_cta.narration)
        if position < 0.75:
            cta_score = 0.45
            report.notes.append(
                f"CTA appears at {position * 100:.0f}% of the video - too early.")
            report.suggestions.append("Move the CTA to the final scene.")
        if cta_words > 12:
            cta_score = min(cta_score, 0.6)
            report.notes.append(f"CTA is {cta_words} words - keep it under 10.")

    # --- 7. Duration accuracy ---------------------------------------
    drift = abs(total_duration - target_duration) / max(target_duration, 1.0)
    duration_score = clamp(1.0 - drift * 1.6)
    if drift > 0.20:
        report.notes.append(
            f"Runs {total_duration:.1f}s against a {target_duration:.0f}s target "
            f"({drift * 100:.0f}% off).")

    parts = {
        "hook": hook_score,
        "pacing": pacing_score,
        "sentence_length": sentence_score,
        "information_density": density_score,
        "dead_time": filler_score,
        "pattern_interrupts": interrupt_score,
        "cta": cta_score,
        "duration_accuracy": duration_score,
    }
    weights = {"hook": 0.26, "pacing": 0.17, "sentence_length": 0.10,
               "information_density": 0.11, "dead_time": 0.08,
               "pattern_interrupts": 0.11, "cta": 0.07,
               "duration_accuracy": 0.10}
    report.score = round(sum(parts[k] * weights[k] for k in parts) * 100, 1)
    report.metrics = {
        **{k: round(v, 3) for k, v in parts.items()},
        "total_words": total_words,
        "total_duration": round(total_duration, 2),
        "words_per_second": round(density, 2),
        "scene_count": len(scenes),
        "long_scenes": long_scenes,
        "measured_timings": measured,
    }
    log_event("RETENTION", f"score {report.score:.0f}/100",
              hook=f"{hook_score:.2f}", pacing=f"{pacing_score:.2f}",
              notes=len(report.notes))
    return report


# --------------------------------------------------------------------------
def auto_improve(script: Script, profile: NicheProfile,
                 report: RetentionReport) -> tuple[Script, list[str]]:
    """Apply the safe, mechanical fixes (spec section 15: 'then automatically
    improve the script/video when possible').

    Deliberately conservative: it only removes filler and splits overlong
    scenes. It never rewrites meaning - that would need another LLM pass and
    risks introducing claims that were never fact-checked.
    """
    applied: list[str] = []
    scenes = script.scene_objects()

    # 1. Strip filler phrases.
    for s in scenes:
        before = s.narration
        for pattern in FILLER_PATTERNS:
            s.narration = re.sub(pattern + r"\s*", "", s.narration, flags=re.I)
        s.narration = re.sub(r"\s{2,}", " ", s.narration).strip()
        s.narration = re.sub(r"^,\s*", "", s.narration)
        if s.narration and s.narration != before:
            s.narration = s.narration[0].upper() + s.narration[1:]
            applied.append(f"removed filler in scene {s.index}")

    # 2. Split overlong scenes at a sentence boundary so the visual changes.
    ceiling = profile.scene_seconds * 1.9
    rebuilt: list[Scene] = []
    for s in scenes:
        span = s.duration if s.duration > 0 else (
            count_words(s.narration) / max(profile.words_per_second, 1.2))
        parts = sentences(s.narration)
        if span > ceiling and len(parts) >= 2:
            mid = _split_point(parts)
            first, second = " ".join(parts[:mid]), " ".join(parts[mid:])
            if count_words(first) >= 3 and count_words(second) >= 3:
                a = Scene(index=0, narration=first, role=s.role,
                          visual_prompt=s.visual_prompt,
                          visual_keywords=list(s.visual_keywords),
                          on_screen_text=s.on_screen_text)
                b = Scene(index=0, narration=second, role=s.role,
                          visual_prompt=_vary_prompt(s.visual_prompt),
                          visual_keywords=list(s.visual_keywords))
                rebuilt += [a, b]
                applied.append(f"split scene {s.index} ({span:.1f}s) into two")
                continue
        rebuilt.append(s)

    for i, s in enumerate(rebuilt):
        s.index = i
        s.duration = 0.0          # timings are re-measured after TTS
        s.start = 0.0

    script.scenes = [s.to_dict() for s in rebuilt]
    script.script = " ".join(s.narration for s in rebuilt)
    script.visual_plan = [s.visual_prompt for s in rebuilt]
    if applied:
        log_event("RETENTION", "auto-improvements applied", count=len(applied))
    return script, applied


def _split_point(parts: list[str]) -> int:
    """Split as close to half the words as possible."""
    counts = [count_words(p) for p in parts]
    total = sum(counts)
    running = 0
    for i, c in enumerate(counts[:-1]):
        running += c
        if running >= total / 2:
            return i + 1
    return max(1, len(parts) - 1)


def _vary_prompt(prompt: str) -> str:
    """Second half of a split scene needs a DIFFERENT image, not the same one."""
    variations = [
        "closer detail shot, shallow depth of field",
        "wider establishing angle, different perspective",
        "low angle, dramatic side lighting",
        "overhead view, symmetrical composition",
    ]
    import hashlib
    pick = variations[int(hashlib.sha1(prompt.encode("utf-8", "ignore")
                                       ).hexdigest()[:4], 16) % len(variations)]
    return f"{prompt}, {pick}"
