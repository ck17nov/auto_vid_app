"""Viral / opportunity scoring (spec sections 4 & 5).

Honesty rules baked in:
  * YouTube does NOT expose other channels' CTR, impressions or retention.
    Nothing here claims to.  `ctr_potential_score` is explicitly a heuristic
    over OBSERVABLE title/thumbnail-independent signals, and is named so it can
    never be mistaken for measured CTR.
  * `performance_ratio` compares a video against its own channel's typical
    performance, which is the only fair way to spot a breakout with public data.

Every weight is configurable (config.yaml -> scoring.weights).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..core.models import ResearchVideo
from ..core.util import age_days, clamp, normalize, words

DEFAULT_WEIGHTS = {
    "view_velocity": 0.28,
    "engagement": 0.18,
    "recency": 0.14,
    "topic_momentum": 0.12,
    "channel_normalized": 0.16,
    "title_pattern": 0.07,
    "content_gap": 0.05,
}

# Title structures that correlate with click-through in short-form.
TITLE_PATTERNS: list[tuple[str, str, float]] = [
    (r"^\d+\s", "listicle", 0.70),
    (r"\b(why|how|what|who|when|where)\b", "question_word", 0.80),
    (r"\?$", "direct_question", 0.85),
    (r"\b(never|stop|don'?t|avoid|mistake|wrong)\b", "warning", 0.78),
    (r"\b(found|discovered|revealed|hidden|secret|nobody|no one)\b", "discovery", 0.88),
    (r"\b(actually|really|truth|myth|misconception)\b", "correction", 0.75),
    (r"\b(before|until|after)\b", "temporal", 0.60),
    (r"\b(vs|versus)\b", "comparison", 0.62),
    (r"\b(this is|here'?s)\b", "demonstrative", 0.55),
    (r"\b(you|your)\b", "second_person", 0.66),
]

# Signals of deceptive clickbait - penalised, never rewarded (spec section 17).
CLICKBAIT_PENALTIES: list[tuple[str, float]] = [
    (r"[!]{2,}", 0.18),
    (r"\b[A-Z]{5,}\b", 0.10),
    (r"\b(gone wrong|gone sexual|you won'?t believe|shocking|insane|omg)\b", 0.22),
    (r"\b(100% real|not clickbait|must watch)\b", 0.25),
    (r"\b(aliens?|proof of god|cure for cancer)\b", 0.18),
]


@dataclass
class ScoreContext:
    """Corpus-level maxima, so scores are relative to the sampled set."""
    max_velocity: float = 1.0
    max_views: float = 1.0
    topic_momentum: dict[str, float] = None       # keyword -> momentum 0..1
    gap_scores: dict[str, float] = None           # video_id -> gap 0..1

    def __post_init__(self) -> None:
        self.topic_momentum = self.topic_momentum or {}
        self.gap_scores = self.gap_scores or {}


# --------------------------------------------------------------------------
# Component scores (each returns 0..1)
# --------------------------------------------------------------------------
def view_velocity_score(video: ResearchVideo, ctx: ScoreContext) -> float:
    """Views per day, log-normalised against the fastest video in the sample."""
    return normalize(video.view_velocity, max(ctx.max_velocity, 1.0))


def engagement_score(video: ResearchVideo) -> float:
    """(likes + comments) / views, scaled.

    Typical healthy short-form engagement is 2-8%; 10% is exceptional, so the
    ceiling sits at 0.10 rather than 1.0.
    """
    if video.views <= 0:
        return 0.0
    rate = (video.likes + video.comments * 2.5) / video.views
    return clamp(rate / 0.10)


def recency_score(video: ResearchVideo, half_life_days: float = 21.0) -> float:
    """Exponential decay. A 4-year-old 50M-view video should score near zero."""
    age = max(video.age_days, 0.0)
    return clamp(math.exp(-age / half_life_days))


def channel_normalized_score(video: ResearchVideo) -> float:
    """How far this video outperformed its own channel's norm."""
    ratio = video.performance_ratio
    if ratio <= 0:
        return 0.0
    # ratio 1.0 = typical, 2.5 = breakout, 6+ = exceptional
    return clamp(math.log1p(ratio) / math.log1p(6.0))


def title_pattern_score(title: str) -> tuple[float, list[str]]:
    """Reward proven structures, penalise deceptive clickbait."""
    text = (title or "").strip()
    lowered = text.lower()
    matched: list[str] = []
    best = 0.30                                   # neutral baseline
    for pattern, name, weight in TITLE_PATTERNS:
        if re.search(pattern, lowered if name != "caps" else text, re.I):
            matched.append(name)
            best = max(best, weight)

    # Length: 30-70 chars reads fully on mobile.
    length = len(text)
    if 30 <= length <= 70:
        best = min(1.0, best + 0.08)
    elif length > 95:
        best -= 0.12

    penalty = 0.0
    for pattern, amount in CLICKBAIT_PENALTIES:
        if re.search(pattern, text):
            penalty += amount
            matched.append("clickbait_risk")
    return clamp(best - penalty), matched


