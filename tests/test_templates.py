"""Style template + anti-spam halt tests (spec sections 45 & 46)."""
from __future__ import annotations

import pytest

from engine.core.config import load_config
from engine.core.db import Database
from engine.core.models import AutomationRequest, JobStatus, Scene, Script, VideoJob
from engine.core.niche import build_profile
from engine.video.compose import MOTION_CYCLE, assign_motion
from engine.video.music import mood_from_text
from engine.video.templates import (DEFAULT_TEMPLATE, TEMPLATES,
                                     apply_to_profile, caption_overrides,
                                     list_templates, select_template,
                                     video_overrides)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


class TestTemplateCatalogue:
    def test_every_spec_template_exists(self):
        """Spec section 45 names these explicitly."""
        required = {"FAST_FACTS", "TECH_NEWS", "SCIENCE_EXPLAINER",
                    "STORYTELLING", "TOP_5", "MYSTERY", "KIDS_STORY",
                    "EDUCATIONAL", "MOTIVATIONAL"}
        assert required <= set(TEMPLATES)

    def test_each_template_controls_every_documented_dimension(self):
        for name, t in TEMPLATES.items():
            assert t.scene_seconds > 0, name              # scene duration
            assert t.font and t.font_scale > 0, name      # typography
            assert t.transition, name                      # transitions
            assert t.caption_style in {"karaoke", "block", "none"}, name
            assert t.words_per_second > 0, name            # pacing
            assert t.visual_style_suffix, name             # background
            assert t.visual_frequency > 0, name            # visual frequency
            assert t.motion_cycle, name
            assert t.music_mood, name

    def test_templates_are_actually_distinct(self):
        """A template set where everything looks the same is pointless."""
        assert len({t.scene_seconds for t in TEMPLATES.values()}) >= 6
        assert len({t.visual_style_suffix for t in TEMPLATES.values()}) == \
            len(TEMPLATES)
        assert len({t.music_mood for t in TEMPLATES.values()}) >= 4

    def test_list_templates_is_serialisable(self):
        listed = list_templates()
        assert len(listed) == len(TEMPLATES)
        for entry in listed:
            assert {"name", "description", "scene_seconds"} <= set(entry)

    def test_every_music_mood_resolves_to_a_real_preset(self):
        for name, t in TEMPLATES.items():
            assert mood_from_text(t.music_mood), name


class TestTemplateSelection:
    @pytest.mark.parametrize("niche,style,expected", [
        ("science", "", "SCIENCE_EXPLAINER"),
        ("space facts", "", "FAST_FACTS"),
        ("interesting facts", "fast-paced", "FAST_FACTS"),
        ("top 10 mysteries", "", "MYSTERY"),
        ("unsolved mysteries", "", "MYSTERY"),
        ("AI news", "", "TECH_NEWS"),
        ("programming tutorials", "", "TECH_NEWS"),
        ("true horror stories", "", "STORYTELLING"),
        ("reddit stories", "", "STORYTELLING"),
        ("motivation", "", "MOTIVATIONAL"),
        ("kids bedtime stories", "", "KIDS_STORY"),
    ])
    def test_selection_matches_intent(self, niche, style, expected):
        chosen = select_template(niche, style,
                                 made_for_kids=build_profile(niche).made_for_kids)
        assert chosen.name == expected, f"{niche!r} -> {chosen.name}"

    def test_unknown_niche_falls_back_to_the_default(self):
        assert select_template("competitive duck herding", "").name == \
            DEFAULT_TEMPLATE

    def test_plural_forms_are_matched(self):
        """Regression: "stories" must match the "story" hint."""
        assert select_template("scary stories", "").name == "STORYTELLING"
        assert select_template("space mystery", "").name == "MYSTERY"

    def test_kids_always_wins_regardless_of_niche(self):
        """Caption style and pacing are part of the kids SAFETY profile."""
        for niche in ("finance", "gaming", "science", "horror"):
            chosen = select_template(niche, "", made_for_kids=True)
            assert chosen.name == "KIDS_STORY", niche

    def test_forced_template_overrides_detection(self):
        assert select_template("science", "", forced="MYSTERY").name == "MYSTERY"
        assert select_template("science", "", forced="mystery").name == "MYSTERY"

    def test_forced_unknown_name_is_ignored_not_fatal(self):
        chosen = select_template("science", "", forced="NOT_A_TEMPLATE")
        assert chosen.name == "SCIENCE_EXPLAINER"

    def test_forced_template_does_not_override_kids_safety(self):
        """A style choice must never downgrade child-safety settings."""
        chosen = select_template("kids stories", "", made_for_kids=True,
                                 forced="")
        assert chosen.caption_style == "block"

    def test_selection_is_deterministic(self):
        first = select_template("space science facts", "cinematic").name
        for _ in range(5):
            assert select_template("space science facts", "cinematic").name == first


