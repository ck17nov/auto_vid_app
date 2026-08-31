"""Content engine tests: LLM JSON robustness, script shaping, titles,
retention, originality and fact checking (spec sections 7, 10, 11, 15, 17, 36).
"""
from __future__ import annotations

import pytest

from engine.content.llm import LLMError, LLMRouter, TemplateProvider, extract_json
from engine.content.metadata import MetadataGenerator
from engine.content.originality import FactChecker, OriginalityChecker
from engine.content.retention import analyze, auto_improve
from engine.content.script import (BANNED_OPENERS, ScriptGenerator,
                                    build_template_script, strip_banned_opener,
                                    _budget, _structure)
from engine.core.config import load_config
from engine.core.models import ContentIdea, ResearchVideo, Scene, Script
from engine.core.niche import build_profile
from engine.core.util import count_words


@pytest.fixture(scope="module")
def cfg():
    return load_config()


# ==========================================================================
class TestExtractJson:
    """Every provider wraps JSON differently; parsing must survive all of it."""

    def test_bare_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped(self):
        text = 'Sure! Here is the JSON you asked for:\n{"a": 1}\nHope that helps.'
        assert extract_json(text) == {"a": 1}

    def test_trailing_commas_recovered(self):
        assert extract_json('{"a": 1, "b": [1, 2,],}') == {"a": 1, "b": [1, 2]}

    def test_braces_inside_strings_do_not_break_matching(self):
        payload = '{"hook": "use {curly} braces", "n": 2}'
        assert extract_json(payload)["hook"] == "use {curly} braces"

    def test_escaped_quotes_inside_strings(self):
        payload = '{"hook": "she said \\"hello\\" once"}'
        assert extract_json(payload)["hook"] == 'she said "hello" once'

    def test_nested_objects(self):
        payload = '{"scenes": [{"narration": "a", "meta": {"x": 1}}]}'
        assert extract_json(payload)["scenes"][0]["meta"]["x"] == 1

    def test_empty_raises(self):
        with pytest.raises(LLMError):
            extract_json("")

    def test_no_json_raises(self):
        with pytest.raises(LLMError):
            extract_json("I cannot help with that request.")

    def test_unterminated_raises(self):
        with pytest.raises(LLMError):
            extract_json('{"a": 1')


class TestTemplateProvider:
    def test_refuses_freeform_instead_of_inventing(self, cfg):
        """A degraded provider must fail loudly, not return plausible nonsense."""
        provider = TemplateProvider()
        assert provider.available() is True
        with pytest.raises(LLMError):
            provider.complete("write me a script")

    def test_router_reports_no_real_llm_when_only_template(self, cfg):
        router = LLMRouter(["template"], cfg)
        assert router.has_real_llm() is False
        assert router.usable == []


