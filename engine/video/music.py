"""Music + SFX (spec section 43).

Two sources, in this order:
  1. A user-supplied library in assets/music/ - put public-domain or properly
     licensed tracks there and they are used as-is.
  2. Procedural synthesis with ffmpeg. Fully original, no third-party rights,
     no downloads, no licence risk, works offline.

Nothing is ever auto-downloaded from a music site.

The synthesised bed is a slow ambient chord pad, deliberately plain and quiet:
its job is to remove dead air under the narration, not to be noticed. Voice
always wins - see `mix_audio` in compose.py, which side-chain ducks the music.
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

from ..core.logging import log_event
from ..core.util import ffmpeg_bin, probe_duration, run

MUSIC_DIR = Path(__file__).resolve().parents[2] / "assets" / "music"

# Chord voicings (Hz) per mood. Low registers keep the vocal range clear.
# NOTE: ffmpeg's `tremolo` filter rejects f < 0.1 Hz, so 0.1 is the floor here.
MOODS: dict[str, dict] = {
    "tension":  {"freqs": [110.00, 130.81, 164.81, 220.00], "trem": 0.10,
                 "lowpass": 1300, "echo": "0.8:0.9:520|1080:0.32|0.18"},
    "cinematic": {"freqs": [98.00, 146.83, 196.00, 293.66], "trem": 0.11,
                  "lowpass": 1500, "echo": "0.8:0.9:700|1400:0.36|0.22"},
    "tech":     {"freqs": [123.47, 164.81, 246.94, 329.63], "trem": 0.22,
                 "lowpass": 2200, "echo": "0.7:0.85:340|680:0.26|0.14"},
    "warm":     {"freqs": [130.81, 164.81, 196.00, 261.63], "trem": 0.11,
                 "lowpass": 1600, "echo": "0.8:0.9:640|1280:0.30|0.18"},
    "playful":  {"freqs": [261.63, 329.63, 392.00, 523.25], "trem": 0.55,
                 "lowpass": 2600, "echo": "0.7:0.8:260|520:0.22|0.12"},
    "sombre":   {"freqs": [87.31, 116.54, 174.61, 233.08], "trem": 0.10,
                 "lowpass": 1100, "echo": "0.8:0.9:820|1600:0.38|0.24"},
}

_MOOD_WORDS = {
    "tension": ("tension", "pulse", "rising", "mystery", "suspense"),
    "cinematic": ("cinematic", "epic", "strings", "orchestral", "pad", "space"),
    "tech": ("electronic", "tech", "digital", "synth", "percussive", "pulse"),
    "warm": ("warm", "soft", "gentle", "piano", "calm", "minimal"),
    "playful": ("playful", "ukulele", "marimba", "happy", "kids", "light"),
    "sombre": ("sombre", "sad", "slow", "drone", "low", "dark", "heartbeat"),
}


# Deterministic tie-break. Without this, "slow cinematic pad with a rising
# pulse" scored 2-2 between `cinematic` and `tension` and the winner depended on
# dict iteration order.
_MOOD_PRIORITY = ("playful", "tech", "cinematic", "warm", "sombre", "tension")


def mood_from_text(text: str) -> str:
    """Pick a music mood from a free-text description.

    Scoring: each matching keyword is worth 1, and the mood's own name appearing
    in the text is worth 2 - if the description literally says "cinematic", that
    is a stronger signal than an incidental keyword. Ties resolve through a fixed
    priority order so the result never depends on dict ordering.
    """
    t = (text or "").lower()
    scores: dict[str, float] = {}
    for mood, keys in _MOOD_WORDS.items():
        score = sum(1.0 for k in keys if k in t)
        if mood in t:
            score += 2.0
        scores[mood] = score

    best = max(scores.values(), default=0.0)
    if best <= 0:
        return "cinematic"
    for mood in _MOOD_PRIORITY:
        if scores.get(mood, 0.0) == best:
            return mood
    return "cinematic"


def library_track(seed: str = "") -> Path | None:
    """Pick a user-supplied track, if the user added any."""
    if not MUSIC_DIR.exists():
        return None
    tracks = sorted([p for p in MUSIC_DIR.iterdir()
                     if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}])
    if not tracks:
        return None
    idx = int(hashlib.sha1(seed.encode()).hexdigest()[:6], 16) % len(tracks)
    return tracks[idx]


def synth_bed(duration: float, out_path: Path, *, mood: str = "cinematic",
              seed: str = "", target_lufs: float = -23.0) -> Path:
    """Synthesise an ambient chord bed of `duration` seconds."""
    spec = MOODS.get(mood, MOODS["cinematic"])
    rng = random.Random(int(hashlib.sha1((seed or mood).encode()).hexdigest()[:8], 16))
    freqs: list[float] = list(spec["freqs"])
    # Small detune per render so repeated videos are not bit-identical.
    freqs = [f * rng.uniform(0.997, 1.003) for f in freqs]
    dur = max(duration + 2.0, 4.0)

    inputs: list[str] = []
    chains: list[str] = []
    for i, f in enumerate(freqs):
        inputs += ["-f", "lavfi", "-i",
                   f"sine=frequency={f:.3f}:duration={dur:.2f}:sample_rate=48000"]
        # Upper voices sit further back; slow per-voice tremolo avoids a static drone.
        voice_gain = -3.0 - i * 3.2
        trem = spec["trem"] * rng.uniform(0.8, 1.25) + i * 0.013
        chains.append(
            f"[{i}:a]volume={voice_gain:.1f}dB,"
            f"tremolo=f={max(trem, 0.1):.3f}:d=0.35,"
            f"afade=t=in:st=0:d=2.2[v{i}]")

    # A brown-noise "air" layer gives the pad texture instead of a pure tone.
    noise_idx = len(freqs)
    inputs += ["-f", "lavfi", "-i",
               f"anoisesrc=color=brown:duration={dur:.2f}:sample_rate=48000:amplitude=0.5"]
    chains.append(f"[{noise_idx}:a]lowpass=f=380,volume=-20dB,"
                  f"tremolo=f=0.1:d=0.5[air]")

    mix_inputs = "".join(f"[v{i}]" for i in range(len(freqs))) + "[air]"
    filter_complex = (
        ";".join(chains) +
        f";{mix_inputs}amix=inputs={len(freqs) + 1}:normalize=0[mixed]"
        f";[mixed]highpass=f=40,lowpass=f={spec['lowpass']},"
        f"aecho={spec['echo']},"
        f"afade=t=out:st={max(dur - 2.0, 0.1):.2f}:d=2.0,"
        # Normalise the bed to a known loudness so mix_audio can position it
        # relative to the voice with a single predictable trim.
        f"loudnorm=I={target_lufs:.1f}:TP=-4.0:LRA=7,"
        f"alimiter=limit=0.89:level=disabled[out]")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg_bin(), "-y", "-loglevel", "error", *inputs,
         "-filter_complex", filter_complex, "-map", "[out]",
         "-t", f"{dur:.2f}", "-ar", "48000", "-ac", "2",
         "-c:a", "pcm_s16le", str(out_path)], timeout=600)
    log_event("MUSIC", "ambient bed synthesised", mood=mood,
              seconds=f"{probe_duration(out_path):.1f}")
    return out_path


def build_music(duration: float, out_path: Path, *, mood_text: str = "",
                seed: str = "", prefer_library: bool = True) -> tuple[Path, str]:
    """Return (path, source_label) for a music bed covering `duration`."""
    mood = mood_from_text(mood_text)
    track = library_track(seed) if prefer_library else None
    if track is not None:
        # Loop/trim the user's track to length and normalise its level.
        out_path.parent.mkdir(parents=True, exist_ok=True)
        run([ffmpeg_bin(), "-y", "-loglevel", "error",
             "-stream_loop", "-1", "-i", str(track),
             "-af", (f"afade=t=in:st=0:d=1.5,"
                     f"afade=t=out:st={max(duration - 2.0, 0.1):.2f}:d=2.0,"
                     f"loudnorm=I=-23.0:TP=-4.0:LRA=7,"
                     f"alimiter=limit=0.89:level=disabled"),
             "-t", f"{duration + 1.0:.2f}", "-ar", "48000", "-ac", "2",
             "-c:a", "pcm_s16le", str(out_path)], timeout=600)
        log_event("MUSIC", "using library track", file=track.name)
        return out_path, f"library:{track.name}"

    synth_bed(duration, out_path, mood=mood, seed=seed)
    return out_path, f"synthesised:{mood}"


def build_transition_sfx(times: list[float], total_duration: float,
                         out_path: Path, *, seed: str = "") -> Path | None:
    """A soft filtered-noise 'whoosh' at each scene change.

    Generated from noise + an envelope, so it is original and licence-free.
    Kept quiet: it marks the cut, it should not be an event in itself.
    """
    times = [t for t in times if 0.15 < t < total_duration - 0.15]
    if not times:
        return None
    rng = random.Random(int(hashlib.sha1((seed or "sfx").encode()).hexdigest()[:8], 16))

    inputs: list[str] = ["-f", "lavfi", "-i",
                         f"anullsrc=r=48000:cl=stereo:d={total_duration + 0.5:.2f}"]
    chains: list[str] = []
    labels: list[str] = []
    # Cap the layer count: dozens of concurrent filters give no audible benefit.
    for i, t in enumerate(times[:24]):
        idx = i + 1
        length = rng.uniform(0.30, 0.46)
        centre = rng.choice([700, 900, 1200, 1600])
        inputs += ["-f", "lavfi", "-i",
                   f"anoisesrc=color=white:duration={length:.2f}:"
                   f"sample_rate=48000:amplitude=0.6"]
        delay_ms = int(max(t - 0.06, 0.0) * 1000)
        chains.append(
            f"[{idx}:a]bandpass=f={centre}:width_type=o:width=2.2,"
            f"afade=t=in:st=0:d={length * 0.28:.3f}:curve=exp,"
            f"afade=t=out:st={length * 0.30:.3f}:d={length * 0.70:.3f}:curve=exp,"
            f"volume=-9dB,aformat=channel_layouts=stereo,"
            f"adelay={delay_ms}|{delay_ms}[s{i}]")
        labels.append(f"[s{i}]")

    filter_complex = (";".join(chains) +
                      f";[0:a]{''.join(labels)}amix=inputs={len(labels) + 1}:"
                      f"normalize=0:dropout_transition=0[out]")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg_bin(), "-y", "-loglevel", "error", *inputs,
         "-filter_complex", filter_complex, "-map", "[out]",
         "-t", f"{total_duration:.2f}", "-ar", "48000", "-ac", "2",
         "-c:a", "pcm_s16le", str(out_path)], timeout=600)
    log_event("MUSIC", "transition SFX built", hits=len(labels))
    return out_path
