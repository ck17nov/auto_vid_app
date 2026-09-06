"""Concrete TTS providers, free-first (spec section 12).

Order of preference:
  1. edge   - Microsoft Edge read-aloud endpoint. Free, no key, no account.
              Returns exact per-word boundaries -> frame-accurate captions.
  2. piper  - fully offline neural TTS (MIT). Needs a downloaded voice model.
  3. gtts   - Google Translate TTS. Free, no key, lower prosody quality.
  4. android- the phone's own TTS engine (used by the Android app, not here).

Never clones a real person's voice: all voices are the provider's own
synthetic voices, which is what their terms allow.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from ..core.logging import log_event
from ..core.util import CommandError, ffmpeg_bin, probe_duration, run, which
from .base import SceneAudio, VoiceSpec, WordMark, estimate_word_marks

# Curated voice map. Multiple genders so the user can choose.
EDGE_VOICES: dict[str, dict[str, list[str]]] = {
    "en":    {"female": ["en-US-AriaNeural", "en-US-JennyNeural", "en-US-EmmaMultilingualNeural"],
              "male":   ["en-US-AndrewMultilingualNeural", "en-US-GuyNeural", "en-US-BrianNeural"]},
    "en-IN": {"female": ["en-IN-NeerjaNeural", "en-IN-NeerjaExpressiveNeural"],
              "male":   ["en-IN-PrabhatNeural"]},
    "en-GB": {"female": ["en-GB-SoniaNeural"], "male": ["en-GB-RyanNeural"]},
    "hi":    {"female": ["hi-IN-SwaraNeural"], "male": ["hi-IN-MadhurNeural"]},
    # Hinglish is Hindi-English code-mixing written in Latin script, which is
    # how people actually write it. An Indian-English voice reads that
    # correctly; the Hindi voices expect Devanagari and mispronounce it.
    "hi-Latn": {"female": ["en-IN-NeerjaNeural", "en-IN-NeerjaExpressiveNeural"],
                "male":   ["en-IN-PrabhatNeural"]},
    "ta":    {"female": ["ta-IN-PallaviNeural"], "male": ["ta-IN-ValluvarNeural"]},
    "te":    {"female": ["te-IN-ShrutiNeural"], "male": ["te-IN-MohanNeural"]},
    "bn":    {"female": ["bn-IN-TanishaaNeural"], "male": ["bn-IN-BashkarNeural"]},
    "mr":    {"female": ["mr-IN-AarohiNeural"], "male": ["mr-IN-ManoharNeural"]},
    "es":    {"female": ["es-ES-ElviraNeural"], "male": ["es-ES-AlvaroNeural"]},
    "fr":    {"female": ["fr-FR-DeniseNeural"], "male": ["fr-FR-HenriNeural"]},
    "de":    {"female": ["de-DE-KatjaNeural"], "male": ["de-DE-ConradNeural"]},
    "pt":    {"female": ["pt-BR-FranciscaNeural"], "male": ["pt-BR-AntonioNeural"]},
    "ar":    {"female": ["ar-EG-SalmaNeural"], "male": ["ar-EG-ShakirNeural"]},
    "id":    {"female": ["id-ID-GadisNeural"], "male": ["id-ID-ArdiNeural"]},
    "ja":    {"female": ["ja-JP-NanamiNeural"], "male": ["ja-JP-KeitaNeural"]},
}

# Some voices support expressive styles; map our abstract style to a real one.
_STYLE_HINT = {
    "energetic": "+8%",
    "excited": "+12%",
    "calm": "-6%",
    "gentle": "-8%",
    "serious": "+0%",
    "storytelling": "-2%",
}


def resolve_voice(spec: VoiceSpec) -> str:
    if spec.voice_id:
        return spec.voice_id
    lang = spec.language or "en"
    table = EDGE_VOICES.get(lang) or EDGE_VOICES.get(lang.split("-")[0]) or EDGE_VOICES["en"]
    options = table.get(spec.gender) or next(iter(table.values()))
    return options[0]


# --------------------------------------------------------------------------
# 1. edge-tts
# --------------------------------------------------------------------------
class EdgeTTSProvider:
    name = "edge"
    # Accepts a rate delta, so the duration re-fit works.
    supports_rate = True

    def __init__(self, timeout: int = 90):
        self.timeout = timeout

    def available(self) -> bool:
        try:
            import edge_tts  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, out_path: Path, spec: VoiceSpec) -> SceneAudio:
        import edge_tts

        voice = resolve_voice(spec)
        rate = spec.rate or _STYLE_HINT.get(spec.style, "+0%")
        mp3_path = out_path.with_suffix(".mp3")
        mp3_path.parent.mkdir(parents=True, exist_ok=True)

        # `boundary` does not exist before edge-tts 7.1. Passing it to an older
        # version raises TypeError, the provider fails outright, and the chain
        # collapses to gTTS - which has neither word boundaries nor rate
        # control, so captions become estimates and the duration re-fit turns
        # into a no-op. That happened on a fresh install pinned to 7.0.0 and
        # produced a 63s video for a 45s request. requirements.txt is pinned
        # correctly now; this keeps a mismatched environment degraded rather
        # than broken, because edge-tts without exact timings still beats gTTS.
        import inspect
        supports_boundary = "boundary" in inspect.signature(
            edge_tts.Communicate.__init__).parameters

        async def _run() -> tuple[bytes, list[WordMark]]:
            # boundary="WordBoundary" is required: the default is SentenceBoundary
            # and yields no per-word timings.
            extra = {"boundary": "WordBoundary"} if supports_boundary else {}
            comm = edge_tts.Communicate(
                text, voice, rate=rate, pitch=spec.pitch or "+0Hz",
                receive_timeout=self.timeout, **extra)
            audio = bytearray()
            marks: list[WordMark] = []
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # offsets are in 100-nanosecond ticks
                    marks.append(WordMark(
                        start=chunk["offset"] / 1e7,
                        duration=chunk["duration"] / 1e7,
                        text=chunk["text"]))
            return bytes(audio), marks

        if not supports_boundary:
            log_event("TTS", "edge-tts is too old for word boundaries",
                      installed=getattr(edge_tts, "__version__", "?"),
                      effect="captions will be estimated, not frame-accurate",
                      fix="pip install -U edge-tts")
        audio_bytes, marks = _run_async(_run())
        if len(audio_bytes) < 800:
            raise RuntimeError(f"edge-tts returned {len(audio_bytes)} bytes of audio")
        mp3_path.write_bytes(audio_bytes)

        to_wav(mp3_path, out_path)
        mp3_path.unlink(missing_ok=True)
        duration = probe_duration(out_path)
        if not marks:
            marks = estimate_word_marks(text, duration)
            exact = False
        else:
            exact = True
        return SceneAudio(scene_index=-1, path=out_path, duration=duration, text=text,
                          words=marks, provider=self.name, exact_timing=exact)


def _run_async(coro):
    """Run a coroutine even if an event loop already exists (FastAPI worker)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# --------------------------------------------------------------------------
