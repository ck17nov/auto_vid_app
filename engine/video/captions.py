"""Caption generation: animated ASS for burn-in + clean SRT for YouTube.

Why per-word Dialogue events instead of ASS \\k karaoke:
  ASS \\k only sweeps SecondaryColour -> PrimaryColour, so it can express
  "words already spoken change colour" but NOT "only the word being spoken is
  highlighted and scaled".  Emitting one event per word-state gives exact
  control over colour, scale and outline of the active word, which is the look
  every high-retention Short uses.  libass handles thousands of events fine.

Safe areas (spec section 44): captions are clamped inside a configurable band
so they never sit under the YouTube Shorts UI (bottom action bar / right rail)
or the top overlay.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.config import Config
from ..core.logging import log_event
from ..tts.base import SceneAudio
from .fonts import display_font


@dataclass
class CaptionWord:
    start: float
    end: float
    text: str


@dataclass
class CaptionGroup:
    """A phrase that appears on screen as a unit."""
    words: list[CaptionWord]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def _ts(seconds: float) -> str:
    """ASS timestamp: h:mm:ss.cc"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms, s = 0, s + 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _escape(text: str) -> str:
    """Escape the characters libass treats as markup."""
    return (text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
            .replace("\n", " ").strip())


def absolute_words(clips: list[tuple[float, SceneAudio]]) -> list[CaptionWord]:
    """Convert per-clip relative word marks into one absolute timeline."""
    out: list[CaptionWord] = []
    for offset, clip in clips:
        for w in clip.words:
            text = (w.text or "").strip()
            if not text:
                continue
            start = offset + max(w.start, 0.0)
            end = start + max(w.duration, 0.08)
            out.append(CaptionWord(start=start, end=end, text=text))
    out.sort(key=lambda w: w.start)
    # Remove overlaps so a word never starts before the previous one ends.
    for i in range(1, len(out)):
        if out[i].start < out[i - 1].end:
            out[i - 1].end = max(out[i - 1].start + 0.06, out[i].start)
    return out


def group_words(words: list[CaptionWord], max_words: int = 4,
                max_chars: int = 26, max_gap: float = 0.60,
                max_span: float = 2.6) -> list[CaptionGroup]:
    """Chunk words into readable phrases.

    Breaks on: sentence punctuation, word count, character count, a long pause
    between words, or a long elapsed span.  Those five rules together keep the
    on-screen text short enough to read at Shorts pace.
    """
    groups: list[CaptionGroup] = []
    current: list[CaptionWord] = []

    def flush() -> None:
        nonlocal current
        if current:
            groups.append(CaptionGroup(words=current))
            current = []

    for w in words:
        prospective_chars = sum(len(x.text) + 1 for x in current) + len(w.text)
        gap = (w.start - current[-1].end) if current else 0.0
        span = (w.end - current[0].start) if current else 0.0
        if current and (len(current) >= max_words
                        or prospective_chars > max_chars
                        or gap > max_gap
                        or span > max_span):
            flush()
        current.append(w)
        if w.text.rstrip().endswith((".", "!", "?", ":", ";")):
            flush()
    flush()
    return groups


class CaptionEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.enabled = bool(cfg.get("captions.enabled", True))
        self.style = str(cfg.get("captions.style", "karaoke"))
        self.font_size = int(cfg.get("captions.font_size", 92))
        self.primary = str(cfg.get("captions.primary_color", "&H00FFFFFF"))
        self.highlight = str(cfg.get("captions.highlight_color", "&H0000E5FF"))
        self.outline = int(cfg.get("captions.outline", 6))
        self.shadow = int(cfg.get("captions.shadow", 3))
        self.safe_bottom = float(cfg.get("captions.safe_bottom", 0.22))
        self.safe_top = float(cfg.get("captions.safe_top", 0.12))
        self.max_words = int(cfg.get("captions.max_words_on_screen", 3))
        self.max_chars = int(cfg.get("captions.max_chars_on_screen", 20))
        self.uppercase = bool(cfg.get("captions.uppercase", True))

    # ------------------------------------------------------------------
    def build(self, clips: list[tuple[float, SceneAudio]], out_ass: Path,
              out_srt: Path, width: int, height: int, *,
              style_override: str | None = None) -> tuple[Path, Path, int]:
        words = absolute_words(clips)
        if not words:
            raise RuntimeError("no word timings available for captions")

        # Long-form frames are wider, so more words fit comfortably.
        landscape = width > height
        max_words = self.max_words + (2 if landscape else 0)
        max_chars = int(self.max_chars * 1.6) if landscape else self.max_chars
        groups = group_words(words, max_words=max_words, max_chars=max_chars)

        style = style_override or self.style
        ass_text = self._render_ass(groups, width, height, style)
        out_ass.parent.mkdir(parents=True, exist_ok=True)
        out_ass.write_text(ass_text, encoding="utf-8")
        out_srt.write_text(self._render_srt(groups), encoding="utf-8")

        log_event("CAPTION", "captions built", groups=len(groups),
                  words=len(words), style=style)
        return out_ass, out_srt, len(groups)

    # ------------------------------------------------------------------
    def _render_ass(self, groups: list[CaptionGroup], width: int, height: int,
                    style: str) -> str:
        font_file, family = display_font(str(self.cfg.get("captions.font_file", "Anton")))
        size = self._scaled_font_size(width, height)
        # MarginV is measured from the bottom for bottom-aligned text.
        margin_v = int(height * self.safe_bottom)
        margin_h = int(width * 0.075)

        header = [
            "[Script Info]",
            "; AutoTube AI generated captions",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "",
            "[V4+ Styles]",
            ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
             "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
             "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
             "Alignment, MarginL, MarginR, MarginV, Encoding"),
            (f"Style: Main,{family},{size},{self.primary},{self.highlight},"
             f"&H00101010,&H80000000,-1,0,0,0,100,100,1.2,0,1,"
             f"{self.outline},{self.shadow},2,{margin_h},{margin_h},{margin_v},1"),
            "",
            "[Events]",
            ("Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
             "MarginV, Effect, Text"),
        ]

        events: list[str] = []
        if style == "block":
            events = self._block_events(groups)
        elif style == "none":
            events = []
        else:
            events = self._karaoke_events(groups)

        return "\n".join(header + events) + "\n"

    def _scaled_font_size(self, width: int, height: int) -> int:
        """Font size configured for 1080x1920; scale to the actual frame."""
        base = self.font_size
        if width > height:                      # long-form: relatively smaller
            return max(28, int(base * (height / 1920) * 1.35))
        return max(28, int(base * (width / 1080)))

    def _karaoke_events(self, groups: list[CaptionGroup]) -> list[str]:
        """One event per word-state: the active word is coloured and scaled up."""
        events: list[str] = []
        for group in groups:
            words = group.words
            for i, active in enumerate(words):
                start = active.start if i > 0 else group.start
                # Hold the last word until the group ends to avoid a flicker gap.
                end = words[i + 1].start if i + 1 < len(words) else group.end + 0.12
                if end <= start:
                    end = start + 0.10

                pieces: list[str] = []
                for j, w in enumerate(words):
                    token = _escape(w.text.upper() if self.uppercase else w.text)
                    if j == i:
                        # Active: highlight colour, slight scale bump, thicker edge.
                        pieces.append(
                            f"{{\\c{self.highlight}\\3c&H00201000&"
                            f"\\fscx108\\fscy108\\bord{self.outline + 1}}}{token}"
                            f"{{\\r}}")
                    else:
                        pieces.append(f"{{\\c{self.primary}}}{token}{{\\r}}")
                text = " ".join(pieces)
                # A short pop on the first word of the group reads as an entrance.
                if i == 0:
                    text = "{\\fad(70,0)}" + text
                events.append(
                    f"Dialogue: 0,{_ts(start)},{_ts(end)},Main,,0,0,0,,{text}")
        return events

    def _block_events(self, groups: list[CaptionGroup]) -> list[str]:
        """Whole phrase, no per-word highlight (calmer; used for kids content)."""
        events: list[str] = []
        for group in groups:
            tokens = [_escape(w.text.upper() if self.uppercase else w.text)
                      for w in group.words]
            text = "{\\fad(90,70)}" + " ".join(tokens)
            events.append(
                f"Dialogue: 0,{_ts(group.start)},{_ts(group.end + 0.14)},"
                f"Main,,0,0,0,,{text}")
        return events

    @staticmethod
    def _render_srt(groups: list[CaptionGroup]) -> str:
        """Plain SRT for the YouTube captions track (no styling, real casing)."""
        lines: list[str] = []
        for i, group in enumerate(groups, start=1):
            text = " ".join(w.text for w in group.words).strip()
            lines += [str(i),
                      f"{_srt_ts(group.start)} --> {_srt_ts(group.end + 0.1)}",
                      text, ""]
        return "\n".join(lines)
