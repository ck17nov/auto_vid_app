"""Topic clustering + content gap detection (spec section 6).

Deliberately structural first, LLM second:

  1. Cluster the researched videos by shared title keywords.
  2. For each cluster, identify which ANGLE TEMPLATES are already saturated
     (listicle, ranking, reaction...) and which are absent (mechanism, origin,
     counter-intuitive correction, consequence...).
  3. The absent angles plus the questions nobody answered become the gap.

The structural pass runs with no API and no key, so gap detection never blocks
on an LLM being available; `ideas.py` then asks the LLM to turn a gap into an
original concept.
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..core.models import ContentGap, ResearchVideo, TopicCluster
from ..core.util import STOPWORDS, clamp, keywords, words

# Angle templates, with the regexes that detect them in a title.
ANGLE_TEMPLATES: dict[str, tuple[str, str]] = {
    "listicle": (r"^\d+\s|\btop\s*\d+\b|\b\d+\s+(things|facts|ways|reasons)\b",
                 "a numbered list of items"),
    "explainer": (r"\b(explained|how (it|they) works?|what is|meaning of)\b",
                  "a plain explanation of what something is"),
    "mechanism": (r"\b(how|why) (does|do|did|is|are|can)\b|\bmechanism\b",
                  "the underlying cause or mechanism"),
    "origin": (r"\b(history|origin|first|invented|discovered|began|ancient)\b",
               "where it came from"),
    "correction": (r"\b(myth|misconception|actually|wrong|truth about|lie)\b",
                   "correcting a widely believed error"),
    "consequence": (r"\b(what (if|happens)|impact|consequence|result|effect)\b",
                    "what it leads to"),
    "comparison": (r"\b(vs|versus|compared|better than|difference)\b",
                   "a direct comparison"),
    "extreme": (r"\b(biggest|smallest|fastest|oldest|deadliest|most|strangest)\b",
                "the extreme case"),
    "human_story": (r"\b(man|woman|scientist|team|he|she|story of|who)\b",
                    "the story of the people involved"),
    "future": (r"\b(future|will|next|2026|2027|coming|soon|prediction)\b",
               "what comes next"),
    "hidden": (r"\b(hidden|secret|nobody|no one|unknown|forgotten|lost)\b",
               "something overlooked"),
    "practical": (r"\b(how to|guide|tips|steps|do this|use)\b",
                  "how the viewer can apply it"),
}

QUESTION_STARTERS = [
    "why does", "how does", "what happens when", "who first", "what if",
    "where does", "when did", "what makes", "how much", "how long",
]


def _title_tokens(video: ResearchVideo) -> set[str]:
    return {w for w in words(video.title)
            if w not in STOPWORDS and len(w) > 3 and not w.isdigit()}


def cluster_videos(videos: list[ResearchVideo], *, min_size: int = 2,
                   max_clusters: int = 12) -> list[TopicCluster]:
    """Greedy keyword clustering.

    Full clustering (k-means over embeddings) is overkill here: titles are short
    and a shared salient keyword is a strong topic signal. Greedy grouping by
    the highest-traction keyword is transparent and needs no model.
    """
    if not videos:
        return []

    # Score each keyword by the traction of the videos containing it.
    kw_videos: dict[str, list[ResearchVideo]] = defaultdict(list)
    for v in videos:
        for token in _title_tokens(v):
            kw_videos[token].append(v)

    kw_weight: dict[str, float] = {}
    for token, vids in kw_videos.items():
        if len(vids) < min_size:
            continue
        kw_weight[token] = sum(v.viral_score for v in vids) / 100.0

    clusters: list[TopicCluster] = []
    claimed: set[str] = set()
    for token, _ in sorted(kw_weight.items(), key=lambda kv: -kv[1]):
        members = [v for v in kw_videos[token] if v.video_id not in claimed]
        if len(members) < min_size:
            continue
        for m in members:
            claimed.add(m.video_id)
        combined = " ".join(m.title for m in members)
        cluster = TopicCluster(
            topic=topic_label(token, members),
            keywords=keywords(combined, limit=10),
            video_ids=[m.video_id for m in members],
            total_views=sum(m.views for m in members),
            avg_velocity=round(sum(m.view_velocity for m in members) / len(members), 2),
            breakout_count=sum(1 for m in members if m.is_breakout),
            title_patterns=sorted({p for m in members
                                   for p in detect_angles(m.title)}),
            example_titles=[m.title for m in members[:6]],
        )
        # Momentum: recent + fast + several videos + breakouts present.
        cluster.momentum = round(clamp(
            0.34 * clamp(len(members) / 6.0)
            + 0.34 * clamp(cluster.avg_velocity / max(
                max((v.view_velocity for v in videos), default=1.0), 1.0))
            + 0.32 * clamp(cluster.breakout_count / 3.0)), 4)
        clusters.append(cluster)
        if len(clusters) >= max_clusters:
            break

    # Leftover high scorers become singleton clusters so nothing is lost.
    leftovers = [v for v in videos if v.video_id not in claimed][:6]
    for v in leftovers:
        if len(clusters) >= max_clusters:
            break
        toks = sorted(_title_tokens(v))
        clusters.append(TopicCluster(
            topic=toks[0] if toks else v.title[:28],
            keywords=keywords(v.title, limit=8),
            video_ids=[v.video_id],
            total_views=v.views,
            avg_velocity=round(v.view_velocity, 2),
            breakout_count=1 if v.is_breakout else 0,
            title_patterns=detect_angles(v.title),
            example_titles=[v.title],
            momentum=round(clamp(v.viral_score / 130.0), 4),
        ))

    clusters.sort(key=lambda c: c.momentum, reverse=True)
    return clusters


def topic_label(token: str, members: list[ResearchVideo]) -> str:
    """Turn a clustering keyword into a readable topic name.

    Clustering keys on a single salient word, but a bare token makes downstream
    text nonsense ("The Correction Behind Black").  This finds the most common
    multi-word phrase containing that token across the member titles, so the
    cluster is labelled "black holes" instead of "black".
    """
    counts: dict[str, int] = {}
    display: dict[str, str] = {}
    for video in members:
        toks = words(video.title)
        for i, w in enumerate(toks):
            if w != token:
                continue
            # Try the token plus its neighbour on either side.
            for phrase in _neighbour_phrases(toks, i):
                # Count singular and plural as the same phrase, otherwise
                # "neutron star" and "neutron stars" each score 1 and neither
                # clears the recurrence threshold.
                key = _canonical(phrase)
                counts[key] = counts.get(key, 0) + 1
                display.setdefault(key, phrase)
    if not counts:
        return token
    # Prefer frequent phrases; break ties toward the longer, more specific one.
    best_key, best_count = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))
    # Only upgrade if the phrase genuinely recurs across titles.
    threshold = 2 if len(members) > 2 else 2
    if best_count >= min(threshold, len(members)):
        return display.get(best_key, best_key)
    return token


def _canonical(phrase: str) -> str:
    """Collapse trivial plurals so phrase counts merge."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss")
                    else w for w in phrase.split())


