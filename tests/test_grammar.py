"""Generated-copy grammar tests.

These are regressions for defects found by watching real rendered output:
  * "Animals is usually explained..."   - plural subject, singular verb
  * "What Animals Actually Does"         - same, in a title
  * "WHAT SPACE ACTUALLY"                - thumbnail cut off mid-thought
  * "TRUTH ANIMALS"                      - non-adjacent words joined into a
                                           phrase that was never written
"""
from __future__ import annotations

import pytest

from engine.content.ideas import build_structural_ideas
from engine.content.script import _budget, _structure, build_template_script
from engine.core.config import load_config
from engine.core.models import ContentGap
from engine.core.niche import build_profile
from engine.core.util import agree, subject_is_plural
from engine.thumbnail.generator import _headline


@pytest.fixture(scope="module")
def cfg():
    return load_config()


class TestPluralDetection:
    @pytest.mark.parametrize("topic,plural", [
        ("animals", True),
        ("black holes", True),
        ("bees", True),
        ("AI agents", True),
        ("people", True),
        ("neutron star", False),
        ("space", False),
        ("the moon", False),
        ("physics", False),
        ("news", False),
        ("a species", False),
        ("business", False),
        ("analysis", False),
        ("", False),
    ])
    def test_head_noun_decides(self, topic, plural):
        assert subject_is_plural(topic) is plural, topic

    def test_agree_picks_the_right_form(self):
        assert agree("animals", "is", "are") == "are"
        assert agree("neutron star", "is", "are") == "is"
        assert agree("", "is", "are") == "is"


class TestNarrationAgreement:
    """The LLM-free path must not produce "Animals is".

    The check targets the constructions where the TOPIC is the grammatical
    subject. A blanket substring scan is wrong: "The consequence of black holes
    is stranger than black holes" is correct English, because the subject is
    "the consequence", not "holes".
    """

    # (template with {t} and {v}) - {v} must be the form agreeing with {t}.
    SUBJECT_CONSTRUCTIONS = [
        "{t} {v} usually explained",
        "there is a reason {t} {v} described",
        "what {v} {t}, in plain terms",
    ]

    def _assert_agrees(self, text: str, topic: str) -> None:
        lowered = text.lower()
        right = agree(topic, "is", "are")
        wrong = "are" if right == "is" else "is"
        for template in self.SUBJECT_CONSTRUCTIONS:
            bad = template.format(t=topic.lower(), v=wrong)
            assert bad not in lowered, f"{topic}: wrong agreement in {text!r}"

    @pytest.mark.parametrize("topic", [
        "animals", "black holes", "neutron star", "space", "bees",
        "AI agents", "the moon",
    ])
    def test_template_script_agrees(self, topic):
        profile = build_profile("science", duration_seconds=30)
        gaps = [ContentGap(
            topic=topic, common_angles=["listicle"],
            missing_angles=["explainer: a plain explanation",
                            "mechanism: the underlying cause",
                            "consequence: what it leads to",
                            "hidden: something overlooked"],
            gap_score=0.6)]
        ideas = build_structural_ideas("science", profile, gaps, [], 4)
        target, _, _ = _budget(profile, 30)
        for idea in ideas:
            script = build_template_script(idea, profile, 30, "en",
                                           _structure(30, True), target)
            self._assert_agrees(script.script, topic)

    @pytest.mark.parametrize("topic", [
        "animals", "black holes", "neutron star", "space", "bees",
    ])
    def test_idea_hooks_and_titles_agree(self, topic):
        profile = build_profile("science")
        gaps = [ContentGap(
            topic=topic, common_angles=[],
            missing_angles=["explainer: a plain explanation",
                            "mechanism: the underlying cause",
                            "consequence: what it leads to"],
            gap_score=0.6)]
        for idea in build_structural_ideas("science", profile, gaps, [], 3):
            self._assert_agrees(idea.hook_concept, topic)
            self._assert_agrees(idea.working_title, topic)

    @pytest.mark.parametrize("topic,expected", [
        ("animals", "animals are usually explained"),
        ("black holes", "black holes are usually explained"),
        ("neutron star", "neutron star is usually explained"),
        ("space", "space is usually explained"),
    ])
    def test_the_context_beat_uses_the_correct_verb(self, topic, expected):
        """Positive assertion, so a silently-dropped agree() call is caught."""
        profile = build_profile("science", duration_seconds=30)
        gaps = [ContentGap(topic=topic, common_angles=[],
                           missing_angles=["hidden: something overlooked"],
                           gap_score=0.6)]
        idea = build_structural_ideas("science", profile, gaps, [], 1)[0]
        target, _, _ = _budget(profile, 30)
        script = build_template_script(idea, profile, 30, "en",
                                       _structure(30, True), target)
        assert expected in script.script.lower(), script.script[:160]

    def test_explainer_hook_uses_the_correct_verb(self):
        profile = build_profile("science")
        for topic, expected in (("animals", "what are animals"),
                                ("neutron star", "what is neutron star")):
            gaps = [ContentGap(topic=topic, common_angles=[],
                               missing_angles=["explainer: a plain explanation"],
                               gap_score=0.6)]
            idea = build_structural_ideas("science", profile, gaps, [], 1)[0]
            assert expected in idea.hook_concept.lower(), idea.hook_concept

    def test_no_placeholder_braces_survive_into_copy(self):
        """A missing .format() key would ship "{v}" to the viewer."""
        profile = build_profile("science")
        gaps = [ContentGap(topic="black holes", common_angles=[],
                           missing_angles=[f"{k}: x" for k in
                                           ("explainer", "mechanism",
                                            "consequence", "hidden", "origin",
                                            "human_story", "extreme", "future",
                                            "comparison", "practical",
                                            "listicle", "correction")],
                           gap_score=0.6)]
        ideas = build_structural_ideas("science", profile, gaps, [], 12)
        assert ideas
        for idea in ideas:
            for text in (idea.hook_concept, idea.working_title, idea.why_now):
                assert "{" not in text and "}" not in text, text


class TestThumbnailHeadline:
    @pytest.mark.parametrize("title,expected", [
        ("What Space Actually Does", "WHAT SPACE"),
        ("The Part Of Black Holes Nobody Explains", "BLACK HOLES"),
        ("The Truth About Animals", "ANIMALS"),
        ("Why Do Black Holes Spin So Fast?", "BLACK HOLES SPIN"),
        ("10 Facts About Neutron Stars", "10 FACTS"),
    ])
    def test_headline_reads_as_a_phrase(self, title, expected):
        assert _headline(title, 3) == expected

    def test_never_ends_on_a_dangling_adverb(self):
        for title in ("This Changes Everything Completely",
                      "Space Explained Simply",
                      "It Happened Suddenly"):
            headline = _headline(title, 3)
            last = headline.split()[-1].lower() if headline else ""
            assert not (last.endswith("ly") and len(last) > 6), headline

    def test_never_ends_on_a_determiner(self):
        for title in ("The Thing Nobody Explains", "What Most People Miss"):
            headline = _headline(title, 3)
            last = headline.split()[-1].lower() if headline else ""
            assert last not in {"nobody", "most", "every", "each"}, headline

    def test_word_count_is_bounded(self):
        for limit in (1, 2, 3, 4):
            assert len(_headline(
                "A Very Long Title With Many Content Words Inside It",
                limit).split()) <= limit

    def test_empty_and_filler_only_titles_are_safe(self):
        assert _headline("", 3) == ""
        assert _headline("the a an of to", 3)  # falls back rather than crashing

    def test_output_is_uppercase(self):
        assert _headline("black holes explained", 3).isupper()