class TestTemplateApplication:
    def test_template_overrides_profile_presentation(self):
        profile = build_profile("science", duration_seconds=45)
        before_scene = profile.scene_seconds
        template = TEMPLATES["FAST_FACTS"]
        applied = apply_to_profile(profile, template)
        assert applied.scene_seconds == template.scene_seconds
        assert applied.scene_seconds != before_scene
        assert applied.words_per_second == template.words_per_second
        assert applied.caption_style == template.caption_style
        assert applied.music_mood == template.music_mood

    def test_template_does_not_relax_kids_restrictions(self):
        """Presentation may change; content restrictions may not."""
        profile = build_profile("kids bedtime stories")
        restrictions_before = set(profile.restrictions)
        applied = apply_to_profile(profile, TEMPLATES["FAST_FACTS"])
        assert set(applied.restrictions) == restrictions_before
        assert applied.made_for_kids is True

    def test_template_does_not_clear_disclaimers(self):
        profile = build_profile("finance basics")
        disclaimers = list(profile.disclaimers)
        applied = apply_to_profile(profile, TEMPLATES["FAST_FACTS"])
        assert applied.disclaimers == disclaimers
        assert applied.is_sensitive is True

    def test_caption_overrides_scale_font_size(self):
        big = caption_overrides(TEMPLATES["MOTIVATIONAL"], 100)
        small = caption_overrides(TEMPLATES["KIDS_STORY"], 100)
        assert big["captions.font_size"] > small["captions.font_size"]
        assert small["captions.style"] == "block"
        assert big["captions.uppercase"] is True
        assert small["captions.uppercase"] is False

    def test_caption_font_size_never_goes_unreadably_small(self):
        assert caption_overrides(TEMPLATES["KIDS_STORY"], 1)[
            "captions.font_size"] >= 24

    def test_video_overrides_carry_transition_and_grade(self):
        overrides = video_overrides(TEMPLATES["MYSTERY"])
        assert overrides["video.transition_duration"] == \
            TEMPLATES["MYSTERY"].transition_duration
        assert overrides["video.contrast"] == TEMPLATES["MYSTERY"].contrast
        assert overrides["video.saturation"] == TEMPLATES["MYSTERY"].saturation

    def test_motion_cycle_is_applied_to_scenes(self):
        scenes = [Scene(index=i, narration="x") for i in range(6)]
        assign_motion(scenes, TEMPLATES["FAST_FACTS"].motion_cycle)
        assert [s.motion for s in scenes] == \
            TEMPLATES["FAST_FACTS"].motion_cycle[:6]

    def test_motion_falls_back_when_cycle_is_missing(self):
        scenes = [Scene(index=i, narration="x") for i in range(4)]
        assign_motion(scenes, None)
        assert [s.motion for s in scenes] == MOTION_CYCLE[:4]
        scenes = [Scene(index=i, narration="x") for i in range(4)]
        assign_motion(scenes, [])
        assert [s.motion for s in scenes] == MOTION_CYCLE[:4]

    def test_no_two_adjacent_scenes_are_static_in_fast_templates(self):
        """FAST_FACTS must keep the frame moving differently every cut."""
        cycle = TEMPLATES["FAST_FACTS"].motion_cycle
        for i in range(len(cycle) - 1):
            assert cycle[i] != cycle[i + 1]


