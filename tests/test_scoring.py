"""Scoring, viral opportunity and content-gap tests (spec sections 4, 5, 6, 36)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from engine.core.models import ResearchVideo
from engine.core.util import rfc3339, utc_now
from engine.research.gaps import (cluster_videos, detect_angles, find_gaps,
                                  research_context_block, topic_label)
from engine.research.scoring import (engagement_score,
                                     recency_score, score_all,
                                     title_pattern_score)


def make_video(video_id: str, title: str, *, views: int = 100_000,
               likes: int = 5_000, comments: int = 200, age_days: float = 7,
               subs: int = 100_000, video_count: int = 200,
               total_views: int = 20_000_000, duration: int = 45) -> ResearchVideo:
    return ResearchVideo(
        video_id=video_id, title=title, channel_id=f"ch_{video_id}",
        channel_title=f"Channel {video_id}",
        published_at=rfc3339(utc_now() - timedelta(days=age_days)),
        duration_seconds=duration, views=views, likes=likes, comments=comments,
        channel_subscribers=subs, channel_video_count=video_count,
        channel_total_views=total_views, is_short=duration <= 180,
        description=f"A video about {title}.")


class TestRecency:
    def test_old_video_scores_near_zero(self):
        """A 4-year-old 50M-view video must not dominate (spec section 5)."""
        old = make_video("old", "The Biggest Black Hole Ever Found",
                         views=50_000_000, age_days=1400)
        fresh = make_video("new", "Why Do Black Holes Spin So Fast?",
                           views=900_000, age_days=3)
        score_all([old, fresh])
        assert recency_score(old) < 0.01
        assert recency_score(fresh) > 0.85

    def test_ranking_prefers_recent_fast_growth(self):
        old = make_video("old", "Huge Black Hole Discovery", views=50_000_000,
                         likes=900_000, comments=40_000, age_days=1400,
                         subs=5_000_000, video_count=800,
                         total_views=2_000_000_000)
        fresh = make_video("new", "Why Do Black Holes Spin So Fast?",
                           views=1_400_000, likes=110_000, comments=3_800,
                           age_days=3, subs=120_000, video_count=150,
                           total_views=18_000_000)
        ranked = score_all([old, fresh])
        assert ranked[0].video_id == "new"


class TestBreakout:
    def test_breakout_detected_above_threshold(self):
        """PerformanceRatio = views / expected-for-channel (spec section 5)."""
        video = make_video("b", "Small Channel Big Hit", views=1_000_000,
                           age_days=5, subs=20_000, video_count=100,
                           total_views=10_000_000)  # expected = 100k -> 10x
        score_all([video])
        assert video.performance_ratio == pytest.approx(10.0, rel=0.01)
        assert video.is_breakout is True

    def test_normal_video_is_not_breakout(self):
        video = make_video("n", "Regular Upload", views=100_000, age_days=5,
                           subs=100_000, video_count=100,
                           total_views=10_000_000)  # expected = 100k -> 1x
        score_all([video])
        assert video.performance_ratio == pytest.approx(1.0, rel=0.01)
        assert video.is_breakout is False

    def test_old_video_never_counts_as_breakout(self):
        video = make_video("o", "Ancient Hit", views=10_000_000, age_days=900,
                           subs=10_000, video_count=50, total_views=1_000_000)
        score_all([video])
        assert video.performance_ratio > 2.5
        assert video.is_breakout is False, "age must disqualify a breakout"

    def test_expected_views_falls_back_to_subscribers(self):
        video = make_video("h", "Hidden Stats", views=40_000, age_days=4,
                           subs=100_000, video_count=0, total_views=0)
        score_all([video])
        # 20% of 100k subscribers = 20k expected -> 2x
        assert video.performance_ratio == pytest.approx(2.0, rel=0.01)


class TestTitlePatterns:
    def test_question_scores_well(self):
        score, patterns = title_pattern_score("Why Do Black Holes Spin So Fast?")
        assert score > 0.7
        assert "direct_question" in patterns or "question_word" in patterns

    def test_clickbait_is_penalised(self):
        clean, _ = title_pattern_score(
            "Scientists Found a Strange Signal From Deep Space")
        junk, patterns = title_pattern_score("Scientists FOUND ALIENS!!! SHOCKING")
        assert junk < clean, "deceptive clickbait must score lower"
        assert "clickbait_risk" in patterns

    def test_ctr_potential_is_bounded_and_named_as_potential(self):
        video = make_video("c", "How Neutron Stars Actually Work")
        score_all([video])
        assert 0.0 <= video.ctr_potential_score <= 100.0
        # The field must never be called plain "ctr" - it is not real CTR.
        assert not hasattr(video, "ctr")


class TestEngagement:
    def test_zero_views_is_safe(self):
        video = make_video("z", "No views yet", views=0, likes=0, comments=0)
        assert engagement_score(video) == 0.0

    def test_high_engagement_saturates_at_one(self):
        video = make_video("e", "Very engaged", views=1000, likes=900,
                           comments=500)
        assert engagement_score(video) == 1.0


class TestClusteringAndGaps:
    @pytest.fixture
    def corpus(self):
        return score_all([
            make_video("a", "10 Facts About Black Holes", views=900_000,
                       age_days=6),
            make_video("b", "7 Crazy Things About Black Holes", views=420_000,
                       age_days=14),
            make_video("c", "Black Hole Facts You Won't Believe", views=180_000,
                       age_days=40),
            make_video("d", "The Biggest Black Holes Ever Found",
                       views=2_000_000, age_days=20),
            make_video("e", "How Neutron Stars Actually Work", views=600_000,
                       age_days=11),
            make_video("f", "Neutron Star Collision Explained", views=250_000,
                       age_days=22),
        ])

    def test_clusters_use_readable_multiword_topics(self, corpus):
        """A bare keyword makes downstream copy nonsense ("Behind Black")."""
        clusters = cluster_videos(corpus)
        topics = [c.topic for c in clusters]
        assert "black holes" in topics
        assert "black" not in topics

    def test_topic_label_merges_plurals(self):
        members = [make_video("x", "How Neutron Stars Work"),
                   make_video("y", "Neutron Star Collision Explained")]
        assert topic_label("neutron", members) == "neutron stars"

    def test_gap_finds_missing_angle_from_spec_example(self, corpus):
        """The spec's own example: listicles exist, so correction is the gap."""
        clusters = cluster_videos(corpus)
        gaps = find_gaps(clusters, corpus)
        black_hole_gap = next(g for g in gaps if "black" in g.topic)
        assert "listicle" in black_hole_gap.common_angles
        missing_keys = [m.split(":")[0] for m in black_hole_gap.missing_angles]
        assert "correction" in missing_keys

    def test_singleton_cluster_does_not_outrank_real_cluster(self):
        videos = score_all([
            make_video("a", "10 Facts About Black Holes", views=900_000),
            make_video("b", "7 Things About Black Holes", views=420_000),
            make_video("c", "More Black Holes Explained", views=300_000),
            make_video("d", "Random Unrelated Clickbait!!!", views=300_000),
        ])
        clusters = cluster_videos(videos)
        gaps = find_gaps(clusters, videos)
        assert gaps, "expected at least one gap"
        assert "black" in gaps[0].topic, (
            "a one-video topic looks maximally unsaturated and must not win")

    def test_detect_angles_never_returns_empty(self):
        assert detect_angles("") == ["statement"]
        assert detect_angles("Something plain") == ["statement"]

    def test_context_block_forbids_copying(self, corpus):
        clusters = cluster_videos(corpus)
        gaps = find_gaps(clusters, corpus)
        block = research_context_block(corpus, clusters, gaps)
        assert "Do not rewrite" in block
        assert "NOT material to copy" in block


class TestScoreStability:
    def test_scores_are_bounded(self):
        videos = score_all([
            make_video("a", "A" * 300, views=10 ** 9, likes=10 ** 8,
                       comments=10 ** 7, age_days=0.1),
            make_video("b", "", views=0, likes=0, comments=0, age_days=5000),
        ])
        for v in videos:
            assert 0.0 <= v.viral_score <= 100.0
            assert 0.0 <= v.ctr_potential_score <= 100.0

    def test_empty_corpus_does_not_crash(self):
        assert score_all([]) == []
        assert cluster_videos([]) == []
        assert find_gaps([], []) == []