# 2. Piper (offline)
# --------------------------------------------------------------------------
class PiperTTSProvider:
    """Offline neural TTS. Zero network, zero cost, unlimited use.

    Needs: `pip install piper-tts` (or the piper binary) and a voice .onnx
    downloaded into assets/piper/. See docs/API_SETUP.md.
    """
    name = "piper"
    # No rate control in the CLI we invoke.
    supports_rate = False

    def __init__(self, model_dir: Path | None = None):
        self.model_dir = Path(model_dir or Path(__file__).resolve().parents[2] / "assets" / "piper")

    def _binary(self) -> str | None:
        return which("piper")

    def _model_for(self, spec: VoiceSpec) -> Path | None:
        if not self.model_dir.exists():
            return None
        models = sorted(self.model_dir.glob("*.onnx"))
        if not models:
            return None
        lang = (spec.language or "en").split("-")[0]
        for m in models:
            if m.name.startswith(lang):
                return m
        return models[0]

    def available(self) -> bool:
        if self._model_for(VoiceSpec()) is None:
            return False
        if self._binary():
            return True
        try:
            import piper  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, out_path: Path, spec: VoiceSpec) -> SceneAudio:
        model = self._model_for(spec)
        if model is None:
            raise RuntimeError("no piper voice model found in assets/piper/")
        raw = out_path.with_suffix(".piper.wav")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        binary = self._binary()
        if binary:
            proc = subprocess.run(
                [binary, "-m", str(model), "-f", str(raw)],
                input=text, text=True, capture_output=True, timeout=600)
            if proc.returncode != 0:
                raise CommandError([binary], proc.returncode, proc.stderr or "")
        else:
            proc = subprocess.run(
                [os.sys.executable, "-m", "piper", "-m", str(model), "-f", str(raw)],
                input=text, text=True, capture_output=True, timeout=600)
            if proc.returncode != 0:
                raise CommandError(["piper"], proc.returncode, proc.stderr or "")
        to_wav(raw, out_path)
        raw.unlink(missing_ok=True)
        duration = probe_duration(out_path)
        return SceneAudio(scene_index=-1, path=out_path, duration=duration, text=text,
                          words=estimate_word_marks(text, duration),
                          provider=self.name, exact_timing=False)