def _neighbour_phrases(toks: list[str], i: int) -> list[str]:
    out: list[str] = []
    if i + 1 < len(toks) and toks[i + 1] not in STOPWORDS:
        out.append(f"{toks[i]} {toks[i + 1]}")
    if i > 0 and toks[i - 1] not in STOPWORDS:
        out.append(f"{toks[i - 1]} {toks[i]}")
    return out


def detect_angles(title: str) -> list[str]:
    text = (title or "").lower()
    found = [name for name, (pattern, _) in ANGLE_TEMPLATES.items()
             if re.search(pattern, text)]
    return found or ["statement"]


def find_gaps(clusters: list[TopicCluster], videos: list[ResearchVideo],
              *, max_gaps: int = 8) -> list[ContentGap]:
    """For each cluster, what angle is missing and what question is unanswered."""
    by_id = {v.video_id: v for v in videos}
    gaps: list[ContentGap] = []

    for cluster in clusters:
        members = [by_id[i] for i in cluster.video_ids if i in by_id]
        if not members:
            continue
        present: set[str] = set()
        for m in members:
            present.update(detect_angles(m.title))

        missing = [f"{name}: {desc}" for name, (_, desc) in ANGLE_TEMPLATES.items()
                   if name not in present]
        # Rank missing angles: mechanism/correction/consequence pull hardest.
        priority = ["correction", "mechanism", "consequence", "hidden", "origin",
                    "human_story", "extreme", "future", "comparison",
                    "explainer", "practical", "listicle"]
        missing.sort(key=lambda m: priority.index(m.split(":")[0])
                     if m.split(":")[0] in priority else 99)

        questions = [f"{starter} {cluster.topic}?"
                     for starter in QUESTION_STARTERS[:4]]

        saturation = len(present) / len(ANGLE_TEMPLATES)
        # A good gap = high-momentum topic where few angles are covered.
        raw = clamp(cluster.momentum * 0.62 + (1.0 - saturation) * 0.38)

        # Evidence confidence. A single video "proves" nothing: its topic looks
        # maximally unsaturated simply because there is one title to inspect,
        # which otherwise lets singletons outrank real multi-video clusters.
        confidence = clamp(len(members) / 3.0, 0.30, 1.0)

        # Topics whose demand signal comes from deceptive clickbait are poor
        # foundations - we are not going to copy that framing anyway.
        risky = sum(1 for m in members
                    if "clickbait_risk" in (m.score_breakdown.get("title_patterns") or []))
        integrity = 1.0 - 0.5 * (risky / len(members))

        gap_score = round(raw * confidence * integrity, 4)

        gaps.append(ContentGap(
            topic=cluster.topic,
            common_angles=sorted(present),
            missing_angles=missing[:6],
            unanswered_questions=questions,
            audience_curiosity=_curiosity_summary(cluster, members),
            gap_score=gap_score,
        ))

    gaps.sort(key=lambda g: g.gap_score, reverse=True)
    return gaps[:max_gaps]