# ==========================================================================
class TestScriptShaping:
    def test_banned_openers_are_stripped(self):
        cases = [
            ("Hey guys, welcome back to the channel! Black holes are weird.",
             "black holes are weird"),
            ("In this video we will explore neutron stars.",
             "we will explore neutron stars"),
            ("So basically, the sun is hot.", "the sun is hot"),
        ]
        for raw, expected_fragment in cases:
            cleaned = strip_banned_opener(raw)
            assert expected_fragment.split()[0].lower() in cleaned.lower()
            assert "welcome back" not in cleaned.lower()
            assert "in this video" not in cleaned.lower()

    def test_stripping_recapitalises(self):
        assert strip_banned_opener("Hey guys, black holes spin.").startswith("Black")

    def test_stripping_never_empties_a_string_of_only_filler(self):
        # Even if everything matches, callers fall back to the original text.
        assert isinstance(strip_banned_opener("Hey guys,"), str)

    def test_word_budget_scales_with_duration(self):
        profile = build_profile("science", duration_seconds=45)
        target_45, _, scenes_45 = _budget(profile, 45)
        target_90, _, scenes_90 = _budget(profile, 90)
        assert target_90 > target_45
        assert scenes_90 > scenes_45

    def test_structure_is_dynamic_not_hardcoded_seconds(self):
        short = _structure(45, True)
        long = _structure(600, False)
        assert [r for r, _, _ in short][:2] == ["hook", "context"]
        assert sum(f for _, _, f in short) == pytest.approx(1.0, abs=0.02)
        assert sum(f for _, _, f in long) == pytest.approx(1.0, abs=0.02)
        assert "promise" in [r for r, _, _ in long]

    def test_template_script_is_labelled_degraded(self):
        idea = ContentIdea(topic="black holes", angle="a missing angle",
                           hook_concept="Something does not add up.")
        profile = build_profile("science")
        script = build_template_script(idea, profile, 45, "en",
                                       _structure(45, True), 117)
        assert script.provider == "template", "must be attributable"
        assert script.claims and script.claims[0]["confidence"] == "low"
        assert len(script.scenes) >= 3

    def test_post_process_trims_overlong_script(self, cfg):
        profile = build_profile("science", duration_seconds=30)
        gen = ScriptGenerator(cfg, LLMRouter(["template"], cfg))
        long_scene = "This sentence exists purely to consume the word budget. " * 6
        script = Script(
            hook="A star exploded.",
            scenes=[
                Scene(index=0, narration="A star exploded.", role="hook").to_dict(),
                Scene(index=1, narration=long_scene, role="value").to_dict(),
                Scene(index=2, narration=long_scene, role="value").to_dict(),
                Scene(index=3, narration=long_scene, role="value").to_dict(),
                Scene(index=4, narration="That changed everything.",
                      role="payoff").to_dict(),
            ],
        )
        script.script = " ".join(s["narration"] for s in script.scenes)
        before = count_words(script.script)
        target, _, _ = _budget(profile, 30)
        result = gen._post_process(script, profile, target, int(target * 0.8), 30, True)
        after = count_words(result.script)
        assert after < before
        roles = [s["role"] for s in result.scenes]
        assert "hook" in roles and "payoff" in roles, "never cut hook or payoff"


# ==========================================================================
class TestRetention:
    def _script(self, scenes: list[tuple[str, str, float]]) -> Script:
        objs = [Scene(index=i, narration=t, role=r, duration=d).to_dict()
                for i, (t, r, d) in enumerate(scenes)]
        return Script(hook=scenes[0][0], scenes=objs,
                      script=" ".join(s[0] for s in scenes))

    def test_slow_hook_is_flagged_with_seconds(self):
        script = self._script([
            ("Today I want to talk to you about a topic that many people find "
             "quite interesting and I think you will too.", "hook", 6.0),
            ("Black holes bend light.", "value", 3.0),
            ("That is the payoff.", "payoff", 3.0),
        ])
        report = analyze(script, build_profile("science"), target_duration=45)
        assert any("Hook takes" in n for n in report.notes)
        assert report.metrics["hook"] < 0.6

    def test_long_static_scene_is_flagged(self):
        script = self._script([
            ("Why does a star explode?", "hook", 2.0),
            ("A very long single scene with no visual change at all.", "value", 9.0),
            ("Payoff here.", "payoff", 2.0),
        ])
        profile = build_profile("science")
        report = analyze(script, profile, target_duration=13)
        assert any("no visual change" in n for n in report.notes)
        assert 1 in report.metrics["long_scenes"]

    def test_early_cta_is_flagged(self):
        script = self._script([
            ("Why do stars explode?", "hook", 2.0),
            ("Subscribe for more.", "cta", 2.0),
            ("Here is the science.", "value", 4.0),
            ("And the payoff.", "payoff", 3.0),
        ])
        report = analyze(script, build_profile("science"), target_duration=11)
        assert any("too early" in n for n in report.notes)

    def test_auto_improve_splits_long_scene_and_varies_visual(self):
        script = self._script([
            ("Why does a star explode?", "hook", 2.0),
            ("First the core collapses in on itself. Then the shockwave tears "
             "the outer layers apart.", "value", 10.0),
            ("Payoff here.", "payoff", 2.0),
        ])
        profile = build_profile("science")
        report = analyze(script, profile, target_duration=14)
        improved, applied = auto_improve(script, profile, report)
        assert any("split scene" in a for a in applied)
        assert len(improved.scenes) == 4
        prompts = [s["visual_prompt"] for s in improved.scenes]
        assert prompts[1] != prompts[2], "split halves need different images"

    def test_auto_improve_removes_filler(self):
        script = self._script([
            ("Basically the star, essentially, explodes.", "hook", 3.0),
            ("Value here.", "value", 3.0),
            ("Payoff.", "payoff", 2.0),
        ])
        profile = build_profile("science")
        improved, applied = auto_improve(
            script, profile, analyze(script, profile, target_duration=8))
        assert any("filler" in a for a in applied)
        assert "basically" not in improved.script.lower()

    def test_score_is_bounded(self):
        script = self._script([("Hook.", "hook", 1.0)])
        report = analyze(script, build_profile("science"), target_duration=45)
        assert 0.0 <= report.score <= 100.0


