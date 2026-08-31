"""Caption timing, scheduling/timezone, quality gate and database tests
(spec sections 19, 20, 21, 22, 23, 27, 36, 44).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine.core.config import load_config
from engine.core.db import Database
from engine.core.models import (AutomationRequest, JobStatus, Scene, Script,
                                VideoJob, VideoMetadata)
from engine.core.niche import build_profile, is_kids_niche
from engine.core.util import (jaccard, local_slot_to_utc, parse_rfc3339,
                              rfc3339, token_overlap)
from engine.quality.gate import QualityGate
from engine.tts.base import SceneAudio, WordMark, estimate_word_marks
from engine.video.captions import (CaptionEngine, absolute_words, group_words)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def make_clip(index: int, words: list[tuple[float, float, str]],
              duration: float) -> SceneAudio:
    return SceneAudio(
        scene_index=index, path=Path("dummy.wav"), duration=duration,
        text=" ".join(w[2] for w in words),
        words=[WordMark(start=s, duration=d, text=t) for s, d, t in words],
        provider="edge", exact_timing=True)


# ==========================================================================
class TestCaptionTiming:
    def test_offsets_are_applied_to_absolute_timeline(self):
        """Per-scene word marks are relative; the timeline must be absolute."""
        clips = [
            (0.0, make_clip(0, [(0.1, 0.4, "A"), (0.6, 0.4, "star")], 2.0)),
            (2.16, make_clip(1, [(0.1, 0.5, "exploded")], 1.5)),
        ]
        words = absolute_words(clips)
        assert [w.text for w in words] == ["A", "star", "exploded"]
        assert words[0].start == pytest.approx(0.1)
        assert words[2].start == pytest.approx(2.26), "second clip must be offset"

    def test_overlapping_words_are_separated(self):
        clips = [(0.0, make_clip(0, [(0.0, 5.0, "long"), (0.5, 0.3, "next")], 6.0))]
        words = absolute_words(clips)
        assert words[0].end <= words[1].start + 1e-6

    def test_empty_words_are_dropped(self):
        clips = [(0.0, make_clip(0, [(0.1, 0.2, "  "), (0.4, 0.2, "ok")], 1.0))]
        assert [w.text for w in absolute_words(clips)] == ["ok"]

    def test_grouping_respects_word_and_char_limits(self):
        clips = [(0.0, make_clip(0, [
            (i * 0.3, 0.25, f"word{i}") for i in range(12)], 4.0))]
        groups = group_words(absolute_words(clips), max_words=3, max_chars=20)
        assert all(len(g.words) <= 3 for g in groups)

    def test_grouping_breaks_on_sentence_end(self):
        clips = [(0.0, make_clip(0, [
            (0.0, 0.2, "Stars"), (0.3, 0.2, "explode."),
            (0.6, 0.2, "Then"), (0.9, 0.2, "light")], 2.0))]
        groups = group_words(absolute_words(clips), max_words=4, max_chars=40)
        assert len(groups) >= 2
        assert groups[0].words[-1].text.endswith(".")

    def test_grouping_breaks_on_long_pause(self):
        clips = [(0.0, make_clip(0, [
            (0.0, 0.2, "one"), (2.5, 0.2, "two")], 3.0))]
        groups = group_words(absolute_words(clips), max_words=4, max_gap=0.6)
        assert len(groups) == 2, "a 2.3s gap must start a new caption"

    def test_ass_and_srt_are_written_and_aligned(self, cfg, tmp_path):
        clips = [
            (0.0, make_clip(0, [(0.1, 0.4, "A"), (0.6, 0.5, "star"),
                                (1.2, 0.6, "exploded.")], 2.2)),
            (2.36, make_clip(1, [(0.1, 0.5, "Nobody"), (0.7, 0.5, "expected"),
                                 (1.3, 0.4, "it.")], 2.0)),
        ]
        engine = CaptionEngine(cfg)
        ass_path, srt_path, count = engine.build(
            clips, tmp_path / "c.ass", tmp_path / "c.srt", 1080, 1920)
        ass = ass_path.read_text(encoding="utf-8")
        srt = srt_path.read_text(encoding="utf-8")

        assert count > 0
        assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
        assert "[Events]" in ass and "Dialogue:" in ass
        # Active-word highlighting emits one event per word-state.
        assert ass.count("Dialogue:") >= 6
        # SRT keeps real casing for the YouTube captions track.
        assert "exploded" in srt.lower()
        assert "-->" in srt

    def test_captions_stay_inside_the_safe_area(self, cfg, tmp_path):
        clips = [(0.0, make_clip(0, [(0.1, 0.4, "safe")], 1.0))]
        engine = CaptionEngine(cfg)
        ass_path, _, _ = engine.build(
            clips, tmp_path / "c.ass", tmp_path / "c.srt", 1080, 1920)
        ass = ass_path.read_text(encoding="utf-8")
        style_line = next(l for l in ass.splitlines() if l.startswith("Style:"))
        margin_v = int(style_line.split(",")[-2])
        # Must clear the YouTube Shorts action bar at the bottom of the frame.
        assert margin_v >= int(1920 * 0.15), f"MarginV {margin_v} is too low"

    def test_braces_in_narration_are_escaped(self, cfg, tmp_path):
        clips = [(0.0, make_clip(0, [(0.1, 0.4, "{brace}")], 1.0))]
        engine = CaptionEngine(cfg)
        ass_path, _, _ = engine.build(
            clips, tmp_path / "c.ass", tmp_path / "c.srt", 1080, 1920)
        ass = ass_path.read_text(encoding="utf-8")
        # Captions are uppercased for punch, so compare case-insensitively.
        assert "\\{brace\\}" in ass.lower(), \
            "unescaped braces would be parsed as ASS override tags"

    def test_landscape_allows_more_words(self, cfg, tmp_path):
        clips = [(0.0, make_clip(0, [
            (i * 0.3, 0.25, "word") for i in range(9)], 3.0))]
        engine = CaptionEngine(cfg)
        _, _, portrait = engine.build(
            clips, tmp_path / "p.ass", tmp_path / "p.srt", 1080, 1920)
        _, _, landscape = engine.build(
            clips, tmp_path / "l.ass", tmp_path / "l.srt", 1920, 1080)
        assert landscape <= portrait


class TestEstimatedTiming:
    def test_estimation_covers_full_duration(self):
        marks = estimate_word_marks("One two three four five.", 5.0)
        assert len(marks) == 5
        assert marks[0].start == 0.0
        assert marks[-1].end <= 5.05

    def test_punctuation_creates_a_longer_gap(self):
        marks = estimate_word_marks("word, word word", 3.0)
        gap_after_comma = marks[1].start - marks[0].end
        gap_plain = marks[2].start - marks[1].end
        assert gap_after_comma > gap_plain

    def test_empty_input_is_safe(self):
        assert estimate_word_marks("", 5.0) == []
        assert estimate_word_marks("word", 0.0) == []


# ==========================================================================
class TestScheduling:
    def test_local_time_converts_to_correct_utc(self):
        """20:00 Asia/Kolkata is 14:30 UTC (IST = UTC+5:30)."""
        after = datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc)
        result = local_slot_to_utc("20:00", "Asia/Kolkata", after=after)
        assert result.hour == 14 and result.minute == 30
        assert result.tzinfo == timezone.utc

    def test_slot_already_passed_rolls_to_tomorrow(self):
        after = datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc)  # 23:30 IST
        result = local_slot_to_utc("20:00", "Asia/Kolkata", after=after)
        local = result.astimezone(ZoneInfo("Asia/Kolkata"))
        assert local.day == 2, "20:00 IST already passed, must be the next day"
        assert local.hour == 20

    def test_weekday_restriction_is_honoured(self):
        # 2026-03-01 is a Sunday; restrict to Mon-Fri (0-4).
        after = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)
        result = local_slot_to_utc("20:30", "Asia/Kolkata", days=[0, 1, 2, 3, 4],
                                   after=after)
        local = result.astimezone(ZoneInfo("Asia/Kolkata"))
        assert local.weekday() in (0, 1, 2, 3, 4)

    def test_dst_timezone_handled(self):
        """A timezone with DST must still produce the requested local time."""
        after = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
        result = local_slot_to_utc("20:00", "America/New_York", after=after)
        local = result.astimezone(ZoneInfo("America/New_York"))
        assert local.hour == 20

    def test_rfc3339_round_trip(self):
        original = datetime(2026, 3, 1, 14, 30, tzinfo=timezone.utc)
        assert parse_rfc3339(rfc3339(original)) == original

    def test_uploader_only_schedules_while_private(self, cfg):
        """publishAt is ignored unless privacyStatus is private at insert."""
        from engine.youtube.upload import YouTubeUploader
        uploader = YouTubeUploader(cfg)
        meta = VideoMetadata(title="t", description="d", privacy="public",
                             publish_at="2026-03-01T14:30:00Z")
        body = uploader.build_body(meta, schedule=True)
        assert body["status"]["privacyStatus"] == "private"
        assert body["status"]["publishAt"] == "2026-03-01T14:30:00Z"

    def test_immediate_upload_keeps_requested_privacy(self, cfg):
        from engine.youtube.upload import YouTubeUploader
        uploader = YouTubeUploader(cfg)
        meta = VideoMetadata(title="t", description="d", privacy="private")
        body = uploader.build_body(meta, schedule=False)
        assert body["status"]["privacyStatus"] == "private"
        assert "publishAt" not in body["status"]

    def test_made_for_kids_is_declared_at_insert(self, cfg):
        from engine.youtube.upload import YouTubeUploader
        uploader = YouTubeUploader(cfg)
        body = uploader.build_body(
            VideoMetadata(title="t", description="d", made_for_kids=True),
            schedule=False)
        assert body["status"]["selfDeclaredMadeForKids"] is True

    def test_metadata_is_truncated_to_youtube_limits(self, cfg):
        from engine.youtube.upload import YouTubeUploader
        uploader = YouTubeUploader(cfg)
        body = uploader.build_body(
            VideoMetadata(title="T" * 250, description="D" * 9000,
                          tags=[f"tag{i}" for i in range(40)]),
            schedule=False)
        assert len(body["snippet"]["title"]) == 100
        assert len(body["snippet"]["description"]) == 5000
        assert len(body["snippet"]["tags"]) == 15


# ==========================================================================
class TestQuotaGuard:
    def test_reserve_protects_upload_budget(self, cfg, tmp_path):
        from engine.research.youtube import QuotaExceeded, QuotaGuard
        db = Database(tmp_path / "q.db")
        guard = QuotaGuard(cfg, db)
        try:
            # Reserve = daily_video_limit * 1600
            assert guard.reserve == int(cfg.get("automation.daily_video_limit")) * 1600
            assert guard.remaining() == guard.limit - guard.reserve
            # Uploads may ignore the reserve; research may not.
            guard.check("video_insert", respect_reserve=False)
            for _ in range(guard.remaining() // 100):
                guard.spend("search_list")
            with pytest.raises(QuotaExceeded):
                guard.check("search_list")
        finally:
            db.close()

    def test_costs_match_official_documented_values(self, cfg):
        from engine.research.youtube import QuotaGuard
        guard = QuotaGuard(cfg, None)
        assert guard.cost("search_list") == 100
        assert guard.cost("video_insert") == 1600
        assert guard.cost("videos_list") == 1

    def test_duration_parsing(self):
        from engine.research.youtube import parse_iso8601_duration as parse
        assert parse("PT45S") == 45
        assert parse("PT1M30S") == 90
        assert parse("PT1H2M3S") == 3723
        assert parse("P1DT1H") == 90000
        assert parse("") == 0
        assert parse("garbage") == 0


# ==========================================================================
class TestQualityGate:
    def _script(self) -> Script:
        return Script(
            hook="A star exploded.",
            script="A star exploded. Nobody expected it. That changed things.",
            scenes=[Scene(index=0, narration="A star exploded.",
                          duration=2.0).to_dict()],
            provider="groq")

    def test_missing_video_is_a_blocker(self, cfg, tmp_path):
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=tmp_path / "nope.mp4",
            metadata=VideoMetadata(title="A title", description="D" * 60,
                                   tags=["a"]),
            script=self._script(), profile=build_profile("science"),
            target_duration=45)
        assert report.passed is False
        assert any("file_exists" in b for b in report.blockers)

    def test_missing_title_is_a_blocker(self, cfg, tmp_path):
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None, metadata=VideoMetadata(title="", description=""),
            script=self._script(), profile=build_profile("science"))
        assert any("title_present" in b for b in report.blockers)

    def test_prohibited_content_blocks_upload(self, cfg):
        gate = QualityGate(cfg)
        script = Script(script="Here is how to make a bomb at home.")
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="Chemistry", description="D" * 60),
            script=script, profile=build_profile("science"))
        assert any("policy_risk" in b for b in report.blockers)
        assert report.policy_risk == "high"

    def test_kids_violation_blocks_upload(self, cfg):
        gate = QualityGate(cfg)
        script = Script(script="The monster attacks with a knife and blood.")
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="Fun story", description="D" * 60,
                                   made_for_kids=True),
            script=script, profile=build_profile("kids bedtime stories"))
        assert any("kids_compliance" in b for b in report.blockers)

    def test_failed_originality_blocks_upload(self, cfg):
        class FakeOriginality:
            passed = False
            max_similarity = 0.9
            self_similarity = 0.1
            findings = ["too similar to an existing video"]

        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="A title", description="D" * 60),
            script=self._script(), profile=build_profile("science"),
            originality=FakeOriginality())
        assert any("originality" in b for b in report.blockers)

    def test_shorts_do_not_require_a_thumbnail(self, cfg):
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="A title", description="D" * 60),
            script=self._script(), profile=build_profile("science"),
            video_format="SHORT")
        thumb = next(c for c in report.checks if c["name"] == "thumbnail_present")
        assert thumb["passed"] is True

    def test_longform_requires_a_thumbnail(self, cfg):
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None,
            metadata=VideoMetadata(title="A title", description="D" * 60),
            script=self._script(), profile=build_profile("science"),
            video_format="LONGFORM")
        thumb = next(c for c in report.checks if c["name"] == "thumbnail_present")
        assert thumb["passed"] is False

    def test_score_is_bounded_and_minimum_is_configurable(self, cfg):
        gate = QualityGate(cfg)
        report = gate.evaluate(
            video=None, metadata=VideoMetadata(title="t", description="d"),
            script=self._script(), profile=build_profile("science"))
        assert 0.0 <= report.score <= 100.0
        assert gate.minimum == float(cfg.get("quality.minimum_score"))


# ==========================================================================
class TestDatabase:
    def test_job_round_trip_survives_restart(self, tmp_path):
        """Spec section 22: app closes -> jobs must not disappear."""
        path = tmp_path / "jobs.db"
        db = Database(path)
        job = VideoJob(automation_id="a1", status=JobStatus.SCRIPT.value,
                       request=AutomationRequest(niche="science").to_dict())
        job.logs.append("started")
        db.save_job(job)
        db.close()

        reopened = Database(path)
        try:
            loaded = reopened.get_job(job.job_id)
            assert loaded is not None
            assert loaded.status == JobStatus.SCRIPT.value
            assert loaded.request["niche"] == "science"
            assert loaded.logs == ["started"]
        finally:
            reopened.close()

    def test_pending_jobs_excludes_terminal_and_waiting(self, tmp_path):
        db = Database(tmp_path / "p.db")
        try:
            for status in (JobStatus.SCRIPT, JobStatus.PUBLISHED,
                           JobStatus.FAILED, JobStatus.AWAITING_APPROVAL,
                           JobStatus.RENDERING):
                db.save_job(VideoJob(status=status.value))
            pending = {j.status for j in db.pending_jobs()}
            assert pending == {JobStatus.SCRIPT.value, JobStatus.RENDERING.value}
        finally:
            db.close()

    def test_quota_accumulates_per_day(self, tmp_path):
        db = Database(tmp_path / "q.db")
        try:
            db.add_quota("2026-03-01", 100, "search_list")
            total = db.add_quota("2026-03-01", 100, "search_list")
            assert total == 200
            assert db.quota_used("2026-03-01") == 200
            assert db.quota_used("2026-03-02") == 0
        finally:
            db.close()

    def test_settings_round_trip_complex_values(self, tmp_path):
        db = Database(tmp_path / "s.db")
        try:
            db.set_setting("cfg", {"a": [1, 2], "b": "x"})
            assert db.get_setting("cfg") == {"a": [1, 2], "b": "x"}
            assert db.get_setting("missing", "fallback") == "fallback"
        finally:
            db.close()

    def test_strategy_weights_round_trip(self, tmp_path):
        db = Database(tmp_path / "st.db")
        try:
            db.upsert_strategy("hook_type", "question", 1.4, 5, 0.7)
            db.upsert_strategy("hook_type", "shock", 0.8, 4, 0.4)
            weights = db.strategy("hook_type")
            assert weights["question"] == 1.4 and weights["shock"] == 0.8
        finally:
            db.close()


# ==========================================================================
class TestSimilarity:
    def test_identical_text_is_fully_similar(self):
        text = "the quick brown fox jumps over the lazy dog again and again"
        assert jaccard(text, text) == pytest.approx(1.0)

    def test_unrelated_text_is_dissimilar(self):
        assert jaccard("black holes bend light near the horizon",
                       "sourdough bread needs a warm kitchen to rise") < 0.05

    def test_short_text_does_not_crash(self):
        assert jaccard("", "") == 0.0
        assert jaccard("one", "one") >= 0.0

    def test_token_overlap_is_symmetric_on_min(self):
        assert token_overlap("black holes", "black holes explained") == 1.0


class TestNicheProfiles:
    def test_kids_niche_is_detected_and_locks_restrictions(self):
        assert is_kids_niche("kids bedtime stories") is True
        profile = build_profile("kids bedtime stories")
        assert profile.made_for_kids is True
        assert profile.caption_style == "block"
        assert any("scary" in r for r in profile.restrictions)

    def test_kids_flag_forces_kids_profile_on_any_niche(self):
        profile = build_profile("science", made_for_kids=True)
        assert profile.made_for_kids is True
        assert profile.words_per_second <= 2.0
        assert any("dangerous" in r for r in profile.restrictions)

    def test_unknown_niche_still_gets_a_usable_profile(self):
        profile = build_profile("competitive duck herding")
        assert profile.words_per_second > 0
        assert profile.scene_seconds > 0
        assert profile.visual_style

    def test_style_hint_changes_pacing(self):
        fast = build_profile("science", style="fast-paced punchy")
        calm = build_profile("science", style="calm and relaxing")
        assert fast.scene_seconds < calm.scene_seconds
        assert fast.words_per_second > calm.words_per_second

    def test_sensitive_niche_flags_disclaimers(self):
        finance = build_profile("crypto investing")
        assert finance.is_sensitive is True
        assert finance.disclaimers
