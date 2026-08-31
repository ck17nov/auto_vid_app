"""Long-form video tests.

Everything here exists because the project shipped with three limits that made
long videos impossible and did so *silently*:

  1. A flat 24-scene cap, so a 10-minute video was 24 stills at 25s each.
  2. A single-shot LLM call, which a 3,000-word script cannot fit inside a free
     tier's per-minute token budget.
  3. A single ffmpeg call taking every clip as an input, which cannot be
     launched at all past a few hundred clips on Windows.

These tests pin the fixes. The render test uses real ffmpeg on real files
because the whole point is that total video length still equals total audio
length once the render is batched.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from engine.content.llm import (LLMError, _is_model_retired,
                                _is_transient_llm)
from engine.content.metadata import MetadataGenerator
from engine.content.script import ScriptGenerator
from engine.core.config import load_config
from engine.core.models import ContentIdea, Scene, Script
from engine.core.niche import build_profile
from engine.core.util import count_words, ffmpeg_bin, probe_duration
from engine.video.compose import SceneTiming, VideoComposer


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# ==========================================================================
# Model retirement: the gemini-2.0-flash shutdown class of failure
# ==========================================================================
class TestModelRetirement:
    @pytest.mark.parametrize("message", [
        "gemini 404: {'error': {'message': 'models/gemini-2.0-flash is not found "
        "for API version v1beta', 'status': 'NOT_FOUND'}}",
        "groq 404: model_not_found",
        "groq 400: The model `llama-3.1-70b-versatile` has been decommissioned",
        "gemini 400: model is no longer available",
        "groq 400: unknown model",
    ])
    def test_retired_models_are_recoverable(self, message):
        assert _is_model_retired(message) is True

    @pytest.mark.parametrize("message", [
        "groq rate limit reached (free tier)",
        "gemini 429: quota exceeded for this project",
        "groq 401: Invalid API Key",
        "gemini 403: permission denied",
        "groq 500: internal server error",
        "groq 400: max_tokens must be <= 8192",
    ])
    def test_other_failures_are_not_treated_as_retirement(self, message):
        """Falling forward through every model on a bad key would burn the
        whole fallback list and report a confusing error."""
        assert _is_model_retired(message) is False

    def test_a_dead_default_falls_forward_to_a_live_model(self):
        """The exact failure that broke this project on 2026-06-01."""
        from engine.content.llm import GeminiProvider, LLMResult

        provider = GeminiProvider(api_key="test-key", model="gemini-2.0-flash")
        tried: list[str] = []

        def fake_call(model, prompt, system, json_mode, temperature, max_tokens):
            tried.append(model)
            if model == "gemini-2.0-flash":
                raise LLMError("gemini 404: models/gemini-2.0-flash is not found")
            return LLMResult(text='{"ok": true}', provider="gemini", model=model)

        provider._call = fake_call
        result = provider.complete("hi")
        assert tried[0] == "gemini-2.0-flash", "the configured model is tried first"
        assert len(tried) == 2, "it must not walk the whole list needlessly"
        assert result.model in GeminiProvider.FALLBACK_MODELS
        assert provider.model == result.model, "the working model must stick"

    def test_a_bad_key_fails_immediately(self):
        from engine.content.llm import GroqProvider

        provider = GroqProvider(api_key="bad")
        calls: list[str] = []

        def fake_call(model, *a, **kw):
            calls.append(model)
            raise LLMError("groq 401: Invalid API Key")

        provider._call = fake_call
        with pytest.raises(LLMError, match="401"):
            provider.complete("hi")
        assert len(calls) == 1, "must not try every model on an auth failure"


# ==========================================================================
# Sectioned script generation
# ==========================================================================
class FakeRouter:
    """Deterministic stand-in for LLMRouter, so this runs offline.

    Returns a plausible outline, then a section on demand. `fail_sections`
    forces specific section indexes to raise, to exercise the degraded path.
    """

    def __init__(self, sections: int = 13, overrun: float = 1.0,
                 fail_sections: frozenset[int] = frozenset()):
        self.sections = sections
        # 1.0 = a compliant provider that respects the word budget.
        # >1 models a provider that hits the scene count but overruns on words.
        self.overrun = overrun
        self.fail_sections = fail_sections
        self.calls: list[int] = []          # max_tokens of each call
        self.outline_calls = 0
        self.section_calls = 0
        self.single_shot_calls = 0

    def complete_json(self, prompt, *, system="", temperature=0.8,
                      max_tokens=4096, attempts=2):
        self.calls.append(max_tokens)
        if "Write ONE SECTION" not in prompt and "Plan an original" not in prompt:
            # The single-shot path: one call for the whole script.
            self.single_shot_calls += 1
            return {
                "title_ideas": ["A Genuinely Specific Title About The Thing"],
                "hook": "The measurement that everyone quotes is the wrong one.",
                "voice_style": "serious",
                "cta": "Check the source before you repeat it.",
                "scenes": [
                    {"role": "value",
                     "narration": " ".join(f"s{n}w{k}" for k in range(12)) + ".",
                     "visual_prompt": f"a photograph of subject {n}",
                     "visual_keywords": ["subject"], "on_screen_text": ""}
                    for n in range(10)
                ],
                "claims": [], "sources": [],
            }, "fake"
        if "Plan an original" in prompt:
            self.outline_calls += 1
            return {
                "title_ideas": ["A Genuinely Specific Title About The Thing"],
                "hook": "The measurement that everyone quotes is the wrong one.",
                "voice_style": "serious",
                "cta": "Check the source before you repeat it.",
                "sections": [
                    {"heading": f"Section Heading {i}",
                     "purpose": f"advance idea {i}",
                     "key_points": [f"point {i}a", f"point {i}b"]}
                    for i in range(self.sections)
                ],
                "sources": [{"title": "A source", "note": "supports the claim"}],
            }, "fake"

        idx = self.section_calls
        self.section_calls += 1
        if idx in self.fail_sections:
            raise LLMError(f"simulated failure on section {idx}")

        # Honour BOTH stated budgets, so the maths is really exercised.
        import re
        scenes = int(m.group(1)) if (m := re.search(
            r"exactly\s+(\d+)\s+scenes", prompt)) else 8
        words = int(m2.group(1)) if (m2 := re.search(
            r"is (\d+) words of narration", prompt)) else scenes * 12
        per_scene = max(2, round(words * self.overrun / max(scenes, 1)))
        return {
            "scenes": [
                {"role": "value",
                 "narration": " ".join(
                     f"w{idx}s{n}p{k}" for k in range(per_scene)) + ".",
                 "visual_prompt": f"a photograph of subject {idx}-{n}",
                 "visual_keywords": ["subject", "light"],
                 "on_screen_text": ""}
                for n in range(scenes)
            ],
            "claims": [{"claim": f"claim {idx}", "confidence": "medium",
                        "basis": "stated in the section"}],
        }, "fake"


def make_idea() -> ContentIdea:
    return ContentIdea(
        topic="deep sea pressure",
        angle="the depth figure quoted everywhere comes from a retracted paper",
        hook_concept="The number everyone repeats was withdrawn in 1998.",
        hook_type="contradiction",
        working_title="The Depth Figure That Was Withdrawn",
    )


class TestSectionedScript:
    def _generate(self, cfg, duration=1200, **router_kw):
        router = FakeRouter(**router_kw)
        gen = ScriptGenerator(cfg, router=router)
        script = gen.generate(make_idea(), build_profile("science",
                                                        duration_seconds=duration),
                              duration=duration, video_format="LONGFORM")
        return script, router

    def test_long_request_uses_the_sectioned_path(self, cfg):
        script, router = self._generate(cfg)
        assert router.section_calls > 1, "must not be a single-shot call"
        assert script.provider == "fake"

    def test_short_request_stays_single_shot(self, cfg):
        router = FakeRouter()
        gen = ScriptGenerator(cfg, router=router)
        gen.generate(make_idea(), build_profile("science", duration_seconds=45),
                     duration=45, video_format="SHORT")
        assert router.outline_calls == 0, "a Short must not plan sections"
        assert router.section_calls == 0
        assert router.single_shot_calls == 1, "a Short needs exactly one call"

    def test_no_single_call_exceeds_a_free_tier_minute(self, cfg):
        """Free tiers allow 6k-12k tokens/minute. Every call must fit alone."""
        _, router = self._generate(cfg)
        assert max(router.calls) <= 3072, router.calls

    def test_scene_count_is_proportional_to_duration(self, cfg):
        script, _ = self._generate(cfg, duration=1200)
        scenes = script.scene_objects()
        assert len(scenes) > 60, f"20 minutes cannot be {len(scenes)} scenes"
        assert 1200 / len(scenes) <= 12.0, "visuals must not hold for 25 seconds"

    def test_scene_indexes_are_contiguous_across_sections(self, cfg):
        script, _ = self._generate(cfg)
        assert [s.index for s in script.scene_objects()] == \
            list(range(len(script.scenes)))

    def test_word_count_lands_near_the_target(self, cfg):
        duration = 1200
        script, _ = self._generate(cfg, duration=duration)
        profile = build_profile("science", duration_seconds=duration)
        target = duration * profile.words_per_second
        got = count_words(script.script)
        assert 0.7 * target <= got <= 1.35 * target, (got, target)

    def test_longform_overrun_is_trimmed_not_absorbed_by_speech_rate(self, cfg):
        """A 17% word overrun forced a +31% delivery rate in a real run.

        Long-form must trim close to the budget so the speaking rate only has
        to make a small correction. Shorts keep the looser threshold, because
        there a scene is a large fraction of the whole video.
        """
        duration = 1200
        script, _ = self._generate(cfg, duration=duration, overrun=1.2)
        profile = build_profile("science", duration_seconds=duration)
        target = duration * profile.words_per_second
        assert count_words(script.script) <= 1.06 * target, (
            f"{count_words(script.script)} words vs {target:.0f} target")

    def test_shorts_keep_the_looser_threshold(self, cfg):
        """A Short must not lose a whole scene over a modest overrun.

        Tested through behaviour rather than by exposing the thresholds: a
        Short that overruns by ~20% should keep every scene and let the
        speaking rate absorb it, where long-form would trim.
        """
        router = FakeRouter(overrun=1.2)
        gen = ScriptGenerator(cfg, router=router)
        short = gen.generate(make_idea(), build_profile("science",
                                                       duration_seconds=45),
                             duration=45, video_format="SHORT")
        # The single-shot fake returns 10 scenes of 12 words = 120 words
        # against a ~117-word target, i.e. only just over: nothing to cut.
        assert len(short.scenes) == 10, "a Short lost scenes it did not need to"

    def test_chapters_are_recorded_with_scene_positions(self, cfg):
        script, _ = self._generate(cfg, sections=8)
        assert len(script.chapters) == 8, script.chapters
        assert script.chapters[0]["scene_index"] == 0
        positions = [c["scene_index"] for c in script.chapters]
        assert positions == sorted(positions), "chapters must be in order"
        assert max(positions) < len(script.scenes), "must point at a real scene"

    def test_chapters_survive_a_provider_that_overruns_the_word_budget(self, cfg):
        """A real model hits the scene count and overshoots the words.

        The script is then trimmed back to budget, which deletes scenes. When
        chapters were keyed to a deleted scene they were dropped outright, so a
        20-minute video lost 3 of its 8 chapters. Headings must be re-anchored
        to the next surviving scene instead.
        """
        script, _ = self._generate(cfg, sections=8, overrun=2.0)
        assert len(script.chapters) == 8, script.chapters
        positions = [c["scene_index"] for c in script.chapters]
        assert positions == sorted(positions)
        assert len(set(positions)) == len(positions), "no two chapters may collide"
        assert max(positions) < len(script.scenes)

    def test_overrun_is_still_trimmed_back_to_the_duration_target(self, cfg):
        duration = 1200
        script, _ = self._generate(cfg, duration=duration, overrun=2.0)
        profile = build_profile("science", duration_seconds=duration)
        target = duration * profile.words_per_second
        assert count_words(script.script) <= 1.35 * target

    def test_a_failed_section_degrades_instead_of_losing_the_video(self, cfg):
        script, router = self._generate(cfg, fail_sections=frozenset({3, 7}))
        assert script.provider == "fake+template", \
            "degradation must be visible in the provider label"
        assert count_words(script.script) > 300, "the video must still exist"

    def test_every_section_failing_still_produces_a_script(self, cfg):
        script, _ = self._generate(
            cfg, sections=6, fail_sections=frozenset(range(6)))
        assert script.scenes
        assert "template" in script.provider

    def test_hook_is_not_a_greeting(self, cfg):
        script, _ = self._generate(cfg)
        assert not script.hook.lower().startswith(
            ("hey", "hi ", "welcome", "in this video"))


# ==========================================================================
# Chapters
# ==========================================================================
class TestChapters:
    def _script_with_chapters(self) -> Script:
        scenes = [Scene(index=i, narration=f"Scene {i} narration here.",
                        duration=6.0, role="value").to_dict()
                  for i in range(30)]
        return Script(
            scenes=scenes, script=" ".join(s["narration"] for s in scenes),
            chapters=[{"heading": f"Heading {n}", "scene_index": n * 5}
                      for n in range(6)])

    def test_headings_beat_narration_derived_labels(self, cfg):
        meta = MetadataGenerator(cfg).build_chapters(self._script_with_chapters())
        assert [c["label"] for c in meta] == [f"Heading {n}" for n in range(6)]

    def test_first_chapter_is_pinned_to_zero(self, cfg):
        """YouTube shows no chapters at all unless the first is at 00:00."""
        meta = MetadataGenerator(cfg).build_chapters(self._script_with_chapters())
        assert meta[0]["seconds"] == 0.0

    def test_chapter_times_follow_scene_durations(self, cfg):
        meta = MetadataGenerator(cfg).build_chapters(self._script_with_chapters())
        # 5 scenes x 6s per chapter.
        assert meta[1]["seconds"] == pytest.approx(30.0)
        assert meta[2]["seconds"] == pytest.approx(60.0)

    def test_chapters_closer_than_ten_seconds_are_dropped(self, cfg):
        scenes = [Scene(index=i, narration="x", duration=2.0).to_dict()
                  for i in range(10)]
        script = Script(scenes=scenes, chapters=[
            {"heading": "A", "scene_index": 0},
            {"heading": "B", "scene_index": 1},      # 2s in - too close
            {"heading": "C", "scene_index": 6},      # 12s in - fine
        ])
        labels = [c["label"] for c in MetadataGenerator(cfg).build_chapters(script)]
        assert labels == ["A", "C"]

    def test_out_of_range_chapters_are_ignored(self, cfg):
        script = Script(
            scenes=[Scene(index=0, narration="x", duration=5.0).to_dict()],
            chapters=[{"heading": "A", "scene_index": 0},
                      {"heading": "ghost", "scene_index": 99}])
        labels = [c["label"] for c in MetadataGenerator(cfg).build_chapters(script)]
        assert labels == ["A"]

    def test_shorts_get_no_chapters(self, cfg):
        """Chapters on a Short are meaningless and clutter the description."""
        script = self._script_with_chapters()
        meta = MetadataGenerator(cfg).build(
            script, make_idea(), build_profile("science"), video_format="SHORT")
        assert meta.chapters == []


# ==========================================================================
# Batched render: the duration must survive segmentation exactly
# ==========================================================================
def _make_image(path: Path, shade: int) -> Path:
    from PIL import Image
    Image.new("RGB", (1280, 720), (shade, 40, 90)).save(path)
    return path


def _make_silence(path: Path, seconds: float) -> Path:
    subprocess.run(
        [ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{seconds:.3f}",
         "-c:a", "pcm_s16le", str(path)], check=True, timeout=120)
    return path


@pytest.mark.slow
class TestBatchedRender:
    N = 11
    SCENE_SECONDS = 1.2

    @pytest.fixture(scope="class")
    def rendered(self, cfg, tmp_path_factory):
        """Render the same timeline twice: flat chain vs batched chain."""
        work = tmp_path_factory.mktemp("batched")
        timings = [
            SceneTiming(index=i,
                        image=_make_image(work / f"img{i}.png", 30 + i * 15),
                        duration=self.SCENE_SECONDS, motion="zoom_in")
            for i in range(self.N)
        ]
        total_audio = self.N * self.SCENE_SECONDS
        audio = _make_silence(work / "audio.wav", total_audio)

        flat = VideoComposer(cfg)
        flat.segment_max = 999                       # force the single-pass path
        clips = flat.render_scene_clips(timings, work / "clips", 640, 360,
                                        parallel=3)
        flat_out = flat.finalize(clips, [t.duration for t in timings], audio,
                                 None, work / "flat.mp4", 640, 360)

        batched = VideoComposer(cfg)
        batched.segment_max = 4                      # 11 clips -> 3 segments
        batched_out = batched.finalize(clips, [t.duration for t in timings],
                                       audio, None, work / "batched.mp4",
                                       640, 360)
        return total_audio, flat_out, batched_out, work

    def test_batching_actually_engaged(self, rendered):
        _, _, batched_out, _ = rendered
        assert self.N > 4, "fixture must exceed segment_max to be meaningful"
        assert batched_out.video.exists()

    def test_video_length_equals_audio_length(self, rendered):
        """The whole reason hierarchical cross-fading is safe."""
        total_audio, _, batched_out, _ = rendered
        assert batched_out.duration == pytest.approx(total_audio, abs=0.12), \
            f"{batched_out.duration:.3f}s video vs {total_audio:.3f}s audio"

    def test_batched_matches_the_flat_chain(self, rendered):
        _, flat_out, batched_out, _ = rendered
        assert batched_out.duration == pytest.approx(flat_out.duration, abs=0.08)

    def test_intermediate_segments_are_cleaned_up(self, rendered):
        _, _, _, work = rendered
        assert not (work / "segments").exists(), \
            "segment scratch files must not be left in the job folder"

    def test_output_has_both_streams(self, rendered):
        _, _, batched_out, _ = rendered
        assert probe_duration(batched_out.video) > 1.0
        streams = VideoComposer(load_config()).probe(
            batched_out.video).get("streams", [])
        video = next(s for s in streams if s.get("codec_type") == "video")
        audio = next(s for s in streams if s.get("codec_type") == "audio")
        assert video.get("width") == 640 and video.get("height") == 360
        assert audio.get("codec_name") == "aac"


# ==========================================================================
# Visual provider circuit breaker
# ==========================================================================
class TestVisualBreaker:
    """A rate limit belongs to the provider, not to a scene.

    Retrying per scene cost ~15s of backoff each on a 71-scene video - 18
    minutes spent waiting to be refused, after which every image came from the
    fallback anyway.
    """

    class _AlwaysRateLimited:
        name = "flaky"

        def __init__(self):
            self.attempts = 0

        def available(self):
            return True

        def fetch(self, req, target):
            self.attempts += 1
            raise RuntimeError("Client error '429 Too Many Requests' for url ...")

    def _engine(self, cfg, provider, limit=3):
        from engine.visuals.engine import VisualEngine
        from engine.visuals.procedural import ProceduralProvider
        engine = VisualEngine(cfg)
        engine.providers = [provider, ProceduralProvider()]
        engine.retry_backoff = 0.0          # keep the test fast
        cfg.set("visuals.breaker_limit", limit)
        return engine

    def test_a_dead_provider_is_dropped_after_three_scenes(self, cfg, tmp_path):
        provider = self._AlwaysRateLimited()
        engine = self._engine(cfg, provider)
        scenes = [Scene(index=i, narration=f"Scene {i}.",
                        visual_prompt=f"a subject {i}") for i in range(12)]
        assets = engine.generate(scenes, tmp_path / "assets", parallel=1,
                                 width=320, height=240)

        assert len(assets) == 12, "every scene must still get an image"
        # 3 scenes x transient_retries attempts, then the breaker trips.
        assert provider.attempts <= 3 * engine.transient_retries, (
            f"{provider.attempts} attempts means the breaker never tripped")
        assert all(a.source == "procedural" for a in assets[3:])

    def test_a_provider_refusing_half_the_time_is_also_dropped(self, cfg, tmp_path):
        """The case a consecutive-failure breaker misses entirely.

        Pollinations refused ~50% of requests on a 71-scene job. A streak
        counter never reached three, so every other scene still paid full
        backoff. The trigger is therefore a failure RATE.
        """
        from engine.visuals.procedural import ProceduralProvider

        class HalfDead(self._AlwaysRateLimited):
            name = "halfdead"

            def fetch(self, req, target):
                self.attempts += 1
                # Keyed on the SCENE, not the attempt: half the scenes fail
                # every retry, which is the shape actually observed. Alternating
                # per attempt would let the in-scene retry rescue it every time.
                if req.scene_index % 2:
                    raise RuntimeError("429 Too Many Requests")
                return ProceduralProvider().fetch(req, target)

        provider = HalfDead()
        engine = self._engine(cfg, provider)
        scenes = [Scene(index=i, narration=f"Scene {i}.",
                        visual_prompt=f"a subject {i}") for i in range(30)]
        engine.generate(scenes, tmp_path / "half", parallel=1,
                        width=320, height=240)
        assert provider.attempts < 30, (
            f"{provider.attempts} attempts across 30 scenes: a 50%-failing "
            f"provider was never dropped")

    def test_the_breaker_does_not_fire_on_a_healthy_provider(self, cfg, tmp_path):
        class Healthy(self._AlwaysRateLimited):
            name = "healthy"

            def fetch(self, req, target):
                self.attempts += 1
                if self.attempts == 1:      # one blip, then fine
                    raise RuntimeError("429 Too Many Requests")
                from engine.visuals.procedural import ProceduralProvider
                return ProceduralProvider().fetch(req, target)

        provider = Healthy()
        engine = self._engine(cfg, provider)
        scenes = [Scene(index=i, narration=f"Scene {i}.",
                        visual_prompt=f"a subject {i}") for i in range(6)]
        engine.generate(scenes, tmp_path / "assets2", parallel=1,
                        width=320, height=240)
        assert provider.attempts >= 6, \
            "a single blip must not disable the provider for the whole job"

    def test_procedural_is_never_dropped(self, cfg, tmp_path):
        """It is the floor of the fallback chain; dropping it strands a scene."""
        provider = self._AlwaysRateLimited()
        engine = self._engine(cfg, provider, limit=1)
        scenes = [Scene(index=i, narration=f"Scene {i}.",
                        visual_prompt=f"a subject {i}") for i in range(4)]
        assets = engine.generate(scenes, tmp_path / "assets3", parallel=1,
                                 width=320, height=240)
        assert [a.source for a in assets] == ["procedural"] * 4


# ==========================================================================
# Colour range and matrix tagging
# ==========================================================================
@pytest.mark.slow
class TestColourDelivery:
    """The delivered file must be limited-range BT.709.

    Source images are JPEG, so the whole pipeline upstream is full range
    (yuvj420p). Shipped untouched, ffprobe reported the finished video as
    `color_range=pc, color_space=bt470bg` - full range tagged as PAL. Players
    that honour the range tag crush blacks; bt470bg is the wrong matrix for HD.
    """

    @pytest.fixture(scope="class")
    def rendered(self, cfg, tmp_path_factory):
        work = tmp_path_factory.mktemp("colour")
        timings = [
            SceneTiming(index=i, image=_make_image(work / f"c{i}.png", 20 + i * 60),
                        duration=1.0, motion="zoom_in")
            for i in range(3)
        ]
        audio = _make_silence(work / "a.wav", 3.0)
        comp = VideoComposer(cfg)
        clips = comp.render_scene_clips(timings, work / "clips", 320, 240,
                                        parallel=2)
        return comp, comp.finalize(clips, [t.duration for t in timings], audio,
                                   None, work / "out.mp4", 320, 240)

    def test_range_is_limited(self, rendered):
        comp, result = rendered
        stream = next(s for s in comp.probe(result.video)["streams"]
                      if s["codec_type"] == "video")
        assert stream.get("color_range") == "tv", (
            f"color_range={stream.get('color_range')} - full range crushes "
            f"blacks on players that respect the tag")

    def test_pixel_format_is_not_the_jpeg_variant(self, rendered):
        comp, result = rendered
        stream = next(s for s in comp.probe(result.video)["streams"]
                      if s["codec_type"] == "video")
        assert stream.get("pix_fmt") == "yuv420p", stream.get("pix_fmt")

    def test_matrix_is_bt709_not_pal(self, rendered):
        comp, result = rendered
        stream = next(s for s in comp.probe(result.video)["streams"]
                      if s["codec_type"] == "video")
        assert stream.get("color_space") == "bt709", (
            f"color_space={stream.get('color_space')} - bt470bg is PAL, not HD")
        assert stream.get("color_primaries") == "bt709"


# ==========================================================================
# Speech-rate calibration
# ==========================================================================
class TestSpeechRateCalibration:
    """Budget words against measured delivery, not a guessed rate.

    The niche profiles guess `words_per_second`, and the guess ran ~20% fast:
    a 45s Short budgeted 127 words at 2.9 wps, edge-tts delivered 2.38, the
    recording came out at 53.3s, and the fix was to speak 28% faster. Speaking
    faster is the wrong lever - budgeting fewer words is the right one.
    """

    @pytest.fixture
    def pipe(self, tmp_path):
        from engine.core.db import Database
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("dry_run", True)
        return Pipeline(cfg, db=Database(tmp_path / "t.db"))

    class _Spec:
        voice_id = "en-US-AriaNeural"

    def _script(self, words: int) -> Script:
        text = " ".join(f"word{i}" for i in range(words))
        return Script(script=text, scenes=[Scene(index=0, narration=text).to_dict()])

    def test_measured_rate_is_stored_and_returned(self, pipe):
        # 120 words over 50s = 2.4 wps.
        pipe._record_speech_rate("en", self._Spec(), self._script(120), 50.0)
        got = pipe.calibrated_words_per_second("en", self._Spec(), 2.9)
        assert got == pytest.approx(2.4, abs=0.01)

    def test_it_is_a_slow_moving_average(self, pipe):
        """One odd script should nudge the estimate, not redefine it."""
        pipe._record_speech_rate("en", self._Spec(), self._script(120), 50.0)
        pipe._record_speech_rate("en", self._Spec(), self._script(200), 50.0)  # 4.0
        got = pipe.calibrated_words_per_second("en", self._Spec(), 2.9)
        assert 2.4 < got < 4.0, got
        assert got == pytest.approx(2.4 * 0.7 + 4.0 * 0.3, abs=0.01)

    def test_rates_are_kept_per_voice(self, pipe):
        class Other:
            voice_id = "hi-IN-SwaraNeural"
        pipe._record_speech_rate("en", self._Spec(), self._script(120), 50.0)
        assert pipe.calibrated_words_per_second("hi", Other(), 2.9) == 2.9, \
            "a measurement for one voice must not leak to another"

    def test_the_profile_guess_is_used_until_there_is_a_measurement(self, pipe):
        assert pipe.calibrated_words_per_second("en", self._Spec(), 2.75) == 2.75

    @pytest.mark.parametrize("words,total", [
        (120, 0.5),      # implausibly short clip
        (5, 50.0),       # too few words to be meaningful
        (120, 5.0),      # 24 wps - impossible, would poison the average
    ])
    def test_implausible_measurements_are_ignored(self, pipe, words, total):
        pipe._record_speech_rate("en", self._Spec(), self._script(words), total)
        assert pipe.calibrated_words_per_second("en", self._Spec(), 2.9) == 2.9

    def test_a_broken_store_never_fails_the_job(self, pipe):
        """Calibration is telemetry; it must not be able to kill a render."""
        def boom(*a, **kw):
            raise RuntimeError("disk on fire")
        pipe.db.get_setting = boom
        pipe._record_speech_rate("en", self._Spec(), self._script(120), 50.0)
        assert pipe.calibrated_words_per_second("en", self._Spec(), 2.9) == 2.9


# ==========================================================================
# Transient handling: wait rather than downgrade
# ==========================================================================
class TestTransientRetry:
    """Free-tier limits are per MINUTE, so falling through is the wrong move.

    Observed on real keys in one run: a single ideas call spent Groq's whole
    8,000 tokens/minute budget, then Gemini answered a transient 503, and the
    pipeline degraded all the way to CPU-only ollama. Twenty-five seconds of
    patience would have kept the good model.
    """

    @pytest.mark.parametrize("message", [
        "groq rate limit reached (free tier)",
        "gemini 429: RESOURCE_EXHAUSTED",
        "gemini 503: The model is overloaded. Please try again later.",
        "groq 502: bad gateway",
        "gemini 504: deadline exceeded",
        "httpx.ReadTimeout: timed out",
        "service temporarily unavailable",
    ])
    def test_transient_conditions_are_retried(self, message):
        assert _is_transient_llm(message) is True

    @pytest.mark.parametrize("message", [
        "groq 401: Invalid API Key",
        "gemini 403: permission denied",
        "groq 404: model_not_found",
        "gemini 400: models/gemini-2.0-flash is not found",
        "groq 400: max_tokens must be an integer",
    ])
    def test_permanent_failures_are_not_retried(self, message):
        assert _is_transient_llm(message) is False

    @pytest.mark.parametrize("message", [
        "groq 400: your prompt used 15034 tokens, over the limit",
        "groq 400: request had 14290 characters",
    ])
    def test_status_codes_are_word_anchored(self, message):
        """`50[234]` unanchored matches inside "15034"; `429` inside "14290".

        Without word boundaries an ordinary message mentioning a token count
        looks like an upstream outage and gets pointlessly retried.
        """
        assert _is_transient_llm(message) is False

    def test_the_pattern_uses_real_escapes_not_control_bytes(self):
        """Guard against a specific corruption that silently disabled this.

        An editing mistake wrote literal 0x08 BACKSPACE bytes into the source
        where the two-character escape `\b` was meant. Inside a raw string
        that makes the pattern match an actual backspace character, so nothing
        ever matched and every transient error was treated as permanent - with
        no syntax error and no failing test to show it.
        """
        source = Path("engine/content/llm.py").read_bytes()
        assert bytes([8]) not in source,             "literal backspace byte in llm.py - the \b escapes are corrupted"

    def test_a_provider_is_retried_before_the_next_one_is_tried(self):
        from engine.content.llm import LLMRouter, LLMResult

        router = LLMRouter([], cfg=None)
        router.retry_backoff = 0.0

        class Flaky:
            name = "flaky"

            def __init__(self):
                self.calls = 0

            def available(self):
                return True

            def complete(self, prompt, **kw):
                self.calls += 1
                if self.calls < 3:
                    raise LLMError("rate limit reached (free tier)")
                return LLMResult(text="ok", provider=self.name, model="m")

        class Weaker:
            name = "weaker"
            used = False

            def available(self):
                return True

            def complete(self, prompt, **kw):
                Weaker.used = True
                return LLMResult(text="worse", provider=self.name, model="m")

        flaky = Flaky()
        router.providers = [flaky, Weaker()]
        result = router.complete("hi")
        assert result.provider == "flaky", "must not downgrade on a rate limit"
        assert flaky.calls == 3
        assert Weaker.used is False

    @pytest.mark.parametrize("message,retry,expected", [
        ("timed out", True, True),                       # hosted: a blip
        ("timed out", False, False),                     # local: our own budget
        ("httpx.ReadTimeout", False, False),
        ("gemini 504: deadline exceeded", False, True),   # far end gave up
        ("groq rate limit reached (free tier)", False, True),
        ("gemini 503: overloaded", False, True),
    ])
    def test_timeouts_are_only_retried_where_that_helps(self, message, retry,
                                                        expected):
        """A timeout means two different things.

        From a hosted provider it is usually a network blip. From a LOCAL model
        it means the request did not fit the time budget we chose, and the
        identical request will not fit next time either. Observed: with both
        hosted providers rate-limited the chain fell through to CPU-only
        ollama, timed out after 300s, and was retried on the same 300s budget -
        ten minutes burned to land in the same place.
        """
        assert _is_transient_llm(message, retry_timeouts=retry) is expected

    def test_ollama_declares_its_timeouts_unretryable(self):
        from engine.content.llm import GroqProvider, OllamaProvider
        assert OllamaProvider.retry_timeouts is False
        assert getattr(GroqProvider, "retry_timeouts", True) is True

    def test_the_router_honours_the_provider_flag(self):
        from engine.content.llm import LLMRouter

        router = LLMRouter([], cfg=None)
        router.retry_backoff = 0.0

        class SlowLocal:
            name = "slowlocal"
            retry_timeouts = False

            def __init__(self):
                self.calls = 0

            def available(self):
                return True

            def complete(self, prompt, **kw):
                self.calls += 1
                raise LLMError("timed out")

        class Fallback:
            name = "fallback"

            def available(self):
                return True

            def complete(self, prompt, **kw):
                from engine.content.llm import LLMResult
                return LLMResult(text="ok", provider=self.name, model="m")

        slow = SlowLocal()
        router.providers = [slow, Fallback()]
        assert router.complete("hi").provider == "fallback"
        assert slow.calls == 1, (
            f"{slow.calls} attempts - a local timeout must not be retried")

    def test_a_permanent_failure_moves_on_immediately(self):
        from engine.content.llm import LLMRouter, LLMResult

        router = LLMRouter([], cfg=None)
        router.retry_backoff = 0.0

        class Broken:
            name = "broken"

            def __init__(self):
                self.calls = 0

            def available(self):
                return True

            def complete(self, prompt, **kw):
                self.calls += 1
                raise LLMError("groq 401: Invalid API Key")

        class Good:
            name = "good"

            def available(self):
                return True

            def complete(self, prompt, **kw):
                return LLMResult(text="ok", provider=self.name, model="m")

        broken = Broken()
        router.providers = [broken, Good()]
        assert router.complete("hi").provider == "good"
        assert broken.calls == 1, "a bad key must not be retried three times"


# ==========================================================================
# Phone-issued OAuth tokens
# ==========================================================================
class TestPhoneAuth:
    """A token from the Android app is bound to a PUBLIC PKCE client.

    It can only be refreshed by that same client id, with no secret.
    Refreshing it with the desktop YOUTUBE_CLIENT_ID/SECRET from .env fails
    with `unauthorized_client`, so connecting from the phone looked like it
    succeeded and then never uploaded anything.
    """

    @pytest.fixture
    def auth(self, tmp_path, monkeypatch):
        from engine.youtube.auth import YouTubeAuth
        monkeypatch.delenv("YOUTUBE_CLIENT_ID", raising=False)
        monkeypatch.delenv("YOUTUBE_CLIENT_SECRET", raising=False)
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        return YouTubeAuth(cfg)

    def test_phone_token_is_authorized_without_env_credentials(self, auth):
        """The whole point: no desktop client needed for the phone flow."""
        assert auth.configured is False
        auth.import_refresh_token("1//refresh-abc", "android-123.apps.googleusercontent.com")
        assert auth.authorized is True

    def test_a_bare_token_is_not_authorized_without_env_credentials(self, auth):
        """Nothing could refresh it, so claiming success would be a lie."""
        auth.import_refresh_token("1//refresh-abc")
        assert auth.authorized is False

    def test_the_issuing_client_is_recorded_as_public(self, auth):
        auth.import_refresh_token("1//refresh-abc", "android-123.apps.googleusercontent.com")
        data = auth.store.read()
        assert data["client_id"] == "android-123.apps.googleusercontent.com"
        assert data["public_client"] is True

    def test_credentials_refresh_with_the_device_client_and_no_secret(self, auth):
        auth.import_refresh_token("1//refresh-abc", "android-123.apps.googleusercontent.com")
        captured = {}

        import google.oauth2.credentials as gc

        class FakeCreds:
            def __init__(self, **kw):
                captured.update(kw)
                self.valid = True
                self.token = "at"
                self.refresh_token = kw["refresh_token"]
                self.token_uri = kw["token_uri"]
                self.scopes = kw["scopes"]
                self.expiry = None

        original = gc.Credentials
        gc.Credentials = FakeCreds
        try:
            auth.credentials()
        finally:
            gc.Credentials = original

        assert captured["client_id"] == "android-123.apps.googleusercontent.com"
        assert captured["client_secret"] is None,             "an Android client has no secret; sending one is rejected"

    def test_a_refresh_does_not_erase_the_recorded_client(self, auth):
        """This made phone auth work exactly once.

        _persist replaced the whole record, so the first successful refresh
        discarded client_id and every later refresh fell back to .env.
        """
        auth.import_refresh_token("1//refresh-abc", "android-123.apps.googleusercontent.com")

        class Creds:
            token = "new-access"
            refresh_token = "1//refresh-abc"
            token_uri = "https://oauth2.googleapis.com/token"
            scopes = ["s"]
            expiry = None

        auth._persist(Creds())
        data = auth.store.read()
        assert data.get("client_id") == "android-123.apps.googleusercontent.com"
        assert data.get("public_client") is True
        assert auth.authorized is True

    def test_desktop_flow_still_uses_the_env_credentials(self, tmp_path, monkeypatch):
        from engine.youtube.auth import YouTubeAuth
        monkeypatch.setenv("YOUTUBE_CLIENT_ID", "desktop-id")
        monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "desktop-secret")
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        auth = YouTubeAuth(cfg)
        auth.import_refresh_token("1//desktop-refresh")     # no client id
        assert auth.authorized is True

        captured = {}
        import google.oauth2.credentials as gc

        class FakeCreds:
            def __init__(self, **kw):
                captured.update(kw)
                self.valid = True
                self.token = "at"
                self.refresh_token = kw["refresh_token"]
                self.token_uri = kw["token_uri"]
                self.scopes = kw["scopes"]
                self.expiry = None

        original = gc.Credentials
        gc.Credentials = FakeCreds
        try:
            auth.credentials()
        finally:
            gc.Credentials = original
        assert captured["client_id"] == "desktop-id"
        assert captured["client_secret"] == "desktop-secret"

    def test_empty_token_is_rejected(self, auth):
        from engine.youtube.auth import AuthError
        with pytest.raises(AuthError):
            auth.import_refresh_token("   ")


# ==========================================================================
# Token store permissions
# ==========================================================================
class TestTokenStorePermissions:
    r"""Hardening must not lock out the process that did the hardening.

    On a machine whose account name contains a hyphen and matches the host
    name ("Sushma-Chandan" on "SUSHMA-CHANDAN"), icacls resolved
    `Sushma-Chandan:F` to the principal `SUSHMA-CHANDAN\` with an EMPTY
    account name. The grant landed nowhere, inheritance had already been
    stripped, and the YouTube token file became unreadable by everyone -
    including the backend that had just written it. Uploads failed with a bare
    PermissionError that pointed at nothing.
    """

    def test_a_written_token_is_still_readable(self, tmp_path):
        from engine.youtube.auth import TokenStore
        store = TokenStore(tmp_path / "secrets" / "token.json")
        store.write({"refresh_token": "1//abc", "scopes": ["s"]})
        assert store.exists() is True
        assert store.read()["refresh_token"] == "1//abc",             "the store locked out its own owner"

    def test_repeated_writes_stay_readable(self, tmp_path):
        """Every refresh rewrites this file, so once is not enough."""
        from engine.youtube.auth import TokenStore
        store = TokenStore(tmp_path / "secrets" / "token.json")
        for n in range(4):
            store.write({"refresh_token": f"1//abc{n}"})
            assert store.read()["refresh_token"] == f"1//abc{n}", f"write {n}"

    @pytest.mark.skipif(os.name != "nt", reason="Windows ACL behaviour")
    def test_the_sid_is_resolvable_on_this_machine(self):
        """Granting by SID is the whole point; if it is empty we silently skip."""
        from engine.youtube.auth import TokenStore
        sid = TokenStore._current_sid()
        assert sid.upper().startswith("S-1-"), f"got {sid!r}"

    @pytest.mark.skipif(os.name != "nt", reason="Windows ACL behaviour")
    def test_granting_by_name_would_have_failed(self, tmp_path):
        """Pins the actual root cause, not just the symptom.

        If this ever starts passing, icacls name resolution changed and the
        comment in _current_sid should be revisited - but granting by SID
        remains correct regardless.
        """
        import subprocess
        target = tmp_path / "probe.json"
        target.write_text("{}", encoding="utf-8")
        user = os.environ.get("USERNAME", "")
        if not user or "-" not in user:
            pytest.skip("this failure needs a hyphenated account name")
        subprocess.run(["icacls", str(target), "/inheritance:r",
                        "/grant:r", f"{user}:F"],
                       capture_output=True, timeout=30, check=False)
        try:
            target.read_text(encoding="utf-8")
            readable = True
        except OSError:
            readable = False
        subprocess.run(["icacls", str(target), "/reset"],
                       capture_output=True, timeout=30, check=False)
        assert readable is False,             "name-based grant now works; the SID approach is still safer"


# ==========================================================================
# The word floor
# ==========================================================================
class TestWordFloor:
    """`min_words` was computed, passed through two signatures, and never used.

    Only overlong scripts were corrected. A real Groq response came back with
    28 words against an 87-word target: valid JSON, decent prose, 14 seconds of
    narration for a 45-second video. The speaking-rate clamp bottoms out around
    -28%, so nothing downstream could rescue it, and it would have failed the
    duration check only after a full render.
    """

    class ShortRouter:
        """Returns a too-short script; optionally complies on the retry."""

        def __init__(self, words: int = 28, comply_on_retry: bool = False,
                     full_words: int = 100):
            self.words = words
            self.comply_on_retry = comply_on_retry
            self.full_words = full_words
            self.calls = 0
            self.saw_nudge = False

        def complete_json(self, prompt, *, system="", temperature=0.8,
                          max_tokens=4096, attempts=2):
            self.calls += 1
            if "TOO SHORT" in prompt:
                self.saw_nudge = True
                n = self.full_words if self.comply_on_retry else self.words
            else:
                n = self.words
            per = max(1, n // 4)
            return {
                "title_ideas": ["A Specific Title"],
                "hook": "The number everyone quotes was withdrawn.",
                "voice_style": "serious",
                "cta": "Check the source.",
                "scenes": [
                    {"role": "value",
                     "narration": " ".join(f"w{i}x{k}" for k in range(per)) + ".",
                     "visual_prompt": f"a photograph {i}",
                     "visual_keywords": ["thing"], "on_screen_text": ""}
                    for i in range(4)
                ],
                "claims": [], "sources": [],
            }, "fakeprovider"

    def _run(self, cfg, router):
        gen = ScriptGenerator(cfg, router=router)
        return gen.generate(make_idea(),
                            build_profile("science", duration_seconds=45),
                            duration=45, video_format="SHORT")

    def test_a_short_script_triggers_a_corrective_retry(self, cfg):
        router = self.ShortRouter(words=28)
        self._run(cfg, router)
        assert router.saw_nudge is True, "the model was never told it was short"
        assert router.calls == 2, f"{router.calls} calls; expected one retry"

    def test_a_complying_retry_is_kept(self, cfg):
        router = self.ShortRouter(words=28, comply_on_retry=True, full_words=110)
        script = self._run(cfg, router)
        assert script.provider == "fakeprovider",             "a good retry must be used, not discarded for the template"
        assert count_words(script.script) >= 90

    def test_a_still_short_retry_falls_back_to_the_template(self, cfg):
        """Formulaic-but-correct-length beats eloquent-but-half-empty."""
        router = self.ShortRouter(words=28, comply_on_retry=False)
        script = self._run(cfg, router)
        assert script.provider == "template"
        # The template builder fills toward the budget.
        assert count_words(script.script) >= 80, count_words(script.script)

    def test_the_result_can_actually_hit_the_target_duration(self, cfg):
        """The point of the floor: a 45s request must not become a 14s video."""
        router = self.ShortRouter(words=28)
        script = self._run(cfg, router)
        profile = build_profile("science", duration_seconds=45)
        seconds = count_words(script.script) / profile.words_per_second
        assert seconds >= 45 * 0.72, (
            f"{seconds:.1f}s of narration for a 45s video is unrecoverable - "
            f"the speaking-rate clamp cannot stretch that far")

    def test_an_adequate_script_is_left_alone(self, cfg):
        router = self.ShortRouter(words=120)
        script = self._run(cfg, router)
        assert router.calls == 1, "no retry needed"
        assert router.saw_nudge is False
        assert script.provider == "fakeprovider"

    def test_the_template_builder_is_not_re_asked(self, cfg):
        """It already fills the budget; re-asking it would loop pointlessly."""
        class DeadRouter:
            calls = 0

            def complete_json(self, prompt, **kw):
                DeadRouter.calls += 1
                raise LLMError("no provider")

        script = self._run(cfg, DeadRouter())
        assert script.provider == "template"
        assert DeadRouter.calls == 1, "the floor check must not re-ask on template"


# ==========================================================================
# The duration check must block
# ==========================================================================
class TestDurationBlocks:
    def test_duration_correct_is_a_blocking_check(self):
        """A real run produced 25.86s for a 45s request - 43% off against a
        25% tolerance - and still scored 95/100 and passed. Handing that to
        the user as a "45 second video" is the gate failing at its one job."""
        from engine.quality.gate import CHECKS
        check = next(c for c in CHECKS if c.name == "duration_correct")
        assert check.blocking is True

    def test_the_other_measurable_failures_still_block(self):
        """Guard against someone relaxing these while tuning."""
        from engine.quality.gate import CHECKS
        must_block = {"file_exists", "playable", "resolution", "audio_present",
                      "audio_not_silent", "duration_correct", "originality",
                      "policy_risk", "kids_compliance", "encoding_compatible",
                      "title_present"}
        blocking = {c.name for c in CHECKS if c.blocking}
        assert must_block <= blocking, must_block - blocking
