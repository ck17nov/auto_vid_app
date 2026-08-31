"""Quality gate (spec section 23).

Runs every pre-publish check, returns a 0-100 score plus hard blockers.
A video with ANY blocker never uploads regardless of score.

Checks are real measurements (ffprobe, silence detection, audio statistics),
not assertions - the whole point is to catch a silent audio track or a
half-written file before it becomes a public upload.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.logging import log_event
from ..core.models import QualityReport, Script, VideoMetadata
from ..core.niche import NicheProfile
from ..core.util import CommandError, ffmpeg_bin, probe_json, run

# Each check contributes `weight` to the score. `blocking` checks veto upload.
@dataclass
class Check:
    name: str
    weight: float
    blocking: bool = False


CHECKS: list[Check] = [
    Check("file_exists", 6.0, blocking=True),
    Check("playable", 8.0, blocking=True),
    Check("resolution", 6.0, blocking=True),
    Check("aspect_ratio", 5.0),
    Check("frame_rate", 3.0),
    Check("audio_present", 8.0, blocking=True),
    Check("audio_not_silent", 8.0, blocking=True),
    Check("audio_loudness", 5.0),
    Check("no_long_silence", 6.0),
    # Blocking. It used to be advisory, and a real run produced a 25.86s video
    # for a 45s request - 43% off against a 25% tolerance - which still scored
    # 95/100 and passed. Presenting that for approval as a "45 second video" is
    # the gate failing at its one job. The tolerance is already generous; going
    # outside it means the video is not the thing that was asked for.
    Check("duration_correct", 7.0, blocking=True),
    Check("subtitles_present", 5.0),
    Check("subtitles_aligned", 5.0),
    Check("thumbnail_present", 4.0),
    Check("title_present", 5.0, blocking=True),
    Check("description_present", 4.0),
    Check("tags_present", 2.0),
    Check("file_size", 3.0),
    Check("encoding_compatible", 4.0, blocking=True),
    Check("originality", 8.0, blocking=True),
    Check("factual_risk", 6.0),
    Check("policy_risk", 6.0, blocking=True),
    Check("kids_compliance", 6.0, blocking=True),
]

# YouTube hard limits.
YOUTUBE_MAX_BYTES = 128 * 1024 * 1024 * 1024      # 128 GB
YOUTUBE_MAX_TITLE = 100
YOUTUBE_MAX_DESC = 5000
SHORTS_MAX_SECONDS = 180

# Content that must never be auto-published (spec section 47).
PROHIBITED_PATTERNS: list[tuple[str, str]] = [
    (r"\b(kill your ?self|how to (make|build) a bomb|how to buy (a )?gun illegally)\b",
     "dangerous instruction"),
    (r"\b(n[i1]gg(er|a)|f[a@]gg?ot|k[i1]ke|ch[i1]nk)\b", "hate speech"),
    (r"\b(child|minor|teen)\b.{0,24}\b(porn|nude|sexual)\b", "CSAE risk"),
    (r"\b(buy (views|subscribers)|sub4sub|view ?bot)\b", "platform manipulation"),
    (r"\b(drink bleach|eat tide pods|hold your breath until)\b",
     "dangerous challenge"),
]

KIDS_PROHIBITED: list[tuple[str, str]] = [
    (r"\b(kill|blood|die|dead|death|gun|knife|weapon|war)\b", "violence"),
    (r"\b(scary|terrifying|horror|nightmare|monster attack)\b", "frightening"),
    (r"\b(damn|hell|stupid|idiot|shut up)\b", "inappropriate language"),
    (r"\b(sexy|kiss|dating|girlfriend|boyfriend)\b", "romance"),
    (r"\b(beer|wine|vodka|smoking|vape|casino|bet)\b", "regulated goods"),
    (r"\b(click the link|buy now|subscribe or)\b", "commercial pressure"),
    (r"\b(try this at home|do this yourself)\b.{0,30}\b(fire|electric|height)\b",
     "unsafe imitation"),
]


class QualityGate:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.minimum = float(cfg.get("quality.minimum_score", 80))
        self.max_silence = float(cfg.get("quality.max_silence_seconds", 1.6))
        self.min_rms_db = float(cfg.get("quality.min_audio_rms_db", -40.0))
        self.duration_tolerance = float(
            cfg.get("quality.duration_tolerance_pct", 25)) / 100.0

    # ------------------------------------------------------------------
    def evaluate(self, *, video: Path | None, metadata: VideoMetadata,
                 script: Script, profile: NicheProfile,
                 subtitle: Path | None = None,
                 thumbnail: Path | None = None,
                 target_duration: float = 45.0,
                 video_format: str = "SHORT",
                 originality: Any = None,
                 factcheck: Any = None) -> QualityReport:
        report = QualityReport()
        results: dict[str, dict[str, Any]] = {}

        def record(name: str, passed: bool, detail: str,
                   partial: float | None = None) -> None:
            results[name] = {"passed": passed, "detail": detail,
                             "partial": 1.0 if passed else (partial or 0.0)}

        # ---- video file ------------------------------------------------
        probe: dict[str, Any] = {}
        vstream: dict[str, Any] = {}
        astream: dict[str, Any] = {}
        exists = bool(video and video.exists() and video.stat().st_size > 10_000)
        record("file_exists", exists,
               f"{video}" if exists else "video file missing or truncated")

        if exists:
            probe = probe_json(video)  # type: ignore[arg-type]
            streams = probe.get("streams", []) or []
            vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
            astream = next((s for s in streams if s.get("codec_type") == "audio"), {})
            fmt = probe.get("format", {}) or {}
            playable = bool(vstream and fmt.get("duration"))
            record("playable", playable,
                   f"container={fmt.get('format_name', '?')} "
                   f"codec={vstream.get('codec_name', 'none')}")

            width = int(vstream.get("width") or 0)
            height = int(vstream.get("height") or 0)
            expected = self._expected_resolution(video_format)
            res_ok = (width, height) == expected
            record("resolution", res_ok,
                   f"{width}x{height} (expected {expected[0]}x{expected[1]})",
                   partial=0.5 if width >= 720 and height >= 720 else 0.0)

            ratio = (width / height) if height else 0.0
            want_ratio = expected[0] / expected[1]
            ratio_ok = abs(ratio - want_ratio) < 0.02
            record("aspect_ratio", ratio_ok, f"{ratio:.3f} (expected {want_ratio:.3f})")

            fps = _parse_fps(vstream.get("r_frame_rate", "0/0"))
            want_fps = float(self.cfg.get("video.default_fps", 30))
            record("frame_rate", abs(fps - want_fps) < 1.0,
                   f"{fps:.2f} fps (expected {want_fps:.0f})",
                   partial=0.6 if fps >= 24 else 0.0)

            codec_ok = (vstream.get("codec_name") == "h264"
                        and vstream.get("pix_fmt") in {"yuv420p", "yuvj420p"}
                        and astream.get("codec_name") == "aac")
            record("encoding_compatible", codec_ok,
                   f"video={vstream.get('codec_name')}/{vstream.get('pix_fmt')} "
                   f"audio={astream.get('codec_name')}")

            size = int(fmt.get("size") or video.stat().st_size)  # type: ignore[union-attr]
            size_ok = 50_000 < size < YOUTUBE_MAX_BYTES
            record("file_size", size_ok, f"{size / 1e6:.1f} MB")

            duration = float(fmt.get("duration") or 0.0)
            drift = abs(duration - target_duration) / max(target_duration, 1.0)
            duration_ok = drift <= self.duration_tolerance
            if video_format == "SHORT" and duration > SHORTS_MAX_SECONDS:
                duration_ok = False
            record("duration_correct", duration_ok,
                   f"{duration:.2f}s vs target {target_duration:.0f}s "
                   f"({drift * 100:.0f}% off, tolerance "
                   f"{self.duration_tolerance * 100:.0f}%)",
                   partial=max(0.0, 1.0 - drift))

            # ---- audio -------------------------------------------------
            has_audio = bool(astream)
            record("audio_present", has_audio,
                   f"channels={astream.get('channels', 0)} "
                   f"rate={astream.get('sample_rate', 0)}")

            if has_audio:
                stats = self._audio_stats(video)  # type: ignore[arg-type]
                rms = stats.get("mean_volume", -99.0)
                peak = stats.get("max_volume", -99.0)
                not_silent = rms > self.min_rms_db and peak > -20.0
                record("audio_not_silent", not_silent,
                       f"mean {rms:.1f} dB, peak {peak:.1f} dB "
                       f"(floor {self.min_rms_db:.0f} dB)")

                target_lufs = float(self.cfg.get("tts.target_lufs", -14.0))
                lufs = stats.get("lufs")
                if lufs is None:
                    record("audio_loudness", False, "loudness measurement failed",
                           partial=0.4)
                else:
                    off = abs(lufs - target_lufs)
                    record("audio_loudness", off <= 1.5,
                           f"{lufs:.2f} LUFS (target {target_lufs:.1f})",
                           partial=max(0.0, 1.0 - off / 4.0))

                silences = self._silences(video)  # type: ignore[arg-type]
                longest = max((d for _, d in silences), default=0.0)
                record("no_long_silence", longest <= self.max_silence,
                       f"longest gap {longest:.2f}s "
                       f"(limit {self.max_silence:.1f}s), {len(silences)} gaps",
                       partial=max(0.0, 1.0 - (longest - self.max_silence) / 3.0))
            else:
                record("audio_not_silent", False, "no audio stream")
                record("audio_loudness", False, "no audio stream")
                record("no_long_silence", False, "no audio stream")
        else:
            for name in ("playable", "resolution", "aspect_ratio", "frame_rate",
                         "encoding_compatible", "file_size", "duration_correct",
                         "audio_present", "audio_not_silent", "audio_loudness",
                         "no_long_silence"):
                record(name, False, "no video file")
            duration = 0.0

        # ---- subtitles --------------------------------------------------
        sub_ok = bool(subtitle and subtitle.exists() and subtitle.stat().st_size > 20)
        record("subtitles_present", sub_ok,
               f"{subtitle.name if subtitle else 'none'}")
        if sub_ok:
            aligned, detail = self._subtitles_aligned(subtitle, duration)  # type: ignore[arg-type]
            record("subtitles_aligned", aligned, detail)
        else:
            record("subtitles_aligned", False, "no subtitle file")

        # ---- thumbnail --------------------------------------------------
        if video_format == "LONGFORM":
            thumb_ok = bool(thumbnail and thumbnail.exists()
                            and thumbnail.stat().st_size < 2 * 1024 * 1024)
            record("thumbnail_present", thumb_ok,
                   f"{thumbnail.name if thumbnail else 'missing'}")
        else:
            # Shorts do not require a custom thumbnail.
            record("thumbnail_present", True,
                   "not required for Shorts" if not thumbnail else thumbnail.name)

        # ---- metadata ---------------------------------------------------
        title = (metadata.title or "").strip()
        record("title_present", bool(title) and len(title) <= YOUTUBE_MAX_TITLE,
               f"{len(title)} chars")
        desc = (metadata.description or "").strip()
        record("description_present",
               len(desc) >= 40 and len(desc) <= YOUTUBE_MAX_DESC,
               f"{len(desc)} chars")
        record("tags_present", bool(metadata.tags), f"{len(metadata.tags)} tags")

        # ---- originality -------------------------------------------------
        orig_ok = True
        orig_detail = "no originality check supplied"
        if originality is not None:
            orig_ok = bool(getattr(originality, "passed", True))
            orig_detail = (f"vs research {getattr(originality, 'max_similarity', 0):.2f}, "
                           f"vs own {getattr(originality, 'self_similarity', 0):.2f}")
            if not orig_ok:
                orig_detail += " | " + "; ".join(
                    getattr(originality, "findings", [])[:2])
        record("originality", orig_ok, orig_detail)
        report.originality_ok = orig_ok

        # ---- factual risk ------------------------------------------------
        fact_risk = getattr(factcheck, "risk", "low") if factcheck else "low"
        fact_ok = fact_risk == "low"
        record("factual_risk", fact_ok,
               f"risk={fact_risk}, "
               f"flags={len(getattr(factcheck, 'flagged', []) or [])}",
               partial={"medium": 0.5, "high": 0.0}.get(fact_risk, 1.0))

        # ---- policy ------------------------------------------------------
        haystack = f"{title}\n{desc}\n{script.script}"
        policy_hits = [label for pattern, label in PROHIBITED_PATTERNS
                       if re.search(pattern, haystack, re.I)]
        record("policy_risk", not policy_hits,
               "clean" if not policy_hits else f"matched: {', '.join(policy_hits)}")

        # ---- kids compliance ---------------------------------------------
        if profile.made_for_kids or metadata.made_for_kids:
            kid_hits = [label for pattern, label in KIDS_PROHIBITED
                        if re.search(pattern, haystack, re.I)]
            consistent = profile.made_for_kids == metadata.made_for_kids
            if not consistent:
                kid_hits.append("madeForKids flag does not match the niche profile")
            record("kids_compliance", not kid_hits,
                   "clean" if not kid_hits else f"matched: {', '.join(kid_hits)}")
        else:
            record("kids_compliance", True, "not child-directed")

        # ---- aggregate ---------------------------------------------------
        earned = 0.0
        total_weight = 0.0
        for check in CHECKS:
            outcome = results.get(check.name)
            if outcome is None:
                continue
            total_weight += check.weight
            earned += check.weight * float(outcome["partial"])
            report.checks.append({
                "name": check.name, "passed": outcome["passed"],
                "blocking": check.blocking, "weight": check.weight,
                "detail": outcome["detail"],
            })
            if not outcome["passed"]:
                message = f"{check.name}: {outcome['detail']}"
                if check.blocking:
                    report.blockers.append(message)
                else:
                    report.warnings.append(message)

        report.score = round((earned / total_weight * 100) if total_weight else 0.0, 1)
        report.policy_risk = ("high" if policy_hits else
                              "medium" if fact_risk != "low" else "low")
        report.passed = not report.blockers and report.score >= self.minimum

        log_event("QUALITY", f"{report.score:.0f}/100",
                  passed=report.passed, blockers=len(report.blockers),
                  warnings=len(report.warnings), minimum=self.minimum)
        for blocker in report.blockers:
            log_event("QUALITY", f"BLOCKER {blocker}")
        return report

    # ------------------------------------------------------------------
    def _expected_resolution(self, video_format: str) -> tuple[int, int]:
        key = ("video.longform_resolution" if video_format == "LONGFORM"
               else "video.default_resolution")
        raw = str(self.cfg.get(key, "1080x1920"))
        try:
            w, h = (int(x) for x in raw.lower().split("x"))
            return w, h
        except ValueError:
            return (1920, 1080) if video_format == "LONGFORM" else (1080, 1920)

    def _audio_stats(self, media: Path) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            proc = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(media),
                        "-af", "volumedetect", "-f", "null", "-"],
                       timeout=600, check=False)
            text = proc.stderr or ""
            for key, pattern in (("mean_volume", r"mean_volume:\s*(-?[\d.]+) dB"),
                                 ("max_volume", r"max_volume:\s*(-?[\d.]+) dB")):
                m = re.search(pattern, text)
                if m:
                    out[key] = float(m.group(1))
        except CommandError:
            pass
        try:
            proc = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(media),
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       timeout=600, check=False)
            text = (proc.stderr or "")
            m = re.search(r'"input_i"\s*:\s*"(-?[\d.]+)"', text)
            if m:
                out["lufs"] = float(m.group(1))
        except CommandError:
            pass
        return out

    def _silences(self, media: Path) -> list[tuple[float, float]]:
        """Detect silent gaps. Returns [(start, duration)]."""
        try:
            proc = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(media),
                        "-af", f"silencedetect=noise=-38dB:d={self.max_silence:.2f}",
                        "-f", "null", "-"], timeout=600, check=False)
        except CommandError:
            return []
        text = proc.stderr or ""
        out: list[tuple[float, float]] = []
        for m in re.finditer(
                r"silence_start:\s*(-?[\d.]+).*?silence_duration:\s*([\d.]+)",
                text, re.S):
            out.append((float(m.group(1)), float(m.group(2))))
        return out

    def _subtitles_aligned(self, subtitle: Path,
                           video_duration: float) -> tuple[bool, str]:
        """Last cue must land inside the video, first cue must start early."""
        try:
            text = subtitle.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"unreadable: {exc}"
        times = re.findall(
            r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
            r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})", text)
        if not times:
            return False, "no cues found"
        def to_s(h, m, s, ms):
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000
        first_start = to_s(*times[0][:4])
        last_end = to_s(*times[-1][4:])
        cues = len(times)
        if video_duration <= 0:
            return False, f"{cues} cues but video duration unknown"
        overshoot = last_end - video_duration
        ok = overshoot <= 0.75 and first_start <= 2.0
        return ok, (f"{cues} cues, first at {first_start:.2f}s, "
                    f"last ends {last_end:.2f}s vs video {video_duration:.2f}s")


def _parse_fps(raw: str) -> float:
    try:
        num, _, den = str(raw).partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0