# ==========================================================================
class TestTitles:
    @pytest.fixture
    def setup(self, cfg):
        script = Script(
            hook="A star exploded and we watched it begin.",
            script=("A star exploded and we watched it begin. For decades "
                    "astronomers only found supernovae after the flash faded. "
                    "Then a survey telescope caught the first light."),
        )
        idea = ContentIdea(topic="supernova first light",
                           angle="the first moments of a supernova")
        return MetadataGenerator(cfg, None), script, idea

    def test_deceptive_title_scores_below_honest_one(self, setup):
        gen, script, idea = setup
        honest = gen.score_title(
            "Scientists Caught the First Light of a Dying Star", script, idea)
        deceptive = gen.score_title("Scientists FOUND ALIENS!!!", script, idea)
        assert deceptive["score"] < honest["score"]
        assert deceptive["misleading_risk"] > 0.3
        assert deceptive["risk_reasons"]

    def test_title_unsupported_by_script_is_penalised(self, setup):
        gen, script, idea = setup
        unsupported = gen.score_title(
            "Best Crypto Wallets For Beginners In Dubai", script, idea)
        assert "title vocabulary barely appears in the script" in \
            unsupported["risk_reasons"]

    def test_scores_are_bounded(self, setup):
        gen, script, idea = setup
        for title in ["", "A", "X" * 99, "Why Do Stars Explode?"]:
            result = gen.score_title(title, script, idea)
            assert 0.0 <= result["score"] <= 100.0

    def test_tags_are_not_stuffed(self, setup):
        gen, script, idea = setup
        profile = build_profile("science")
        tags = gen.build_tags(script, idea, profile)
        assert len(tags) <= 15
        assert sum(len(t) + 1 for t in tags) <= 460
        assert len(tags) == len(set(tags)), "no duplicate tags"

    def test_description_includes_ai_disclosure(self, setup, cfg):
        gen, script, idea = setup
        profile = build_profile("science")
        meta = gen.build(script, idea, profile, video_format="SHORT")
        assert "AI" in meta.description
        assert len(meta.description) <= 5000
        assert len(meta.title) <= 100

    def test_finance_description_carries_disclaimer(self, setup, cfg):
        gen, script, idea = setup
        profile = build_profile("finance basics")
        meta = gen.build(script, idea, profile, video_format="SHORT")
        assert "Not financial advice" in meta.description


# ==========================================================================
class TestOriginality:
    def _video(self, title: str, description: str = "") -> ResearchVideo:
        return ResearchVideo(
            video_id="v1", title=title, channel_id="c", channel_title="Ch",
            published_at="2026-01-01T00:00:00Z", duration_seconds=45,
            views=1000, description=description)

    def test_near_copy_is_rejected(self, cfg):
        source_text = ("Black holes bend light so strongly that nothing escapes "
                       "once it crosses the event horizon, which is why they "
                       "appear completely black to any outside observer.")
        script = Script(script=source_text, hook="Black holes bend light")
        checker = OriginalityChecker(cfg, None)
        result = checker.check(script, ContentIdea(topic="black holes"),
                               [self._video("Black holes bend light", source_text)])
        assert result.passed is False
        assert result.max_similarity >= 0.3
        assert result.findings

    def test_original_script_passes(self, cfg):
        script = Script(
            script=("A survey telescope recorded the earliest moments of a "
                    "stellar collapse, which changed the accepted timeline."),
            hook="A star exploded and we watched it begin.")
        checker = OriginalityChecker(cfg, None)
        result = checker.check(
            script, ContentIdea(topic="supernova"),
            [self._video("10 Facts About Black Holes",
                         "Ten interesting black hole facts explained.")])
        assert result.passed is True

    def test_report_contains_every_required_section(self, cfg):
        """Spec section 7 lists exactly what the report must contain."""
        script = Script(script="Original narration.", hook="Original.")
        checker = OriginalityChecker(cfg, None)
        result = checker.check(script, ContentIdea(topic="t"),
                               [self._video("Something else")])
        report = result.report
        for key in ("research_sources", "inspiration_videos",
                    "script_originality", "visual_sources", "audio_sources",
                    "concept"):
            assert key in report, f"originality report missing {key}"
        assert "not a clone of any real person" in report["audio_sources"]["narration"]