# --------------------------------------------------------------------------
# 3. gTTS
# --------------------------------------------------------------------------
class GTTSProvider:
    name = "gtts"
    # gTTS has no rate parameter at all. Re-fitting duration by
    # changing the speaking rate is a silent no-op here, which is
    # how a 45s request shipped as 63s.
    supports_rate = False
    _LANG = {"en": "en", "en-IN": "en", "hi": "hi", "ta": "ta", "te": "te",
             "bn": "bn", "mr": "mr", "es": "es", "fr": "fr", "de": "de",
             "pt": "pt", "ar": "ar", "id": "id", "ja": "ja"}

    def available(self) -> bool:
        try:
            import gtts  # noqa: F401
            return True
        except ImportError:
            return False

    def synthesize(self, text: str, out_path: Path, spec: VoiceSpec) -> SceneAudio:
        from gtts import gTTS

        lang = self._LANG.get(spec.language, "en")
        tld = "co.in" if (spec.language or "").endswith("IN") else "com"
        mp3 = out_path.with_suffix(".mp3")
        mp3.parent.mkdir(parents=True, exist_ok=True)
        gTTS(text=text, lang=lang, tld=tld, slow=False).save(str(mp3))
        to_wav(mp3, out_path)
        mp3.unlink(missing_ok=True)
        duration = probe_duration(out_path)
        return SceneAudio(scene_index=-1, path=out_path, duration=duration, text=text,
                          words=estimate_word_marks(text, duration),
                          provider=self.name, exact_timing=False)


# --------------------------------------------------------------------------
# Audio conditioning
# --------------------------------------------------------------------------
def to_wav(src: Path, dst: Path, sample_rate: int = 48000) -> Path:
    """Decode to 48 kHz mono WAV and lightly condition the voice.

    - highpass 80 Hz  : removes rumble the phone speaker cannot reproduce
    - dynaudnorm      : evens out level without pumping
    Loudness targeting happens later, on the full mix (two-pass loudnorm).

    THERE IS NO LOWPASS HERE, AND ADDING ONE AT 12 kHz WILL BRING BACK THE BEEP.

    The chain used to include `lowpass=f=12000` to "tame TTS sibilance". edge-tts
    returns 24 kHz audio, and ffmpeg applies -af at the decoded source rate and
    only resamples afterwards - so that cutoff sat exactly at Nyquist (sr / 2).
    ffmpeg's `lowpass` is a bilinear-transform biquad, and the prewarp term
    tan(pi * f / sr) diverges as f approaches sr / 2, leaving coefficients that
    ring instead of attenuating. Fed real speech, the filter self-oscillated and
    printed a sustained tone at exactly 12000.0 Hz, measured 35 dB above the
    voice fundamental - the high-pitched beep audible under every single video.

    It was also pointless: a 24 kHz stream holds nothing above 12 kHz to remove.

    If sibilance ever does need taming, the cutoff must be derived from the
    actual source rate and kept well below Nyquist. A fixed 12 kHz is only safe
    for sources at 32 kHz or above, which this provider is not.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(src),
         "-af", "highpass=f=80,dynaudnorm=f=180:g=11:p=0.9:m=8",
         "-ar", str(sample_rate), "-ac", "1", "-c:a", "pcm_s16le", str(dst)],
        timeout=300)
    return dst


def trim_trailing_silence(path: Path, threshold_db: float = -45.0,
                          keep: float = 0.18) -> float:
    """Trim silence at the END only.

    Leading audio is never touched: word marks are relative to clip start, so
    trimming the head would desynchronise every caption.
    """
    tmp = path.with_suffix(".trim.wav")
    try:
        run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(path),
             "-af", (f"areverse,silenceremove=start_periods=1:start_silence={keep}:"
                     f"start_threshold={threshold_db}dB:detection=peak,areverse"),
             "-c:a", "pcm_s16le", str(tmp)], timeout=300)
        if tmp.exists() and tmp.stat().st_size > 1000:
            new_dur = probe_duration(tmp)
            if new_dur > 0.25:
                shutil.move(str(tmp), str(path))
                return new_dur
    except CommandError as exc:
        log_event("TTS", "trailing-silence trim skipped", error=str(exc)[:120])
    finally:
        tmp.unlink(missing_ok=True)
    return probe_duration(path)


def build_providers(order: list[str]) -> list:
    registry = {"edge": EdgeTTSProvider, "piper": PiperTTSProvider, "gtts": GTTSProvider}
    out = []
    for name in order:
        cls = registry.get(name)
        if cls:
            out.append(cls())
    return out or [EdgeTTSProvider()]
