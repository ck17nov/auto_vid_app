"""Video composition engine (spec section 14).

Two-pass design, chosen for robustness and speed:

  Pass A - one near-lossless clip per scene (Ken Burns motion applied).
           Independent per scene, so it parallelises across cores and a single
           bad image cannot abort the whole render.
  Pass B - ONE ffmpeg run that cross-fades the clips, burns the animated
           captions, mixes voice + music + SFX with side-chain ducking, and
           does the single final encode.

Doing it as one giant filter_complex is possible but a 12-scene graph with
zoompan + xfade + subtitles is fragile and impossible to debug; doing it in
three passes would re-encode the video twice and cost quality. Two passes is
the balance: exactly one lossy video encode.

Long-form: past `video.render_segment_max` clips, Pass B is split into a
batched pre-stitch plus one final pass. This is not an extra lossy generation -
the segments are near-lossless intermediates and the delivery encode still
happens exactly once. It exists because one ffmpeg call cannot take hundreds of
inputs.

Timeline maths: the audio track is authoritative. Scene visual lengths are
padded so that each cross-fade is centred on its narration boundary and the
total video length equals the total audio length exactly - see `_clip_lengths`.
"""
from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from ..core.config import Config
from ..core.logging import log_event
from ..core.models import Scene
from ..core.util import (CommandError, ensure_dir, ffmpeg_bin, probe_duration,
                         probe_json, run)
from .fonts import FONT_DIR

# Motion styles. Every scene moves: a static frame is the single biggest
# retention killer in a Short (spec section 15).
MOTION_CYCLE = ["zoom_in", "pan_right", "zoom_out", "pan_left",
                "zoom_in", "pan_up", "zoom_out", "pan_down"]

TRANSITIONS = ["fade", "fade", "smoothleft", "fade", "slideup", "fade"]


@dataclass
class SceneTiming:
    index: int
    image: Path
    duration: float          # narration span this scene owns (incl. trailing gap)
    motion: str = "zoom_in"


@dataclass
class RenderResult:
    video: Path
    duration: float
    width: int
    height: int
    fps: int
    clips: list[Path] = field(default_factory=list)
    audio_stats: dict = field(default_factory=dict)