class TestAntiSpamHalt:
    """Spec section 46: stop rather than publish near-identical videos."""

    def _pipeline(self, tmp_path, monkeypatch):
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        db = Database(tmp_path / "spam.db")
        return Pipeline(cfg, db), db, cfg

    def test_identical_scripts_in_a_row_trigger_a_halt(self, tmp_path):
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        db = Database(tmp_path / "spam.db")
        pipe = Pipeline(cfg, db)
        try:
            body = ("Black holes bend light so strongly that nothing escapes "
                    "once it crosses the event horizon, which is why they look "
                    "completely black from outside.")
            for i in range(4):
                script = Script(script=body, hook="Black holes bend light.")
                db.save_script(script, f"hash{i}")
            problems = pipe.preflight(AutomationRequest(niche="science"))
            assert any("near-identical" in p for p in problems), problems
        finally:
            pipe.close()

    def test_varied_scripts_do_not_trigger_a_halt(self, tmp_path):
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        db = Database(tmp_path / "ok.db")
        pipe = Pipeline(cfg, db)
        try:
            for i, body in enumerate([
                "A star exploded and we watched the first light escape it.",
                "Neutron stars spin hundreds of times every second somehow.",
                "The universe has an edge we will never be able to reach.",
                "Gravity is not a force in the way most people imagine it.",
            ]):
                db.save_script(Script(script=body, hook=body[:20]), f"h{i}")
            problems = pipe.preflight(AutomationRequest(niche="science"))
            assert not any("near-identical" in p for p in problems), problems
        finally:
            pipe.close()

    def test_a_single_script_never_triggers_a_halt(self, tmp_path):
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        db = Database(tmp_path / "one.db")
        pipe = Pipeline(cfg, db)
        try:
            db.save_script(Script(script="Only one script here.", hook="One."),
                           "h")
            assert pipe._recent_similarity_run() == 0
        finally:
            pipe.close()

    def test_empty_history_is_safe(self, tmp_path):
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        db = Database(tmp_path / "empty.db")
        pipe = Pipeline(cfg, db)
        try:
            assert pipe._recent_similarity_run() == 0
        finally:
            pipe.close()

    def test_daily_limit_is_enforced(self, tmp_path):
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        cfg.set("automation.daily_video_limit", 2)
        db = Database(tmp_path / "limit.db")
        pipe = Pipeline(cfg, db)
        try:
            for _ in range(2):
                db.save_job(VideoJob(status=JobStatus.PUBLISHED.value))
            problems = pipe.preflight(AutomationRequest(niche="science"))
            assert any("daily video limit" in p for p in problems), problems
        finally:
            pipe.close()