def _curiosity_summary(cluster: TopicCluster, members: list[ResearchVideo]) -> str:
    top = max(members, key=lambda m: m.viral_score)
    engaged = max(members, key=lambda m: m.engagement_rate)
    return (
        f"{len(members)} recent videos about '{cluster.topic}' are performing; "
        f"the strongest is '{top.title[:80]}' at {top.views:,} views "
        f"({top.view_velocity:,.0f}/day). Highest engagement is "
        f"{engaged.engagement_rate * 100:.1f}%. Angles already used: "
        f"{', '.join(cluster.title_patterns)}.")


def research_context_block(videos: list[ResearchVideo],
                           clusters: list[TopicCluster],
                           gaps: list[ContentGap], *, limit: int = 8) -> str:
    """Compact, factual research summary for the LLM prompt.

    Titles are provided as EVIDENCE OF DEMAND ONLY, with an explicit instruction
    not to rewrite them - this is the originality guard at the prompt level.
    """
    lines: list[str] = ["RESEARCH (observed public data - for orientation only):"]
    for v in videos[:limit]:
        flag = " [BREAKOUT]" if v.is_breakout else ""
        lines.append(
            f"- \"{v.title[:96]}\" | {v.views:,} views | "
            f"{v.view_velocity:,.0f}/day | {v.age_days:.0f}d old | "
            f"engagement {v.engagement_rate * 100:.1f}% | "
            f"channel-relative {v.performance_ratio or 0:.1f}x{flag}")

    if clusters:
        lines.append("\nTOPIC MOMENTUM:")
        for c in clusters[:5]:
            lines.append(f"- '{c.topic}': {len(c.video_ids)} videos, "
                         f"momentum {c.momentum:.2f}, "
                         f"angles used: {', '.join(c.title_patterns)}")

    if gaps:
        lines.append("\nIDENTIFIED CONTENT GAPS:")
        for g in gaps[:4]:
            lines.append(f"- '{g.topic}' (gap {g.gap_score:.2f}): "
                         f"already covered = {', '.join(g.common_angles[:5])}; "
                         f"NOT covered = {'; '.join(g.missing_angles[:3])}")

    lines.append(
        "\nCRITICAL: the titles above show what the audience wants; they are NOT "
        "material to copy. Do not rewrite, translate, reorder or paraphrase any "
        "of them. Take an angle none of them takes, and write from your own "
        "understanding of the subject.")
    return "\n".join(lines)
