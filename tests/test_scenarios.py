"""Cross-cutting scenario tests.

These exercise combinations that behave differently end to end - child-directed
content, long-form 16:9, non-English languages, and the failure paths - without
running a full render (which is covered by scripts/verify_dry_run.py).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from engine.content.metadata import MetadataGenerator
from engine.content.originality import FactChecker, OriginalityChecker
from engine.content.retention import analyze
from engine.content.script import _budget, _structure, build_template_script
from engine.core.config import load_config
from engine.core.models import (ContentIdea, ResearchVideo, Scene, Script,
                                VideoMetadata)
from engine.core.niche import build_profile, is_kids_niche
from engine.core.util import count_words, rfc3339, utc_now
from engine.quality.gate import QualityGate
from engine.research.scoring import score_all
from engine.tts.providers import EDGE_VOICES, resolve_voice
from engine.tts.base import VoiceSpec
from engine.video.captions import CaptionEngine
from engine.video.compose import VideoComposer
from engine.video.music import mood_from_text


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def idea_for(topic: str) -> ContentIdea:
    return ContentIdea(
        topic=topic, angle=f"the part of {topic} nobody explains",
        working_title=f"The Part Of {topic.title()} Nobody Explains",
        hook_concept=f"There is a part of {topic} almost nobody mentions.",
        hook_type="reveal",
        why_now=f"The usual framing of {topic} is well covered.")


# ==========================================================================
class TestKidsScenario:
    """Child-directed content must behave differently at every layer."""

    def test_kids_profile_changes_pacing_and_captions(self):
        kids = build_profile("kids bedtime stories", duration_seconds=45)
        adult = build_profile("science", duration_seconds=45)
        assert kids.made_for_kids and not adult.made_for_kids
        assert kids.words_per_second < adult.words_per_second
        assert kids.scene_seconds > adult.scene_seconds
        assert kids.caption_style == "block"
        assert adult.caption_style == "karaoke"

    def test_kids_word_budget_is_lower_for_same_duration(self):
        kids_target, _, _ = _budget(build_profile("kids stories"), 45)
        adult_target, _, _ = _budget(build_profile("science"), 45)
        assert kids_target < adult_target, "children need fewer words per second"

    def test_kids_script_uses_calm_voice(self):
        profile = build_profile("kids bedtime stories")
        target, _, _ = _budget(profile, 45)
        script = build_template_script(
            idea_for("friendly robots"), profile, 45, "en",
            _structure(45, True), target)
        assert script.voice_style == "calm"

    def test_kids_captions_render_as_block_not_karaoke(self, cfg, tmp_path):
        from tests.test_media import make_clip
        clips = [(0.0, make_clip(0, [(0.1, 0.4, "Once"), (0.6, 0.4, "upon"),
                                     (1.1, 0.4, "a"), (1.6, 0.4, "time.")], 2.2))]
        engine = CaptionEngine(cfg)
        ass_path, _, _ = engine.build(
            clips, tmp_path / "k.ass", tmp_path / "k.srt", 1080, 1920,
            style_override="block")
        ass = ass_path.read_text(encoding="utf-8")
        # Block style fades a whole phrase and never colours a single word.
        assert "\\fad(" in ass
        assert "\\fscx108" not in ass, "block style must not scale a single word"

    def test_kids_content_with_violence_is_blocked(self, cfg):
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="A gentle story", description="D" * 60,
                                   made_for_kids=True),
            script=Script(script="The wolf attacks with a knife and there is blood."),
            profile=build_profile("kids bedtime stories"))
        assert any("kids_compliance" in b for b in report.blockers)
        assert report.passed is False

    def test_kids_flag_mismatch_is_blocked(self, cfg):
        """A kids niche published without the flag is a compliance failure."""
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="A gentle story", description="D" * 60,
                                   made_for_kids=False),
            script=Script(script="A friendly bear finds a warm cave."),
            profile=build_profile("kids bedtime stories"))
        assert any("kids_compliance" in b for b in report.blockers)

    def test_clean_kids_content_passes_compliance(self, cfg):
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="The Bear Who Found A Cave",
                                   description="D" * 60, made_for_kids=True),
            script=Script(script="A friendly bear found a warm cave and slept."),
            profile=build_profile("kids bedtime stories"))
        kid_check = next(c for c in report.checks if c["name"] == "kids_compliance")
        assert kid_check["passed"] is True

    def test_kids_description_states_no_external_links(self, cfg):
        gen = MetadataGenerator(cfg, None)
        profile = build_profile("kids bedtime stories")
        script = Script(hook="A bear found a cave.",
                        script="A friendly bear found a warm cave and slept well.")
        meta = gen.build(script, idea_for("friendly bears"), profile,
                         made_for_kids=True)
        assert "Made for children" in meta.description
        assert meta.made_for_kids is True

    def test_kids_niche_detection_covers_common_phrasings(self):
        for niche in ("kids bedtime stories", "nursery rhymes", "toddler learning",
                      "cartoon for kids", "preschool songs"):
            assert is_kids_niche(niche) is True, niche
        for niche in ("science", "finance", "gaming"):
            assert is_kids_niche(niche) is False, niche


# ==========================================================================
class TestLongformScenario:
    def test_resolution_is_landscape(self, cfg):
        composer = VideoComposer(cfg)
        assert composer.resolution("LONGFORM") == (1920, 1080)
        assert composer.resolution("SHORT") == (1080, 1920)

    def test_longform_structure_includes_a_promise_beat(self):
        roles = [r for r, _, _ in _structure(600, False)]
        assert "promise" in roles
        assert roles[0] == "hook"

    def test_longform_scene_count_scales_with_duration(self):
        profile = build_profile("history", duration_seconds=600)
        _, _, short_scenes = _budget(build_profile("history", duration_seconds=45), 45)
        _, _, long_scenes = _budget(profile, 600)
        assert long_scenes > short_scenes
        assert long_scenes <= 24, "scene count must stay bounded"

    def test_longform_captions_are_smaller_and_wider(self, cfg, tmp_path):
        from tests.test_media import make_clip
        clips = [(0.0, make_clip(0, [
            (i * 0.3, 0.25, f"word{i}") for i in range(10)], 3.5))]
        engine = CaptionEngine(cfg)
        port, _, port_groups = engine.build(
            clips, tmp_path / "p.ass", tmp_path / "p.srt", 1080, 1920)
        land, _, land_groups = engine.build(
            clips, tmp_path / "l.ass", tmp_path / "l.srt", 1920, 1080)

        def font_size(text: str) -> int:
            line = next(l for l in text.splitlines() if l.startswith("Style:"))
            return int(line.split(",")[2])

        assert font_size(land.read_text(encoding="utf-8")) < \
            font_size(port.read_text(encoding="utf-8"))
        assert land_groups <= port_groups, "wider frame fits more words per card"

    def test_longform_requires_thumbnail_shorts_do_not(self, cfg):
        gate = QualityGate(cfg)
        script = Script(script="Body text.", hook="Hook.")
        meta = VideoMetadata(title="A Title", description="D" * 60)
        long_report = gate.evaluate(video=None, metadata=meta, script=script,
                                    profile=build_profile("history"),
                                    video_format="LONGFORM")
        short_report = gate.evaluate(video=None, metadata=meta, script=script,
                                     profile=build_profile("history"),
                                     video_format="SHORT")
        long_thumb = next(c for c in long_report.checks
                          if c["name"] == "thumbnail_present")
        short_thumb = next(c for c in short_report.checks
                           if c["name"] == "thumbnail_present")
        assert long_thumb["passed"] is False
        assert short_thumb["passed"] is True

    def test_longform_gets_chapters(self, cfg):
        gen = MetadataGenerator(cfg, None)
        scenes = [Scene(index=i, narration=f"Section {i} explains something.",
                        duration=40.0, role="value").to_dict() for i in range(6)]
        script = Script(hook="Hook.", scenes=scenes,
                        script=" ".join(s["narration"] for s in scenes))
        chapters = gen.build_chapters(script)
        assert chapters, "long-form needs chapters"
        assert chapters[0]["seconds"] == 0.0, "YouTube requires a 00:00 chapter"
        assert all(c["seconds"] >= 0 for c in chapters)


# ==========================================================================
class TestLanguageScenario:
    @pytest.mark.parametrize("language,expected_prefix", [
        ("en", "en-US"),
        ("en-IN", "en-IN"),
        ("hi", "hi-IN"),
        ("ta", "ta-IN"),
        ("te", "te-IN"),
        ("es", "es-ES"),
    ])
    def test_voice_resolves_per_language(self, language, expected_prefix):
        voice = resolve_voice(VoiceSpec(language=language, gender="female"))
        assert voice.startswith(expected_prefix), f"{language} -> {voice}"

    def test_unknown_language_falls_back_to_english(self):
        voice = resolve_voice(VoiceSpec(language="xx-YY", gender="female"))
        assert voice.startswith("en-")

    def test_both_genders_available_for_english_and_hindi(self):
        for lang in ("en", "hi", "en-IN"):
            table = EDGE_VOICES[lang]
            assert table.get("female"), lang
            assert table.get("male"), lang
            female = resolve_voice(VoiceSpec(language=lang, gender="female"))
            male = resolve_voice(VoiceSpec(language=lang, gender="male"))
            assert female != male

    def test_non_english_language_is_recorded_on_the_script(self):
        profile = build_profile("science")
        target, _, _ = _budget(profile, 45)
        script = build_template_script(idea_for("space"), profile, 45, "hi",
                                       _structure(45, True), target)
        assert script.language == "hi"

    def test_metadata_carries_language_for_youtube(self, cfg):
        gen = MetadataGenerator(cfg, None)
        meta = gen.build(
            Script(hook="H", script="Some narration about space exploration."),
            idea_for("space"), build_profile("science"), language="hi")
        assert meta.language == "hi"

    def test_upload_body_sets_language_fields(self, cfg):
        from engine.youtube.upload import YouTubeUploader
        body = YouTubeUploader(cfg).build_body(
            VideoMetadata(title="t", description="d", language="hi"),
            schedule=False)
        assert body["snippet"]["defaultLanguage"] == "hi"
        assert body["snippet"]["defaultAudioLanguage"] == "hi"


# ==========================================================================
class TestNicheAdaptation:
    """The engine must not be hard-coded to one niche (spec section 8)."""

    @pytest.mark.parametrize("niche", [
        "technology", "AI", "science", "space", "history", "interesting facts",
        "education", "kids bedtime stories", "storytelling", "productivity",
        "finance basics", "gaming", "travel", "cars", "programming",
        "business", "general entertainment", "competitive duck herding",
    ])
    def test_every_niche_yields_a_complete_usable_profile(self, niche):
        profile = build_profile(niche, duration_seconds=45)
        assert profile.name == niche
        assert profile.tone and profile.visual_style and profile.hook_style
        assert 1.2 <= profile.words_per_second <= 4.0
        assert 1.5 <= profile.scene_seconds <= 8.0
        assert profile.music_mood
        assert profile.search_modifiers
        # A profile must always produce a usable prompt block.
        block = profile.prompt_block()
        assert "NICHE:" in block and "VISUAL STYLE:" in block

    @pytest.mark.parametrize("niche,expected_mood", [
        ("science", "cinematic"),
        ("kids bedtime stories", "playful"),
        ("technology", "tech"),
    ])
    def test_music_mood_follows_the_niche(self, niche, expected_mood):
        profile = build_profile(niche)
        assert mood_from_text(profile.music_mood) == expected_mood

    def test_sensitive_niches_get_disclaimers_and_restrictions(self):
        for niche in ("finance basics", "health myths", "crypto investing"):
            profile = build_profile(niche)
            assert profile.is_sensitive is True, niche
            assert profile.disclaimers, niche
            assert profile.restrictions, niche

    def test_visual_style_actually_differs_between_niches(self):
        styles = {build_profile(n).visual_style
                  for n in ("science", "history", "finance basics",
                            "kids bedtime stories", "gaming")}
        assert len(styles) == 5, "each family needs its own visual identity"


# ==========================================================================
class TestFailurePaths:
    def test_empty_research_produces_a_usable_fallback_idea(self):
        from engine.content.ideas import build_structural_ideas
        ideas = build_structural_ideas("science", build_profile("science"),
                                       [], [], 5)
        assert ideas, "must never return zero ideas"
        assert ideas[0].hook_concept
        assert "no_research" in ideas[0].risk_flags

    def test_script_with_no_scenes_does_not_crash_retention(self):
        report = analyze(Script(script="", scenes=[]), build_profile("science"))
        assert report.score == 0.0
        assert report.notes

    def test_originality_handles_empty_corpus(self, cfg):
        checker = OriginalityChecker(cfg, None)
        result = checker.check(Script(script="Original text.", hook="Original."),
                               idea_for("space"), [])
        assert result.passed is True
        assert result.report["research_sources"] == []

    def test_factchecker_handles_empty_script(self, cfg):
        result = FactChecker(cfg).check(Script(script=""), build_profile("science"))
        assert result.risk in {"low", "medium", "high"}

    def test_quality_gate_survives_completely_empty_input(self, cfg):
        report = QualityGate(cfg).evaluate(
            video=None, metadata=VideoMetadata(), script=Script(),
            profile=build_profile("science"))
        assert report.passed is False
        assert report.blockers
        assert 0.0 <= report.score <= 100.0

    def test_scoring_handles_a_video_with_no_channel_data(self):
        video = ResearchVideo(
            video_id="x", title="A Title", channel_id="", channel_title="",
            published_at=rfc3339(utc_now() - timedelta(days=3)),
            duration_seconds=45, views=1000)
        score_all([video])
        assert video.performance_ratio == 0.0
        assert video.is_breakout is False
        assert 0.0 <= video.viral_score <= 100.0

    def test_malformed_published_date_does_not_crash(self):
        video = ResearchVideo(
            video_id="x", title="A Title", channel_id="c", channel_title="C",
            published_at="not-a-date", duration_seconds=45, views=1000)
        score_all([video])
        assert video.age_days == 0.0
        assert video.view_velocity > 0


# ==========================================================================
class TestOriginalityDoesNotFalsePositive:
    """Regression: sharing a topic word must not read as plagiarism."""

    def _video(self, title: str, description: str = "") -> ResearchVideo:
        return ResearchVideo(
            video_id="v", title=title, channel_id="c", channel_title="Ch",
            published_at=rfc3339(utc_now() - timedelta(days=5)),
            duration_seconds=45, views=1000, description=description)

    def test_shared_topic_word_with_short_title_does_not_block(self, cfg):
        """The exact failure seen in end-to-end testing."""
        script = Script(
            hook="One team changed how we understand space.",
            script=("One team changed how we understand space. Space is usually "
                    "explained the same way every time. Most explanations stop "
                    "at the surface."))
        checker = OriginalityChecker(cfg, None)
        result = checker.check(
            script, idea_for("space"),
            [self._video("Why Space Is Dark",
                         "A short video covering why space is dark.")])
        assert result.passed is True, result.findings

    def test_genuine_reuse_still_blocks(self, cfg):
        shared = ("black holes bend light so strongly that nothing escapes once "
                  "it crosses the event horizon which is why they look black")
        checker = OriginalityChecker(cfg, None)
        result = checker.check(
            Script(hook="Black holes bend light.", script=shared),
            idea_for("black holes"),
            [self._video("Black Holes Explained", shared)])
        assert result.passed is False
        assert any("reuses" in f for f in result.findings)

    def test_hook_echoing_a_title_warns_without_blocking(self, cfg):
        checker = OriginalityChecker(cfg, None)
        result = checker.check(
            Script(hook="Why do black holes spin so fast",
                   script="An entirely separate body of narration text here."),
            idea_for("black holes"),
            [self._video("Why Do Black Holes Spin So Fast?",
                         "Unrelated description text.")])
        assert result.passed is True
        assert any("echoes the title" in f for f in result.findings)


class TestNarrationHygiene:
    """Regression: internal analysis text must never be spoken."""

    def test_analysis_fields_are_not_narrated_verbatim(self):
        from engine.content.script import _narratable
        leaks = [
            "1 angles on 'space' are already saturated; this one is not.",
            "derived from structural gap analysis without an LLM",
            "gap score 0.62, momentum 0.48",
            "cluster: black holes, n=5",
        ]
        for text in leaks:
            assert _narratable(text) == "", f"would have been spoken: {text}"

    def test_plain_sentences_are_allowed(self):
        from engine.content.script import _narratable
        assert _narratable("New imaging changed what we can see")
        assert _narratable("the story of the people involved")

    def test_template_script_narration_is_clean(self):
        from engine.content.ideas import build_structural_ideas
        from engine.core.models import ContentGap
        profile = build_profile("science", duration_seconds=40)
        gaps = [ContentGap(
            topic="space", common_angles=["listicle"],
            missing_angles=["human_story: the story of the people involved"],
            gap_score=0.6)]
        idea = build_structural_ideas("science", profile, gaps, [], 1)[0]
        target, _, _ = _budget(profile, 40)
        script = build_template_script(idea, profile, 40, "en",
                                       _structure(40, True), target)
        forbidden = ["saturated", "gap score", "momentum", "cluster", "n=",
                     "without an LLM", "structural"]
        lowered = script.script.lower()
        for token in forbidden:
            assert token.lower() not in lowered, \
                f"internal text '{token}' leaked into narration"
        assert count_words(script.script) > 20