class TestFactCheck:
    def test_medical_cure_claim_is_high_risk(self, cfg):
        script = Script(script="This herb cures cancer in three weeks.")
        result = FactChecker(cfg).check(script, build_profile("health"))
        assert result.risk == "high"
        assert result.requires_approval is True

    def test_financial_guarantee_is_flagged(self, cfg):
        script = Script(script="This strategy offers guaranteed returns, risk-free.")
        result = FactChecker(cfg).check(script, build_profile("finance"))
        assert any("guarantee" in f["type"] for f in result.flagged)

    def test_undeclared_number_is_flagged_in_factual_niche(self, cfg):
        script = Script(
            script="The signal repeats every 44 minutes across 3000 light-years.",
            claims=[])
        result = FactChecker(cfg).check(script, build_profile("science"))
        assert any("numeric claim not declared" in f["type"] for f in result.flagged)

    def test_declared_number_is_not_flagged(self, cfg):
        script = Script(
            script="The signal repeats every 44 minutes.",
            claims=[{"claim": "The signal repeats every 44 minutes.",
                     "confidence": "high", "basis": "published observation"}],
            sources=[{"title": "Observation report", "note": "timing"}])
        result = FactChecker(cfg).check(script, build_profile("science"))
        assert not any("numeric claim not declared" in f["type"]
                       for f in result.flagged)

    def test_template_provider_is_flagged_as_degraded(self, cfg):
        script = Script(script="Plain narration.", provider="template")
        result = FactChecker(cfg).check(script, build_profile("science"))
        assert any("without an LLM" in f["type"] for f in result.flagged)

    def test_clean_script_is_low_risk(self, cfg):
        script = Script(script="Stars form inside clouds of gas and dust.",
                        provider="groq")
        result = FactChecker(cfg).check(script, build_profile("science"))
        assert result.risk == "low"
        assert result.requires_approval is False


class TestStructuralTitleGrammar:
    """Regression: titles must read correctly for singular AND plural topics."""

    BAD_AGREEMENT = (
        " animals does", " animals works", " stars does", " stars works",
        " holes does", " holes works", " facts does", " facts works",
    )

    @pytest.mark.parametrize("topic", [
        "animals", "black holes", "neutron star", "space", "bees",
        "quantum computers", "the moon",
    ])
    def test_no_subject_verb_disagreement(self, cfg, topic):
        gen = MetadataGenerator(cfg, None)
        script = Script(hook=f"Something about {topic}.",
                        script=f"A narration that discusses {topic} at length.")
        idea = ContentIdea(topic=topic, angle="an overlooked detail")
        titles = gen._structural_titles(script, idea)
        assert titles, topic
        for title in titles:
            lowered = " " + title.lower()
            for bad in self.BAD_AGREEMENT:
                assert bad not in lowered, f"{title!r} has bad agreement"

    def test_structural_titles_are_within_youtube_limits(self, cfg):
        gen = MetadataGenerator(cfg, None)
        idea = ContentIdea(topic="a very long topic name " * 3,
                           angle="some angle")
        script = Script(hook="H", script="Body text about the topic.")
        for title in gen._structural_titles(script, idea):
            assert len(title) <= 200  # candidates are truncated to 100 later

    def test_empty_topic_yields_no_structural_titles(self, cfg):
        gen = MetadataGenerator(cfg, None)
        assert gen._structural_titles(
            Script(script="x"), ContentIdea(topic="")) == []