class TestPipelineTemplateIntegration:
    """The template must actually reach the engines, not just be selected."""

    def _pipeline(self, tmp_path, **overrides):
        from engine.pipeline import Pipeline
        cfg = load_config()
        cfg.set("app.workspace", str(tmp_path))
        for key, value in overrides.items():
            cfg.set(key, value)
        return Pipeline(cfg, Database(tmp_path / "tpl.db"))

    def test_template_reaches_caption_and_video_engines(self, tmp_path):
        pipe = self._pipeline(tmp_path)
        try:
            request = AutomationRequest(niche="kids bedtime stories",
                                        made_for_kids=True)
            profile = build_profile(request.niche, made_for_kids=True)
            profile, template = pipe.apply_style_template(request, profile)

            assert template.name == "KIDS_STORY"
            # Caption engine must have been rebuilt with the template's values.
            assert pipe.caption_engine.style == "block"
            assert pipe.caption_engine.uppercase is False
            assert pipe.caption_engine.safe_bottom == template.safe_bottom
            # Composer too.
            assert pipe.composer.transition_dur == template.transition_duration
            assert pipe.composer.contrast == template.contrast
            assert pipe.composer.saturation == template.saturation
            # And the motion cycle used by stage_visuals.
            assert pipe._motion_cycle == template.motion_cycle
        finally:
            pipe.close()

    def test_two_niches_produce_different_engine_settings(self, tmp_path):
        fast = self._pipeline(tmp_path / "a")
        slow = self._pipeline(tmp_path / "b")
        try:
            req_fast = AutomationRequest(niche="interesting facts",
                                         style="fast-paced")
            req_slow = AutomationRequest(niche="unsolved mysteries")
            _, t_fast = fast.apply_style_template(
                req_fast, build_profile(req_fast.niche))
            _, t_slow = slow.apply_style_template(
                req_slow, build_profile(req_slow.niche))

            assert t_fast.name == "FAST_FACTS" and t_slow.name == "MYSTERY"
            assert fast.composer.transition_dur < slow.composer.transition_dur
            assert fast.caption_engine.font_size > slow.caption_engine.font_size
        finally:
            fast.close()
            slow.close()

    def test_forced_template_from_config_is_honoured(self, tmp_path):
        pipe = self._pipeline(tmp_path, **{"video.style_template": "MOTIVATIONAL"})
        try:
            request = AutomationRequest(niche="science")
            _, template = pipe.apply_style_template(
                request, build_profile("science"))
            assert template.name == "MOTIVATIONAL"
        finally:
            pipe.close()

    def test_profile_pacing_follows_the_template(self, tmp_path):
        pipe = self._pipeline(tmp_path)
        try:
            request = AutomationRequest(niche="interesting facts",
                                        style="fast-paced")
            profile, template = pipe.apply_style_template(
                request, build_profile(request.niche))
            assert profile.scene_seconds == template.scene_seconds
            assert profile.words_per_second == template.words_per_second
        finally:
            pipe.close()


class TestProceduralExposure:
    """No frame may render effectively black - it reads as a broken encode."""

    def test_exposure_floor_lifts_a_dark_frame(self):
        from PIL import Image, ImageStat
        from engine.visuals.procedural import _ensure_exposure
        dark = Image.new("RGB", (64, 64), (3, 4, 6))
        lifted = _ensure_exposure(dark)
        assert ImageStat.Stat(lifted.convert("L")).mean[0] > \
            ImageStat.Stat(dark.convert("L")).mean[0]

    def test_pure_black_does_not_divide_by_zero(self):
        from PIL import Image
        from engine.visuals.procedural import _ensure_exposure
        assert _ensure_exposure(Image.new("RGB", (32, 32), (0, 0, 0))) is not None

    def test_well_exposed_frame_is_left_alone(self):
        from PIL import Image, ImageStat
        from engine.visuals.procedural import _ensure_exposure
        mid = Image.new("RGB", (64, 64), (60, 60, 60))
        before = ImageStat.Stat(mid.convert("L")).mean[0]
        after = ImageStat.Stat(_ensure_exposure(mid).convert("L")).mean[0]
        assert abs(after - before) < 0.5

    def test_overbright_frame_is_pulled_down(self):
        from PIL import Image, ImageStat
        from engine.visuals.procedural import _ensure_exposure
        bright = Image.new("RGB", (64, 64), (240, 240, 240))
        after = ImageStat.Stat(_ensure_exposure(bright).convert("L")).mean[0]
        assert after < 240, "a near-white frame would swallow white captions"

    @pytest.mark.parametrize("seed", [0, 7919, 15838, 23757])
    def test_generated_frames_are_never_near_black(self, seed, tmp_path):
        from PIL import Image, ImageStat
        from engine.visuals.base import VisualRequest
        from engine.visuals.procedural import ProceduralProvider
        target = tmp_path / f"p_{seed}.jpg"
        ProceduralProvider().fetch(
            VisualRequest(scene_index=0, prompt="deep space scene",
                          keywords=["space"], width=540, height=960, seed=seed),
            target)
        mean = ImageStat.Stat(Image.open(target).convert("L")).mean[0]
        assert mean >= 22.0, f"seed {seed} produced a luma of {mean:.1f}"
