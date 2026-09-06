"""The end-to-end pipeline (spec sections 21, 22, 24, 36, 49).

Every stage:
  * persists job state to SQLite before and after, so an app crash, a phone
    reboot or a killed worker resumes instead of losing work;
  * writes its artifact to the job directory, which IS the dry-run output
    (research.json, idea.json, script.json, assets/, voice.wav, video.mp4,
    thumbnail.jpg, metadata.json, quality_report.json);
  * is individually retryable with backoff.

Duration honesty: the target length is enforced against MEASURED narration, not
an estimate.  If the synthesised voice overruns, the pipeline re-synthesises at
a corrected speaking rate rather than shipping a "45 second" video that runs 70.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .analytics.collect import AnalyticsCollector, StrategyLearner
from .content.ideas import IdeaGenerator
from .content.llm import LLMRouter
from .content.metadata import MetadataGenerator
from .content.originality import FactChecker, OriginalityChecker
from .content.retention import analyze as analyze_retention, auto_improve
from .content.script import ScriptGenerator
from .core.config import Config, load_config
from .core.db import Database
from .core.logging import log_event, setup_logging
from .core.models import (AutomationRequest, ContentIdea, JobStatus, Mode,
                          QualityReport, ResearchVideo, Script, VideoJob,
                          VideoMetadata)
from .core.niche import build_profile
from .core.util import (clamp, ensure_dir, have_ffmpeg, read_json,
                        safe_write_json, sha1, slugify)
from .quality.gate import QualityGate
from .research.gaps import cluster_videos, find_gaps, research_context_block
from .research.youtube import QuotaGuard, YouTubeResearch
from .thumbnail.generator import ThumbnailGenerator
from .tts.engine import VoiceEngine
from .video.captions import CaptionEngine
from .video.compose import SceneTiming, VideoComposer, assign_motion, cleanup_clips
from .video.music import build_music, build_transition_sfx
from .video.templates import (apply_to_profile, caption_overrides,
                              select_template, video_overrides)
from .visuals.engine import VisualEngine
from .youtube.auth import YouTubeAuth
from .youtube.upload import YouTubeUploader


class PipelineError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage


@dataclass
class PipelineResult:
    job: VideoJob
    artifacts: dict[str, str] = field(default_factory=dict)
    quality: QualityReport | None = None
    uploaded: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.job.status not in (JobStatus.FAILED.value,
                                       JobStatus.REJECTED.value)


class Pipeline:
    def __init__(self, cfg: Config | None = None, db: Database | None = None):
        self.cfg = cfg or load_config()
        self.workspace = self.cfg.workspace
        setup_logging(jsonl=self.workspace / "logs" / "events.jsonl")
        self.db = db or Database(self.workspace / "autotube.db")

        self.router = LLMRouter(
            list(self.cfg.get("content.llm_provider_order",
                              ["groq", "gemini", "ollama", "template"])), self.cfg)
        self.quota = QuotaGuard(self.cfg, self.db)
        self.research_engine = YouTubeResearch(self.cfg, self.db, self.quota)
        self.idea_engine = IdeaGenerator(self.cfg, self.router, self.db)
        self.script_engine = ScriptGenerator(self.cfg, self.router)
        self.metadata_engine = MetadataGenerator(self.cfg, self.router)
        self.voice_engine = VoiceEngine(self.cfg)
        self.visual_engine = VisualEngine(self.cfg)
        self.caption_engine = CaptionEngine(self.cfg)
        self.composer = VideoComposer(self.cfg)
        self.thumbnail_engine = ThumbnailGenerator(self.cfg)
        self.quality_gate = QualityGate(self.cfg)
        self.originality = OriginalityChecker(self.cfg, self.db)
        self.factchecker = FactChecker(self.cfg)
        self.auth = YouTubeAuth(self.cfg)
        self.uploader = YouTubeUploader(self.cfg, self.auth, self.quota)
        self.learner = StrategyLearner(self.cfg, self.db)
        self._motion_cycle: list[str] | None = None

    # ==================================================================
    # Job lifecycle helpers
    # ==================================================================
    def _advance(self, job: VideoJob, status: JobStatus, note: str = "") -> None:
        job.status = status.value
        job.updated_at = time.time()
        if note:
            job.logs.append(f"{time.strftime('%H:%M:%S')} {status.value}: {note}")
        self.db.save_job(job)

    def _job_dir(self, job: VideoJob, request: AutomationRequest) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.created_at))
        name = f"{stamp}_{slugify(request.niche, 24)}_{job.job_id[-6:]}"
        path = ensure_dir(self.workspace / "jobs" / name)
        job.dir = str(path)
        return path

    def _retry(self, stage: str, fn: Callable[[], Any], job: VideoJob) -> Any:
        attempts = int(self.cfg.get("automation.max_retries", 3))
        backoff = float(self.cfg.get("automation.retry_backoff_seconds", 20))
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except Exception as exc:
                last = exc
                job.retry_count += 1
                job.error = f"{stage}: {str(exc)[:400]}"
                self.db.save_job(job)
                if attempt >= attempts:
                    break
                wait = backoff * attempt
                log_event(stage.upper(), "stage failed, retrying",
                          attempt=f"{attempt}/{attempts}", wait=f"{wait:.0f}s",
                          error=str(exc)[:200])
                time.sleep(wait)
        raise PipelineError(stage, str(last))

    # ==================================================================
    # Preconditions (spec section 46: anti-spam)
    # ==================================================================
    def preflight(self, request: AutomationRequest) -> list[str]:
        problems: list[str] = []
        if not have_ffmpeg():
            problems.append(
                "ffmpeg/ffprobe not found on PATH - required for rendering. "
                "See docs/SETUP.md")
        if not self.research_engine.configured:
            message = ("YOUTUBE_API_KEY not set - research cannot run "
                       "(free, no credit card: docs/YOUTUBE_SETUP.md)")
            if self.cfg.dry_run:
                # Spec section 36 wants a dry run to produce every artifact.
                # Blocking here meant you could not see what the pipeline
                # makes until after signing up for an API key - so in dry run
                # this degrades loudly instead. Ideas then come from the
                # structural builder, with no trend evidence behind them.
                log_event("PREFLIGHT", "dry run without research",
                          reason=message,
                          effect="ideas will be structural, not trend-driven")
            else:
                problems.append(message)
        if not self.router.has_real_llm():
            log_event("PREFLIGHT", "no LLM configured - script quality will be "
                                   "degraded to the template builder",
                      hint="set GROQ_API_KEY or GEMINI_API_KEY, or run ollama")
            # The template builder assembles framing lines, and there are only
            # so many honest ones - it tops out around 200 words. That is a
            # whole 45s Short but nowhere near a 10-minute video, and the gap
            # would be papered over by slowing the narration until it failed
            # the duration check after the full render. Say so first instead.
            if request.duration_seconds > 120:
                problems.append(
                    f"a {request.duration_seconds}s video needs a real LLM: the "
                    f"template builder cannot honestly fill more than about "
                    f"two minutes of narration without repeating itself. Set "
                    f"GROQ_API_KEY or GEMINI_API_KEY (both free, no credit "
                    f"card - docs/API_SETUP.md), or request a shorter video.")

        limit = int(self.cfg.get("automation.daily_video_limit", 3))
        since = time.time() - 86400
        made_today = self.db.count_jobs_since(
            since, (JobStatus.PUBLISHED.value, JobStatus.SCHEDULED.value,
                    JobStatus.READY.value))
        if made_today >= limit:
            problems.append(
                f"daily video limit reached ({made_today}/{limit}) - "
                f"raise automation.daily_video_limit to continue")

        similar = self._recent_similarity_run()
        halt_after = int(self.cfg.get("automation.stop_after_similar_videos", 3))
        if similar >= halt_after:
            problems.append(
                f"automation halted: the last {similar} scripts were near-"
                f"identical (>= "
                f"{float(self.cfg.get('automation.duplicate_similarity_threshold', 0.8)) * 100:.0f}% "
                f"similar). This is what mass-produced spam looks like. Review "
                f"the queue, change the niche or keywords, then continue.")
        return problems

    def _recent_similarity_run(self) -> int:
        """How many of the most recent scripts are near-duplicates of each other.

        Spec section 46: if several videos in a row are effectively the same
        video, stop rather than keep publishing them.
        """
        from .core.util import jaccard
        recent = self.db.recent_script_texts(limit=6)
        texts = [t for _, t in recent if t]
        if len(texts) < 2:
            return 0
        threshold = float(
            self.cfg.get("automation.duplicate_similarity_threshold", 0.80))
        run = 1
        for i in range(1, len(texts)):
            if jaccard(texts[i - 1], texts[i], n=4) >= threshold:
                run += 1
            else:
                break
        return run if run >= 2 else 0

    # ==================================================================
    # Stages
    # ==================================================================
    def stage_research(self, job: VideoJob, request: AutomationRequest,
                      profile) -> list[ResearchVideo]:
        self._advance(job, JobStatus.RESEARCH, f"niche={request.niche}")
        skipped = ""
        if not self.research_engine.configured and self.cfg.dry_run:
            # Preflight already logged why. Record it in the artifact too, so a
            # dry-run job folder never looks like a real research run.
            skipped = "YOUTUBE_API_KEY not set; dry run continued without trends"
            videos: list[ResearchVideo] = []
        else:
            videos = self._retry("research", lambda: self.research_engine.research(
                request.niche, profile, video_format=request.video_format,
                extra_keywords=request.keywords), job)
        safe_write_json(Path(job.dir) / "research.json",
                        {"niche": request.niche,
                         "count": len(videos),
                         "skipped": skipped,
                         "quota_used_today": self.quota.used(),
                         "videos": [v.to_dict() for v in videos]})
        return videos

    def stage_idea(self, job: VideoJob, request: AutomationRequest, profile,
                   videos: list[ResearchVideo]) -> tuple[ContentIdea, str]:
        self._advance(job, JobStatus.IDEA, "generating concepts")
        clusters = cluster_videos(videos)
        gaps = find_gaps(clusters, videos)
        context = research_context_block(videos, clusters, gaps)
        hints = self.learner.hints()

        ideas = self._retry("idea", lambda: self.idea_engine.generate(
            request.niche, profile, videos, clusters, gaps,
            research_context=context, strategy_hints=hints), job)
        if not ideas:
            raise PipelineError("idea", "no usable ideas produced")

        best = ideas[0]
        safe_write_json(Path(job.dir) / "idea.json", {
            "selected": best.to_dict(),
            "all_candidates": [i.to_dict() for i in ideas],
            "clusters": [c.to_dict() for c in clusters],
            "gaps": [g.to_dict() for g in gaps],
        })
        job.idea = best.to_dict()
        self.db.mark_idea_used(best.idea_id)
        log_event("IDEA", "concept selected",
                  title=best.working_title[:70],
                  score=f"{best.opportunity_score:.1f}",
                  hook_type=best.hook_type)
        return best, context

    def stage_script(self, job: VideoJob, request: AutomationRequest, profile,
                     idea: ContentIdea, context: str) -> Script:
        self._advance(job, JobStatus.SCRIPT, idea.topic[:60])
        hints = self.learner.hints()

        # Budget words against what this voice has actually been measured
        # delivering, not the niche profile's guess. The guess ran ~20% fast,
        # which is how a "45 second" request became a 53-second recording that
        # then had to be sped up by 28% to fit.
        spec = self.voice_engine.voice_spec(request.language, "energetic")
        measured = self.calibrated_words_per_second(
            request.language, spec, profile.words_per_second)
        if abs(measured - profile.words_per_second) > 0.05:
            log_event("SCRIPT", "using measured speech rate for the word budget",
                      profile=f"{profile.words_per_second:.2f} wps",
                      measured=f"{measured:.2f} wps")
            profile.words_per_second = measured

        script = self._retry("script", lambda: self.script_engine.generate(
            idea, profile, duration=request.duration_seconds,
            language=request.language, video_format=request.video_format,
            research_context=context, strategy_hints=hints), job)

        # Retention pass + safe auto-improvement (spec section 15).
        report = analyze_retention(script, profile,
                                  target_duration=request.duration_seconds)
        script, applied = auto_improve(script, profile, report)
        if applied:
            report = analyze_retention(script, profile,
                                       target_duration=request.duration_seconds)
        script.retention_score = report.score
        script.retention_notes = report.notes

        self.db.save_script(script, sha1(script.script))
        safe_write_json(Path(job.dir) / "script.json", {
            **script.to_dict(),
            "retention_report": report.to_dict(),
            "auto_improvements": applied,
        })
        job.script = script.to_dict()
        self.db.save_job(job)
        return script

    # ------------------------------------------------------------------
    SPEECH_RATE_KEY = "measured_words_per_second"

    def _speech_rate_key(self, language: str, spec) -> str:
        return f"{language or 'en'}|{getattr(spec, 'voice_id', '') or 'default'}"

    def _record_speech_rate(self, language: str, spec, script: Script,
                            total: float) -> None:
        """Blend the measured words-per-second into a stored average.

        Deliberately a slow-moving average with a small weight: one script full
        of long words should nudge the estimate, not redefine it. Only recorded
        at the base speaking rate, because a re-fitted clip was deliberately
        sped up or slowed down and says nothing about natural pace.
        """
        from .core.util import count_words
        if total <= 1.0:
            return
        words = count_words(script.script)
        if words < 20:
            return
        measured = words / total
        if not 0.8 <= measured <= 6.0:       # implausible: ignore rather than poison
            return
        key = self._speech_rate_key(language, spec)
        try:
            store = self.db.get_setting(self.SPEECH_RATE_KEY, {}) or {}
            if not isinstance(store, dict):
                store = {}
            previous = store.get(key)
            blended = measured if previous is None else previous * 0.7 + measured * 0.3
            store[key] = round(blended, 3)
            self.db.set_setting(self.SPEECH_RATE_KEY, store)
        except Exception as exc:            # never fail a job over telemetry
            log_event("TTS", "could not store measured speech rate",
                      error=str(exc)[:120])
            return
        log_event("TTS", "measured speech rate recorded", voice=key,
                  measured=f"{measured:.2f} wps",
                  stored=f"{blended:.2f} wps")

    def calibrated_words_per_second(self, language: str, spec,
                                    fallback: float) -> float:
        """The stored measurement for this voice, or the profile's guess."""
        try:
            store = self.db.get_setting(self.SPEECH_RATE_KEY, {}) or {}
            value = float(store.get(self._speech_rate_key(language, spec), 0.0))
        except Exception:
            return fallback
        return value if 0.8 <= value <= 6.0 else fallback

    def stage_voice(self, job: VideoJob, request: AutomationRequest, profile,
                    script: Script) -> tuple[Path, float, list]:
        self._advance(job, JobStatus.VOICE, f"lang={request.language}")
        job_dir = Path(job.dir)
        scenes = script.scene_objects()
        spec = self.voice_engine.voice_spec(request.language, script.voice_style)

        def synthesize(rate: str | None = None):
            if rate is not None:
                spec.rate = rate
            clips = self.voice_engine.synthesize_scenes(
                scenes, job_dir / "voice_scenes", spec)
            total, offsets = self.voice_engine.concat(clips, job_dir / "voice.wav")
            return clips, total, offsets

        clips, total, offsets = self._retry("voice", synthesize, job)

        # Calibrate the words-per-second estimate from what the voice actually
        # delivered, BEFORE any re-fit: a re-fitted clip was deliberately sped
        # up or slowed down, so it says nothing about natural pace.
        #
        # `words_per_second` in the niche profile is a guess, and it was
        # consistently optimistic: a "45s" Short came out at 53s because 127
        # words were budgeted at 2.9 wps when edge-tts delivered 2.38. The
        # correction was then made by speaking 28% faster, which is the wrong
        # lever - the right one is to budget fewer words next time.
        #
        # Measure against SPEECH time, not the assembled timeline. `total`
        # includes the 0.16s breath gap `concat` inserts between every scene,
        # so with 19 scenes that is 2.9 seconds of silence counted as speaking.
        # More scenes then meant a lower apparent rate, a smaller word budget,
        # and more scenes again - a feedback loop that walked the stored rate
        # from 2.43 down to 1.84 wps over a handful of runs.
        speech_seconds = sum(c.duration for c in clips) or total
        self._record_speech_rate(request.language, spec, script, speech_seconds)

        # ---- duration re-fit -------------------------------------------
        target = float(request.duration_seconds)
        tolerance = float(self.cfg.get("quality.duration_tolerance_pct", 25)) / 100.0
        drift = (total - target) / target if target > 0 else 0.0
        # The trigger used to be 80% of the quality tolerance, i.e. ~20% drift,
        # on the theory that re-synthesising costs a meaningful share of a short
        # job. Measured, it does not: TTS for a 14-scene Short is ~50s of a
        # ~7-minute job, and for long-form ~4 of 65 minutes. Meanwhile the loose
        # trigger let real misses through - a "45s" Short delivered at 49.3s
        # (+9.6%) and a "240s" long-form at 256s (+6.7%). Both were requested
        # precisely, so both are wrong. One cheap extra pass is the better deal.
        trigger = float(self.cfg.get("quality.refit_pct", 8)) / 100.0
        if request.video_format == "LONGFORM":
            trigger = float(self.cfg.get("quality.longform_refit_pct", 7)) / 100.0
        # Never let the trigger exceed what the gate would fail anyway.
        trigger = min(trigger, tolerance * 0.8)

        # Re-fitting works by changing the SPEAKING RATE, so it only helps if
        # the provider that actually voiced the scenes can honour one. gTTS
        # cannot - it has no rate parameter - and Piper as invoked here cannot
        # either. Re-synthesising with those is pure cost for an identical
        # result: on a fresh install where edge-tts had failed and gTTS took
        # over, the pipeline "corrected" a 63.3s recording by +16% and got
        # 63.3s back, then shipped a 41% duration miss. Say so instead.
        used = {c.provider for _, c in offsets} or {"unknown"}
        rate_capable = self.voice_engine.can_change_rate(used)
        if abs(drift) > trigger and not rate_capable:
            log_event("TTS", "duration is off but the voice cannot be re-fitted",
                      providers=",".join(sorted(used)),
                      measured=f"{total:.1f}s", target=f"{target:.0f}s",
                      drift=f"{drift * 100:+.0f}%",
                      note="only edge-tts supports a rate change; the quality "
                           "gate will block this on duration")

        if abs(drift) > trigger and rate_capable:
            # Correct the speaking rate rather than shipping a mistimed video.
            # edge-tts rate is a percentage delta on the base speed.
            base = _parse_rate(spec.rate)
            # Clamped to what still sounds like a person talking. The old
            # bounds allowed +42%, and a real run shipped at +35% - measurably
            # rushed. If the correction needed is larger than this the script
            # is the wrong length, and the duration check (now blocking) should
            # catch it rather than the delivery hiding it.
            lo = float(self.cfg.get("tts.rate_floor", 0.88))
            hi = float(self.cfg.get("tts.rate_ceiling", 1.16))
            wanted = (1.0 + base / 100.0) * (total / target)
            needed = clamp(wanted, lo, hi)
            if abs(wanted - needed) > 0.01:
                log_event("TTS", "rate correction clamped to stay natural",
                          wanted=f"{(wanted - 1) * 100:+.0f}%",
                          using=f"{(needed - 1) * 100:+.0f}%",
                          note="script length is the real problem here")
            new_rate = f"{(needed - 1.0) * 100:+.0f}%"
            log_event("TTS", "re-fitting narration to target duration",
                      measured=f"{total:.1f}s", target=f"{target:.0f}s",
                      old_rate=spec.rate, new_rate=new_rate)
            try:
                clips, total, offsets = synthesize(new_rate)
            except Exception as exc:
                log_event("TTS", "re-fit failed, keeping original narration",
                          error=str(exc)[:160])


        # Record measured per-scene timings back onto the script.
        durations: list[float] = []
        for i, (offset, clip) in enumerate(offsets):
            nxt = offsets[i + 1][0] if i + 1 < len(offsets) else total
            span = nxt - offset
            durations.append(span)
            if clip.scene_index < len(scenes):
                scenes[clip.scene_index].start = offset
                scenes[clip.scene_index].duration = span
        script.scenes = [s.to_dict() for s in scenes]
        script.estimated_duration = round(total, 2)
        job.script = script.to_dict()
        job.voice_path = str(job_dir / "voice.wav")
        self.db.save_job(job)
        return job_dir / "voice.wav", total, offsets

    def stage_visuals(self, job: VideoJob, request: AutomationRequest, profile,
                      script: Script) -> list:
        self._advance(job, JobStatus.VISUALS, f"scenes={len(script.scenes)}")
        job_dir = Path(job.dir)
        scenes = script.scene_objects()
        assign_motion(scenes, self._motion_cycle)
        w, h = self.composer.resolution(request.video_format)

        # Scene durations were measured during the voice stage and written back
        # onto the script. Passing them lets the stock-video provider skip
        # clips shorter than the scene, which would otherwise loop visibly.
        durations = [s.duration or 0.0 for s in scenes]

        assets = self._retry("visuals", lambda: self.visual_engine.generate(
            scenes, job_dir / "assets", style=request.style,
            made_for_kids=profile.made_for_kids, width=w, height=h,
            durations=durations), job)

        script.scenes = [s.to_dict() for s in scenes]
        job.script = script.to_dict()
        job.assets = [a.to_dict() for a in assets]
        self.db.save_assets(job.job_id, assets)
        self.db.save_job(job)
        return assets

    def stage_render(self, job: VideoJob, request: AutomationRequest, profile,
                    script: Script, voice: Path, total: float,
                    offsets: list) -> Path:
        self._advance(job, JobStatus.RENDERING, f"{total:.1f}s")
        job_dir = Path(job.dir)
        w, h = self.composer.resolution(request.video_format)
        scenes = script.scene_objects()

        # ---- captions ---------------------------------------------------
        caption_style = (profile.caption_style
                         if self.cfg.get("captions.style") == "karaoke"
                         else str(self.cfg.get("captions.style")))
        ass_path, srt_path, groups = self.caption_engine.build(
            offsets, job_dir / "captions.ass", job_dir / "captions.srt",
            w, h, style_override=caption_style)
        job.subtitle_path = str(srt_path)

        # ---- music + sfx ------------------------------------------------
        # Both are switchable. A synthesised bed is a taste call, not a
        # requirement: plenty of good Shorts have voice only, and if the pad
        # bothers you, turning it off is a better answer than fighting it.
        boundaries = [off for off, _ in offsets][1:]
        music_path, music_src = None, "none"
        if bool(self.cfg.get("video.music_enabled", True)):
            music_path, music_src = self._retry("music", lambda: build_music(
                total, job_dir / "music.wav", mood_text=profile.music_mood,
                seed=job.job_id), job)
        else:
            log_event("MUSIC", "music disabled in config")

        sfx_path = None
        if bool(self.cfg.get("video.sfx_enabled", True)):
            try:
                sfx_path = build_transition_sfx(
                    boundaries, total, job_dir / "sfx.wav", seed=job.job_id)
            except Exception as exc:
                log_event("MUSIC", "SFX skipped", error=str(exc)[:140])

        # ---- master mix -------------------------------------------------
        master, audio_stats = self._retry("audio", lambda: self.composer.mix_audio(
            voice, job_dir / "master.wav", music=music_path, sfx=sfx_path), job)

        # ---- video ------------------------------------------------------
        durations = [s.duration for s in scenes if s.duration > 0]
        if not durations:
            raise PipelineError("render", "no measured scene durations")
        timings = [SceneTiming(index=s.index, image=Path(s.asset_path),
                               duration=s.duration, motion=s.motion)
                   for s in scenes if s.asset_path and s.duration > 0]
        if len(timings) != len(durations):
            raise PipelineError("render",
                                f"{len(durations)} timed scenes but "
                                f"{len(timings)} have images")

        clips = self._retry("render", lambda: self.composer.render_scene_clips(
            timings, job_dir / "clips", w, h), job)
        result = self._retry("render", lambda: self.composer.finalize(
            clips, durations, master, ass_path, job_dir / "video.mp4", w, h), job)
        cleanup_clips(clips)

        job.video_path = str(result.video)
        target = float(request.duration_seconds)
        safe_write_json(job_dir / "render_report.json", {
            "duration": result.duration, "resolution": f"{w}x{h}",
            "fps": result.fps, "caption_groups": groups,
            "music_source": music_src, "sfx": bool(sfx_path),
            "audio_lufs": audio_stats.get("input_i"),
            "audio_true_peak": audio_stats.get("input_tp"),
            "scene_count": len(timings),
            "motions": [t.motion for t in timings],
            # Recorded so the artifact is self-describing: "42.8s" means
            # nothing without knowing what was asked for.
            "video_format": request.video_format,
            "target_duration": target,
            "duration_error_pct": (round((result.duration - target) / target * 100, 1)
                                   if target > 0 else None),
            "batched_render": len(timings) > self.composer.segment_max,
            "segments": (-(-len(timings) // self.composer.segment_max)
                         if len(timings) > self.composer.segment_max else 1),
        })
        self.db.save_job(job)
        return result.video

    def stage_finalize(self, job: VideoJob, request: AutomationRequest, profile,
                       idea: ContentIdea, script: Script, video: Path,
                       videos: list[ResearchVideo],
                       assets: list) -> tuple[VideoMetadata, QualityReport]:
        self._advance(job, JobStatus.QUALITY_CHECK, "metadata + checks")
        job_dir = Path(job.dir)

        # ---- metadata ---------------------------------------------------
        meta = self._retry("metadata", lambda: self.metadata_engine.build(
            script, idea, profile, video_format=request.video_format,
            language=request.language,
            made_for_kids=profile.made_for_kids or request.made_for_kids,
            synthetic_disclosure=bool(
                self.cfg.get("youtube.synthetic_disclosure", True))), job)

        # ---- thumbnail --------------------------------------------------
        thumbnail: Path | None = None
        try:
            thumbnail, variants = self.thumbnail_engine.generate(
                title=meta.title, out_dir=job_dir / "thumbnails", video=video,
                video_format=request.video_format,
                made_for_kids=profile.made_for_kids)
            job.thumbnail_path = str(thumbnail)
            safe_write_json(job_dir / "thumbnail_report.json",
                            {"selected": thumbnail.name,
                             "variants": [{"style": v.style, "score": v.score,
                                           "text": v.text, "metrics": v.metrics}
                                          for v in variants]})
        except Exception as exc:
            log_event("THUMBNAIL", "generation failed", error=str(exc)[:180])

        # ---- originality + fact check -----------------------------------
        orig = self.originality.check(
            script, idea, videos, assets,
            voice_provider=self.voice_engine.providers[0].name
            if self.voice_engine.providers else "")
        safe_write_json(job_dir / "originality_report.json", orig.to_dict())

        fact = self.factchecker.check(script, profile)
        safe_write_json(job_dir / "factcheck_report.json", fact.to_dict())

        # ---- quality gate -----------------------------------------------
        quality = self.quality_gate.evaluate(
            video=video, metadata=meta, script=script, profile=profile,
            subtitle=Path(job.subtitle_path) if job.subtitle_path else None,
            thumbnail=thumbnail, target_duration=float(request.duration_seconds),
            video_format=request.video_format, originality=orig, factcheck=fact)

        safe_write_json(job_dir / "metadata.json", meta.to_dict())
        safe_write_json(job_dir / "quality_report.json", quality.to_dict())
        job.metadata = meta.to_dict()
        job.quality = quality.to_dict()
        self.db.save_job(job)
        return meta, quality

    def stage_publish(self, job: VideoJob, request: AutomationRequest,
                      meta: VideoMetadata, quality: QualityReport,
                      fact_requires_approval: bool = False) -> dict[str, Any] | None:
        if not quality.passed:
            self._advance(job, JobStatus.REJECTED,
                          f"quality {quality.score:.0f}/100 "
                          f"({len(quality.blockers)} blockers)")
            log_event("PUBLISH", "upload refused by quality gate",
                      score=f"{quality.score:.0f}",
                      minimum=self.quality_gate.minimum,
                      blockers="; ".join(quality.blockers[:3]))
            return None

        approval_required = (
            bool(self.cfg.get("automation.approval_required", True))
            or request.mode == Mode.APPROVAL.value
            or fact_requires_approval
            or meta.made_for_kids)      # kids content always confirms (spec 9)

        if approval_required:
            reason = ("kids classification must be confirmed"
                      if meta.made_for_kids else
                      "factual risk needs review" if fact_requires_approval else
                      "approval mode")
            self._advance(job, JobStatus.AWAITING_APPROVAL, reason)
            log_event("PUBLISH", "waiting for human approval", reason=reason,
                      job=job.job_id)
            return None

        return self.publish_now(job, request, meta)

    def publish_now(self, job: VideoJob, request: AutomationRequest,
                    meta: VideoMetadata) -> dict[str, Any]:
        """Actually upload (or schedule). Also used by the approve flow."""
        job_dir = Path(job.dir)
        schedule = (request.publish_mode != "immediate"
                    and (request.frequency != "once" or bool(request.upload_time)))
        # force_private also suppresses SCHEDULING, not just the privacy field.
        # A scheduled insert carries publishAt, and YouTube publishes on that
        # timestamp by itself - so leaving the schedule in place would make the
        # video public regardless of what privacyStatus said at upload.
        if self.uploader.force_private:
            schedule = False
        if schedule and request.upload_time:
            meta.publish_at = self.uploader.resolve_publish_at(
                upload_time=request.upload_time, timezone=request.timezone,
                days=request.days or None)

        result = self.uploader.upload(
            video=Path(job.video_path), meta=meta,
            thumbnail=Path(job.thumbnail_path) if job.thumbnail_path else None,
            subtitle=Path(job.subtitle_path) if job.subtitle_path else None,
            schedule=bool(meta.publish_at))

        safe_write_json(job_dir / "upload_result.json", result.to_dict())
        job.metadata = meta.to_dict()

        if result.dry_run or not result.video_id:
            self._advance(job, JobStatus.READY,
                          "artifacts complete; upload not performed")
        elif meta.publish_at:
            job.youtube_video_id = result.video_id
            job.scheduled_for = meta.publish_at
            self._advance(job, JobStatus.SCHEDULED, meta.publish_at)
            self.db.save_published(job)
        else:
            job.youtube_video_id = result.video_id
            job.published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._advance(job, JobStatus.PUBLISHED, result.url)
            self.db.save_published(job)
        return result.to_dict()

    # ==================================================================
    # Full run
    # ==================================================================
    def apply_style_template(self, request: AutomationRequest, profile):
        """Fold the chosen StyleTemplate into the profile and engine settings.

        A NicheProfile says what the content IS; a StyleTemplate says how the
        video looks and moves (spec section 45). Applying it in one place means
        every downstream stage - script pacing, captions, transitions, motion,
        colour grade, music - reads a single consistent set of numbers.

        Returns (profile, template).
        """
        template = select_template(
            request.niche, request.style,
            made_for_kids=profile.made_for_kids,
            forced=str(self.cfg.get("video.style_template", "")))
        profile = apply_to_profile(profile, template)

        base_font = int(self.cfg.get("captions.font_size", 112))
        for key, value in caption_overrides(template, base_font).items():
            self.cfg.set(key, value)
        for key, value in video_overrides(template).items():
            self.cfg.set(key, value)

        # These two cached the previous settings at construction time.
        self.caption_engine = CaptionEngine(self.cfg)
        self.composer = VideoComposer(self.cfg)
        self._motion_cycle = list(template.motion_cycle)

        log_event("PIPELINE", "style template selected", template=template.name,
                  scene_seconds=template.scene_seconds,
                  captions=template.caption_style,
                  transition=f"{template.transition}/{template.transition_duration}s")
        return profile, template

    def run(self, request: AutomationRequest, *,
            skip_preflight: bool = False) -> PipelineResult:
        job = VideoJob(automation_id=request.id, request=request.to_dict())
        job_dir = self._job_dir(job, request)
        self.db.save_job(job)

        profile = build_profile(
            request.niche, audience=request.audience, style=request.style,
            made_for_kids=request.made_for_kids, language=request.language,
            duration_seconds=request.duration_seconds)

        profile, template = self.apply_style_template(request, profile)
        safe_write_json(job_dir / "niche_profile.json", {
            **profile.to_dict(),
            "style_template": template.to_dict(),
        })

        if not skip_preflight:
            problems = self.preflight(request)
            blocking = [p for p in problems if "ffmpeg" in p or "limit" in p
                        or "YOUTUBE_API_KEY" in p]
            if blocking:
                job.error = " | ".join(blocking)
                self._advance(job, JobStatus.FAILED, job.error)
                raise PipelineError("preflight", job.error)

        log_event("PIPELINE", "started", job=job.job_id, niche=request.niche,
                  duration=request.duration_seconds, format=request.video_format,
                  mode=request.mode, dry_run=self.cfg.dry_run)
        started = time.time()

        try:
            videos = self.stage_research(job, request, profile)
            idea, context = self.stage_idea(job, request, profile, videos)
            script = self.stage_script(job, request, profile, idea, context)
            voice, total, offsets = self.stage_voice(job, request, profile, script)
            assets = self.stage_visuals(job, request, profile, script)
            video = self.stage_render(job, request, profile, script, voice,
                                      total, offsets)
            meta, quality = self.stage_finalize(job, request, profile, idea,
                                                script, video, videos, assets)
            fact = read_json(job_dir / "factcheck_report.json", {}) or {}
            uploaded = self.stage_publish(
                job, request, meta, quality,
                fact_requires_approval=bool(fact.get("requires_approval")))
        except PipelineError:
            self._advance(job, JobStatus.FAILED, job.error)
            raise
        except Exception as exc:
            job.error = str(exc)[:500]
            self._advance(job, JobStatus.FAILED, job.error)
            raise PipelineError("pipeline", str(exc)) from exc

        elapsed = time.time() - started
        artifacts = {p.name: str(p) for p in sorted(job_dir.iterdir())
                     if p.is_file()}
        safe_write_json(job_dir / "job.json", job.to_dict())
        log_event("PIPELINE", "finished", job=job.job_id, status=job.status,
                  quality=f"{quality.score:.0f}/100",
                  seconds=f"{elapsed:.0f}")
        return PipelineResult(job=job, artifacts=artifacts, quality=quality,
                              uploaded=uploaded)

    # ==================================================================
    # Approval / analytics entry points
    # ==================================================================
    def approve(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if job is None:
            raise PipelineError("approve", f"unknown job {job_id}")
        if job.status != JobStatus.AWAITING_APPROVAL.value:
            raise PipelineError(
                "approve", f"job {job_id} is {job.status}, not awaiting approval")
        request = AutomationRequest.from_dict(job.request or {})
        meta = VideoMetadata.from_dict(job.metadata or {})
        log_event("APPROVAL", "approved by user", job=job_id)
        return self.publish_now(job, request, meta)

    def reject(self, job_id: str, reason: str = "") -> VideoJob:
        job = self.db.get_job(job_id)
        if job is None:
            raise PipelineError("reject", f"unknown job {job_id}")
        self._advance(job, JobStatus.REJECTED, reason or "rejected by user")
        return job

    def collect_analytics(self, *, days: int = 28) -> dict[str, Any]:
        collector = AnalyticsCollector(self.cfg, self.auth, self.db)
        stats = collector.collect_all(days=days)
        for job in self.db.list_jobs(JobStatus.SCHEDULED.value, limit=200):
            # Promote scheduled jobs whose publish time has passed.
            if job.scheduled_for and job.scheduled_for <= time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()):
                self._advance(job, JobStatus.PUBLISHED, "scheduled time reached")
        insights = self.learner.learn()
        return {"collected": [s.to_dict() for s in stats],
                "insights": [i.to_dict() for i in insights],
                "hints": self.learner.hints()}

    def resume_pending(self) -> list[str]:
        """Recover jobs interrupted by a crash/reboot (spec section 22)."""
        pending = self.db.pending_jobs()
        recovered: list[str] = []
        for job in pending:
            log_event("RECOVERY", "found interrupted job", job=job.job_id,
                      status=job.status, retries=job.retry_count)
            if job.retry_count >= int(self.cfg.get("automation.max_retries", 3)) * 3:
                self._advance(job, JobStatus.FAILED, "retry budget exhausted")
                continue
            recovered.append(job.job_id)
        return recovered

    def close(self) -> None:
        self.db.close()


def _parse_rate(rate: str) -> float:
    """'+8%' -> 8.0"""
    try:
        return float(str(rate).replace("%", "").strip())
    except ValueError:
        return 0.0