def topic_momentum_score(video: ResearchVideo, ctx: ScoreContext) -> float:
    """How much traction this video's keywords have across the whole sample."""
    if not ctx.topic_momentum:
        return 0.0
    kws = [w for w in words(video.title) if len(w) > 3]
    if not kws:
        return 0.0
    hits = [ctx.topic_momentum.get(k, 0.0) for k in kws]
    hits.sort(reverse=True)
    top = hits[:3]
    return clamp(sum(top) / len(top)) if top else 0.0


def ctr_potential_score(video: ResearchVideo) -> float:
    """HEURISTIC ONLY - not measured CTR (which YouTube never exposes).

    Combines title structure, engagement and how far the video outran its
    channel's baseline.  Documented as a potential score everywhere it appears.
    """
    title, _ = title_pattern_score(video.title)
    eng = engagement_score(video)
    norm = channel_normalized_score(video)
    return round(clamp(title * 0.5 + eng * 0.28 + norm * 0.22) * 100, 1)


# --------------------------------------------------------------------------
# Derived metrics + composite
# --------------------------------------------------------------------------
def enrich(video: ResearchVideo) -> ResearchVideo:
    """Fill in age, velocity, engagement and channel-relative performance."""
    video.age_days = age_days(video.published_at)
    denom = max(video.age_days, 0.75)             # avoid day-0 explosions
    video.view_velocity = video.views / denom
    video.engagement_rate = (
        (video.likes + video.comments) / video.views if video.views else 0.0)

    # Expected views for this channel = lifetime average per video.
    if video.channel_video_count > 0 and video.channel_total_views > 0:
        expected = video.channel_total_views / video.channel_video_count
    elif video.channel_subscribers > 0:
        # Fallback heuristic when the channel hides its totals: a typical video
        # reaches roughly 10-30% of subscriber count. 20% is the midpoint.
        expected = video.channel_subscribers * 0.20
    else:
        expected = 0.0
    video.performance_ratio = round(video.views / expected, 3) if expected > 0 else 0.0
    return video


def build_context(videos: list[ResearchVideo]) -> ScoreContext:
    max_v = max((v.view_velocity for v in videos), default=1.0)
    max_views = max((v.views for v in videos), default=1.0)

    # Topic momentum: keyword appears in several well-performing recent videos.
    weight: dict[str, float] = {}
    for v in videos:
        recency = recency_score(v)
        traction = normalize(v.view_velocity, max(max_v, 1.0))
        for w in set(words(v.title)):
            if len(w) <= 3:
                continue
            weight[w] = weight.get(w, 0.0) + recency * traction
    if weight:
        peak = max(weight.values()) or 1.0
        weight = {k: clamp(val / peak) for k, val in weight.items()}
    return ScoreContext(max_velocity=max_v, max_views=max_views,
                        topic_momentum=weight)


def score_video(video: ResearchVideo, ctx: ScoreContext,
                weights: dict[str, float] | None = None,
                breakout_threshold: float = 2.5) -> ResearchVideo:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    title_s, patterns = title_pattern_score(video.title)

    parts = {
        "view_velocity": view_velocity_score(video, ctx),
        "engagement": engagement_score(video),
        "recency": recency_score(video),
        "topic_momentum": topic_momentum_score(video, ctx),
        "channel_normalized": channel_normalized_score(video),
        "title_pattern": title_s,
        "content_gap": ctx.gap_scores.get(video.video_id, 0.0),
    }
    total = sum(parts[k] * w.get(k, 0.0) for k in parts)
    denom = sum(w.get(k, 0.0) for k in parts) or 1.0

    video.score_breakdown = {k: round(v, 4) for k, v in parts.items()}
    video.score_breakdown["title_patterns"] = patterns  # type: ignore[assignment]
    video.viral_score = round(clamp(total / denom) * 100, 2)
    video.ctr_potential_score = ctr_potential_score(video)
    video.is_breakout = (video.performance_ratio >= breakout_threshold
                        and video.age_days <= 180
                        and video.views >= 1000)
    return video


def score_all(videos: list[ResearchVideo], weights: dict[str, float] | None = None,
              breakout_threshold: float = 2.5) -> list[ResearchVideo]:
    for v in videos:
        enrich(v)
    ctx = build_context(videos)
    for v in videos:
        score_video(v, ctx, weights, breakout_threshold)
    videos.sort(key=lambda v: v.viral_score, reverse=True)
    return videos


def breakouts(videos: list[ResearchVideo]) -> list[ResearchVideo]:
    return [v for v in videos if v.is_breakout]