class VideoComposer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fps = int(cfg.get("video.default_fps", 30))
        self.crf = int(cfg.get("video.crf", 19))
        self.preset = str(cfg.get("video.preset", "medium"))
        self.audio_bitrate = str(cfg.get("video.audio_bitrate", "192k"))
        self.transition = str(cfg.get("video.transition", "fade"))
        self.transition_dur = float(cfg.get("video.transition_duration", 0.35))
        self.kenburns = bool(cfg.get("video.kenburns", True))
        # Colour grade comes from the active style template (spec section 45).
        self.contrast = float(cfg.get("video.contrast", 1.045))
        self.saturation = float(cfg.get("video.saturation", 1.07))
        self.target_lufs = float(cfg.get("tts.target_lufs", -14.0))
        # Above this many clips the xfade chain is rendered in segments - see
        # finalize(). One ffmpeg call cannot take 300 inputs: Windows caps a
        # command line at 32,767 characters and each input costs a path plus a
        # filter node, so a long-form video would fail to launch at all.
        self.segment_max = max(4, int(cfg.get("video.render_segment_max", 40)))
        # How many per-scene clips to encode at once. This used to be a
        # hard-coded 3, which is fine on a laptop and actively harmful on the
        # 2-core Ampere instance the Oracle free tier now gives you: three
        # concurrent x264 encodes, each internally threaded, just thrash.
        # Leave one core for the API so the phone still gets responses while a
        # render is running.
        configured = int(cfg.get("video.render_parallel", 0))
        if configured > 0:
            self.render_parallel = configured
        else:
            self.render_parallel = max(1, min(4, (os.cpu_count() or 2) - 1))

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------
    def resolution(self, video_format: str) -> tuple[int, int]:
        key = ("video.longform_resolution" if video_format == "LONGFORM"
               else "video.default_resolution")
        raw = str(self.cfg.get(key, "1080x1920"))
        try:
            w, h = (int(x) for x in raw.lower().split("x"))
        except ValueError:
            w, h = (1920, 1080) if video_format == "LONGFORM" else (1080, 1920)
        # H.264 requires even dimensions.
        return w - (w % 2), h - (h % 2)

    # ------------------------------------------------------------------
    # Pass A: per-scene motion clips
    # ------------------------------------------------------------------
    def _clip_lengths(self, durations: list[float]) -> list[float]:
        """Pad each clip so cross-fades are centred and total length is exact.

        With n clips and transition T, an xfade chain outputs
        sum(L_i) - (n-1)*T.  Setting L_0 = d_0 + T/2, L_last = d_last + T/2 and
        L_i = d_i + T in between makes the output length exactly sum(d_i) and
        places each transition symmetrically across its scene boundary.
        """
        n = len(durations)
        t = self.transition_dur
        if n == 1:
            return [durations[0]]
        out: list[float] = []
        for i, d in enumerate(durations):
            if i == 0 or i == n - 1:
                out.append(d + t / 2.0)
            else:
                out.append(d + t)
        return out

    def _motion_filter(self, motion: str, frames: int, w: int, h: int) -> str:
        """zoompan expression with smoothstep easing.

        Source images are rendered 1.18x the frame, so zoom 1.0 shows the whole
        image and zoom 1.18 is a native-resolution crop: the pan never
        interpolates beyond the real pixels, which keeps edges sharp.
        """
        n = max(frames - 1, 1)
        # p = linear progress, e = eased progress (smoothstep)
        p = f"(on/{n})"
        e = f"({p}*{p}*(3-2*{p}))"
        zc = "iw/2-(iw/zoom/2)"
        yc = "ih/2-(ih/zoom/2)"

        if not self.kenburns:
            return f"zoompan=z=1.09:x='{zc}':y='{yc}':d=1:s={w}x{h}:fps={self.fps}"

        if motion == "zoom_in":
            z, x, y = f"'1.01+0.15*{e}'", f"'{zc}'", f"'{yc}'"
        elif motion == "zoom_out":
            z, x, y = f"'1.16-0.15*{e}'", f"'{zc}'", f"'{yc}'"
        elif motion == "pan_right":
            z, x, y = "'1.11'", f"'(iw-iw/zoom)*{e}'", f"'{yc}'"
        elif motion == "pan_left":
            z, x, y = "'1.11'", f"'(iw-iw/zoom)*(1-{e})'", f"'{yc}'"
        elif motion == "pan_up":
            z, x, y = "'1.11'", f"'{zc}'", f"'(ih-ih/zoom)*(1-{e})'"
        elif motion == "pan_down":
            z, x, y = "'1.11'", f"'{zc}'", f"'(ih-ih/zoom)*{e}'"
        else:
            z, x, y = f"'1.02+0.12*{e}'", f"'{zc}'", f"'{yc}'"

        return (f"zoompan=z={z}:x={x}:y={y}:d=1:s={w}x{h}:fps={self.fps}")

    def render_scene_clips(self, timings: list[SceneTiming], out_dir: Path,
                           w: int, h: int,
                           parallel: int | None = None) -> list[Path]:
        ensure_dir(out_dir)
        if parallel is None:
            parallel = self.render_parallel
        lengths = self._clip_lengths([t.duration for t in timings])

        def one(args: tuple[SceneTiming, float]) -> Path:
            timing, length = args
            frames = max(int(round(length * self.fps)), 2)
            target = out_dir / f"clip_{timing.index:02d}.mp4"
            # Slight oversize + lanczos guarantees zoompan has real pixels.
            src_w, src_h = int(w * 1.18) & ~1, int(h * 1.18) & ~1
            vf = (f"scale={src_w}:{src_h}:force_original_aspect_ratio=increase:"
                  f"flags=lanczos,crop={src_w}:{src_h},setsar=1,"
                  + self._motion_filter(timing.motion, frames, w, h)
                  + ",format=yuv420p")
            run([ffmpeg_bin(), "-y", "-loglevel", "error",
                 "-loop", "1", "-framerate", str(self.fps),
                 "-t", f"{length:.3f}", "-i", str(timing.image),
                 "-vf", vf, "-frames:v", str(frames),
                 "-c:v", "libx264", "-crf", "14", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", "-an", str(target)], timeout=1200)
            return target

        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            clips = list(pool.map(one, list(zip(timings, lengths))))
        log_event("VIDEO", "scene clips rendered", clips=len(clips),
                  transition=f"{self.transition_dur:.2f}s",
                  parallel=parallel, cores=os.cpu_count())
        return clips

    # ------------------------------------------------------------------
    # Audio loudness measurement (two-pass loudnorm)
    # ------------------------------------------------------------------
    def _measure_loudness(self, path: Path) -> dict[str, str]:
        """First loudnorm pass. Two-pass is materially more accurate than the
        single-pass dynamic mode, and audio-only analysis is cheap."""
        try:
            proc = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path),
                        "-af", f"loudnorm=I={self.target_lufs}:TP=-1.5:LRA=11:print_format=json",
                        "-f", "null", "-"], timeout=600, check=False)
        except CommandError:
            return {}
        text = (proc.stderr or "") + (proc.stdout or "")
        start = text.rfind("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return {}
        import json
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return {}
        return {k: str(v) for k, v in data.items()}

    def mix_audio(self, voice: Path, out_path: Path, *, music: Path | None = None,
                  sfx: Path | None = None) -> tuple[Path, dict]:
        """Voice + music + SFX with side-chain ducking, then loudness targeting.

        The music is keyed off the voice through `sidechaincompress`, so the pad
        drops whenever narration is present and fills the gaps between lines.
        That is what keeps the voice intelligible without making the bed
        inaudible (spec section 43).
        """
        music_db = float(self.cfg.get("video.music_volume_db", -9))
        duck_db = float(self.cfg.get("video.duck_music_db", -11))
        sfx_db = float(self.cfg.get("video.sfx_volume_db", -4))

        inputs: list[str] = ["-i", str(voice)]
        chains: list[str] = [
            # Voice: to stereo, gentle de-ess/presence shaping, soft limiting.
            "[0:a]aformat=channel_layouts=stereo:sample_rates=48000,"
            "equalizer=f=3000:t=q:w=1.4:g=2.0,"        # presence/intelligibility
            "equalizer=f=220:t=q:w=1.2:g=-1.5,"        # reduce boxiness
            "acompressor=threshold=0.09:ratio=3:attack=12:release=220:makeup=1.6,"
            "alimiter=limit=0.95:level=disabled[voice]"
        ]
        mix_labels = ["[voice]"]
        idx = 1

        if music is not None and music.exists():
            inputs += ["-i", str(music)]
            chains.append(
                f"[{idx}:a]aformat=channel_layouts=stereo:sample_rates=48000,"
                f"volume={music_db:.1f}dB[music_raw]")
            # Side-chain key must share the music's layout.
            chains.append("[voice]asplit=2[voice_out][voice_key]")
            chains.append(
                "[music_raw][voice_key]sidechaincompress="
                "threshold=0.035:ratio=12:attack=6:release=420:"
                "makeup=1:level_sc=1[music_ducked]")
            # Extra static trim while ducked, for headroom under the voice.
            chains.append(f"[music_ducked]volume={max(duck_db + 11, -18):.1f}dB[music]")
            mix_labels = ["[voice_out]", "[music]"]
            idx += 1

        if sfx is not None and sfx.exists():
            inputs += ["-i", str(sfx)]
            chains.append(
                f"[{idx}:a]aformat=channel_layouts=stereo:sample_rates=48000,"
                f"volume={sfx_db:.1f}dB[sfx]")
            mix_labels.append("[sfx]")
            idx += 1

        if len(mix_labels) > 1:
            chains.append(
                f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:"
                f"normalize=0:dropout_transition=0:duration=first[mixed]")
        else:
            chains.append(f"{mix_labels[0]}anull[mixed]")

        raw_mix = out_path.with_name(out_path.stem + "_premaster.wav")
        chains.append("[mixed]alimiter=limit=0.97:level=disabled[out]")
        run([ffmpeg_bin(), "-y", "-loglevel", "error", *inputs,
             "-filter_complex", ";".join(chains), "-map", "[out]",
             "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(raw_mix)],
            timeout=900)

        # Two-pass loudness normalisation to the YouTube-friendly target.
        measured = self._measure_loudness(raw_mix)
        norm = f"loudnorm=I={self.target_lufs}:TP=-1.5:LRA=11"
        if measured.get("input_i"):
            norm += (f":measured_I={measured['input_i']}"
                     f":measured_TP={measured['input_tp']}"
                     f":measured_LRA={measured['input_lra']}"
                     f":measured_thresh={measured['input_thresh']}"
                     f":offset={measured.get('target_offset', '0.0')}"
                     f":linear=true")
        run([ffmpeg_bin(), "-y", "-loglevel", "error", "-i", str(raw_mix),
             "-af", f"{norm},alimiter=limit=0.985:level=disabled",
             "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(out_path)],
            timeout=900)
        raw_mix.unlink(missing_ok=True)

        stats = self._measure_loudness(out_path)
        log_event("AUDIO", "master mix complete",
                  lufs=stats.get("input_i", "?"), tp=stats.get("input_tp", "?"),
                  music="yes" if music else "no", sfx="yes" if sfx else "no")
        return out_path, stats

    # ------------------------------------------------------------------
    # Pass B: stitch + captions + final encode
    # ------------------------------------------------------------------
    def _xfade_chain(self, clips: list[Path], lengths: list[float]) -> tuple[str, str]:
        """Build the xfade filter chain and return (chain, final_label)."""
        if len(clips) == 1:
            return "[0:v]null[vout]", "[vout]"
        t = self.transition_dur
        parts: list[str] = []
        current = "[0:v]"
        cursor = 0.0
        for i in range(1, len(clips)):
            cursor += lengths[i - 1] - t
            kind = (self.transition if self.transition != "auto"
                    else TRANSITIONS[i % len(TRANSITIONS)])
            label = f"[vx{i}]"
            parts.append(f"{current}[{i}:v]xfade=transition={kind}:"
                         f"duration={t:.3f}:offset={max(cursor, 0.0):.3f}{label}")
            current = label
        return ";".join(parts), current

    def _render_segments(self, clips: list[Path], lengths: list[float],
                         work_dir: Path) -> tuple[list[Path], list[float]]:
        """Pre-stitch clips in batches, returning (segment files, durations).

        Hierarchical cross-fading is duration-exact, which is why it is safe to
        do. With n clips, transition T and per-clip padded lengths L, a flat
        chain outputs sum(L) - (n-1)*T. Splitting into S segments gives each
        segment sum(L in segment) - (count-1)*T, and cross-fading the S
        segments together removes a further (S-1)*T. The two removals add up to
        exactly (n-1)*T, so the total is unchanged and every transition is
        still centred on its scene boundary.

        Segments are encoded near-lossless (CRF 16) because they are an
        intermediate: captions, colour grade and the real encode all happen in
        the single final pass, so the video is still only lossy-encoded once at
        delivery quality.
        """
        ensure_dir(work_dir)
        segments: list[Path] = []
        durations: list[float] = []
        step = self.segment_max
        for seg_no, start in enumerate(range(0, len(clips), step)):
            group = clips[start:start + step]
            group_lengths = lengths[start:start + step]
            target = work_dir / f"segment_{seg_no:03d}.mp4"
            chain, vlabel = self._xfade_chain(group, group_lengths)
            inputs: list[str] = []
            for c in group:
                inputs += ["-i", str(c)]
            run([ffmpeg_bin(), "-y", "-loglevel", "error", *inputs,
                 "-filter_complex", f"{chain};{vlabel}format=yuv420p[vseg]",
                 "-map", "[vseg]",
                 "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", "-an", str(target)], timeout=3600)
            expected = sum(group_lengths) - (len(group) - 1) * self.transition_dur
            actual = probe_duration(target)
            if abs(actual - expected) > 0.12:
                log_event("VIDEO", "segment duration drifted",
                          segment=seg_no, expected=f"{expected:.3f}",
                          actual=f"{actual:.3f}")
            segments.append(target)
            durations.append(actual)
        log_event("VIDEO", "clips pre-stitched into segments",
                  clips=len(clips), segments=len(segments),
                  per_segment=step)
        return segments, durations

    def finalize(self, clips: list[Path], durations: list[float], audio: Path,
                 ass_file: Path | None, out_path: Path, w: int, h: int) -> RenderResult:
        lengths = self._clip_lengths(durations)

        # Long-form: pre-stitch in batches so the final call has a handful of
        # inputs instead of hundreds. Duration is preserved exactly (see
        # _render_segments), so captions stay in sync.
        segment_dir: Path | None = None
        if len(clips) > self.segment_max:
            segment_dir = out_path.parent / "segments"
            clips, lengths = self._render_segments(clips, lengths, segment_dir)

        chain, vlabel = self._xfade_chain(clips, lengths)

        filters = [chain]
        # Caption burn-in. fontsdir keeps rendering identical across machines.
        if ass_file is not None and ass_file.exists():
            ass_arg = _ffmpeg_path(ass_file)
            fonts_arg = _ffmpeg_path(FONT_DIR)
            filters.append(
                f"{vlabel}subtitles=filename='{ass_arg}':fontsdir='{fonts_arg}'"
                f":alpha=1[vsub]")
            vlabel = "[vsub]"
        # Mild contrast/saturation lift reads better on phone screens.
        #
        # Then a real colour-range conversion, which matters more than it looks.
        # The source images are JPEG, so everything upstream is FULL range
        # (yuvj420p). Delivered untouched, ffprobe reported the finished file as
        # `pix_fmt=yuvj420p, color_range=pc, color_space=bt470bg` - full range
        # tagged as PAL. Any player that honours the range tag crushes blacks
        # and clips highlights, and bt470bg is simply the wrong matrix for HD.
        # `scale=in_range=pc:out_range=tv` remaps the values properly rather
        # than just relabelling them, and the encoder flags below tag BT.709.
        filters.append(
            f"{vlabel}eq=contrast={self.contrast:.3f}:"
            f"saturation={self.saturation:.3f},"
            f"scale=in_range=pc:out_range=tv,format=yuv420p[vfinal]")

        inputs: list[str] = []
        for c in clips:
            inputs += ["-i", str(c)]
        inputs += ["-i", str(audio)]
        audio_index = len(clips)

        cmd = [ffmpeg_bin(), "-y", "-loglevel", "error", *inputs,
               "-filter_complex", ";".join(filters),
               "-map", "[vfinal]", "-map", f"{audio_index}:a",
               "-c:v", "libx264", "-crf", str(self.crf), "-preset", self.preset,
               "-profile:v", "high", "-level", "4.2",
               "-pix_fmt", "yuv420p",
               # Tag what we actually produced: limited-range BT.709, the
               # standard for HD delivery and what YouTube assumes.
               "-color_range", "tv", "-colorspace", "bt709",
               "-color_primaries", "bt709", "-color_trc", "bt709",
               # The colour flags above set the container/stream metadata;
               # writing them into the x264 VUI too means a decoder reading
               # only the bitstream still gets it right.
               "-x264-params",
               ("keyint=60:min-keyint=30:scenecut=40:"
                "colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=off"),
               "-c:a", "aac", "-b:a", self.audio_bitrate, "-ar", "48000", "-ac", "2",
               "-movflags", "+faststart", "-shortest", str(out_path)]
        # A flat hour-long timeout is fine for a Short and far too tight for a
        # 40-minute video: allow 20x realtime plus a floor, capped at 6 hours.
        total = sum(lengths)
        run(cmd, timeout=int(min(21600, max(3600, total * 20))))

        if segment_dir is not None:
            for seg in segment_dir.glob("segment_*.mp4"):
                seg.unlink(missing_ok=True)
            with suppress(OSError):
                segment_dir.rmdir()

        dur = probe_duration(out_path)
        log_event("VIDEO", "final render complete", seconds=f"{dur:.2f}",
                  resolution=f"{w}x{h}", crf=self.crf,
                  size_mb=f"{out_path.stat().st_size / 1e6:.1f}")
        return RenderResult(video=out_path, duration=dur, width=w, height=h,
                            fps=self.fps, clips=clips)

    # ------------------------------------------------------------------
    def probe(self, path: Path) -> dict:
        return probe_json(path)


def _ffmpeg_path(path: Path) -> str:
    """Escape a Windows path for use inside an ffmpeg filter argument.

    Filters parse ':' and '\\' themselves, so C:\\a\\b.ass must become
    C\\:/a/b.ass or ffmpeg reads the drive letter as an option separator.
    """
    text = str(path).replace("\\", "/")
    return text.replace(":", "\\:", 1) if len(text) > 1 and text[1] == ":" else text


def assign_motion(scenes: list[Scene],
                  cycle: list[str] | None = None) -> None:
    """Give consecutive scenes different motion so the video never feels static.

    The cycle comes from the active style template, so MYSTERY holds and pushes
    while FAST_FACTS whips between pans.
    """
    order = cycle or MOTION_CYCLE
    if not order:
        order = MOTION_CYCLE
    for i, scene in enumerate(scenes):
        if not scene.motion or scene.motion == "zoom_in":
            scene.motion = order[i % len(order)]


def cleanup_clips(clips: list[Path]) -> None:
    for c in clips:
        try:
            c.unlink(missing_ok=True)
        except OSError:
            pass
    if clips:
        parent = clips[0].parent
        if parent.name == "clips" and not any(parent.iterdir()):
            shutil.rmtree(parent, ignore_errors=True)
