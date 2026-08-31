"""Analytics collection + the self-improvement loop (spec sections 25 & 26).

Two hard honesty rules:
  * CTR and impressions exist ONLY for the authenticated user's own channel,
    via the YouTube Analytics API.  Never for competitors.
  * The learning layer is plain weighted statistics, clearly labelled as such -
    not "machine learning" (spec section 26 explicitly asks for this).

The loop: published video -> wait -> collect analytics -> group by dimension
(hook_type, title_type, duration bucket, publish hour) -> compute a weight per
value -> feed that back into idea scoring and script hints.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..core.config import Config
from ..core.db import Database
from ..core.logging import log_event
from ..core.util import clamp, utc_now

ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2/reports"

# Metrics available per-video on the owner's own channel.
CORE_METRICS = [
    "views", "estimatedMinutesWatched", "averageViewDuration",
    "averageViewPercentage", "likes", "comments", "subscribersGained",
]
# Impressions/CTR live in a separate dimension set and are not always available
# for very new or very low-volume videos.
IMPRESSION_METRICS = ["impressions", "impressionsCtr"]


@dataclass
class VideoStats:
    video_id: str
    views: int = 0
    watch_time_minutes: float = 0.0
    avg_view_duration: float = 0.0
    avg_view_percentage: float = 0.0
    likes: int = 0
    comments: int = 0
    subscribers_gained: int = 0
    impressions: int = 0
    ctr: float = 0.0
    ctr_available: bool = False
    collected_days: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id, "views": self.views,
            "watch_time_minutes": self.watch_time_minutes,
            "avg_view_duration": self.avg_view_duration,
            "avg_view_percentage": self.avg_view_percentage,
            "likes": self.likes, "comments": self.comments,
            "subscribers_gained": self.subscribers_gained,
            "impressions": self.impressions, "ctr": self.ctr,
            "ctr_available": self.ctr_available,
            "collected_days": self.collected_days,
            "note": "own-channel data only; competitor CTR is never available",
        }


class AnalyticsCollector:
    def __init__(self, cfg: Config, auth=None, db: Database | None = None):
        self.cfg = cfg
        self.auth = auth
        self.db = db

    # ------------------------------------------------------------------
    def collect(self, video_id: str, *, days: int = 28) -> VideoStats:
        """Fetch own-channel analytics for one video."""
        if self.auth is None:
            raise RuntimeError("analytics requires an authorised YouTubeAuth")
        import httpx

        creds = self.auth.credentials()
        end = utc_now().date()
        start = end - timedelta(days=days)
        stats = VideoStats(video_id=video_id, collected_days=days)
        headers = {"Authorization": f"Bearer {creds.token}"}

        params = {
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "metrics": ",".join(CORE_METRICS),
            "filters": f"video=={video_id}",
        }
        with httpx.Client(timeout=45) as client:
            resp = client.get(ANALYTICS_BASE, params=params, headers=headers)
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"YouTube Analytics {resp.status_code}: {resp.text[:240]}")
            data = resp.json()
            rows = data.get("rows") or []
            if rows:
                values = rows[0]
                mapping = dict(zip(CORE_METRICS, values))
                stats.views = int(mapping.get("views", 0) or 0)
                stats.watch_time_minutes = float(
                    mapping.get("estimatedMinutesWatched", 0) or 0)
                stats.avg_view_duration = float(
                    mapping.get("averageViewDuration", 0) or 0)
                stats.avg_view_percentage = float(
                    mapping.get("averageViewPercentage", 0) or 0)
                stats.likes = int(mapping.get("likes", 0) or 0)
                stats.comments = int(mapping.get("comments", 0) or 0)
                stats.subscribers_gained = int(
                    mapping.get("subscribersGained", 0) or 0)

            # Impressions/CTR: separate call, and genuinely optional.
            try:
                imp_params = {**params, "metrics": ",".join(IMPRESSION_METRICS)}
                imp = client.get(ANALYTICS_BASE, params=imp_params, headers=headers)
                if imp.status_code < 400:
                    imp_rows = (imp.json() or {}).get("rows") or []
                    if imp_rows:
                        stats.impressions = int(imp_rows[0][0] or 0)
                        stats.ctr = float(imp_rows[0][1] or 0.0)
                        stats.ctr_available = True
            except Exception as exc:
                log_event("ANALYTICS", "impressions unavailable",
                          video_id=video_id, error=str(exc)[:140])

        if self.db is not None:
            self.db.save_analytics(video_id, stats.to_dict())
        log_event("ANALYTICS", "collected", video_id=video_id,
                  views=stats.views,
                  retention=f"{stats.avg_view_percentage:.1f}%",
                  ctr=(f"{stats.ctr:.2f}%" if stats.ctr_available else "n/a"))
        return stats

    def collect_all(self, *, min_age_hours: float = 24.0,
                    days: int = 28) -> list[VideoStats]:
        """Collect for every published video old enough to have data."""
        if self.db is None:
            return []
        rows = self.db.query(
            "SELECT youtube_video_id, published_time FROM published_videos "
            "WHERE youtube_video_id != ''")
        out: list[VideoStats] = []
        for row in rows:
            vid = row["youtube_video_id"]
            try:
                out.append(self.collect(vid, days=days))
            except Exception as exc:
                log_event("ANALYTICS", "collection failed", video_id=vid,
                          error=str(exc)[:160])
        return out


# --------------------------------------------------------------------------
# Self-improving strategy (simple weighted statistics, NOT machine learning)
# --------------------------------------------------------------------------
DIMENSIONS = ("hook_type", "title_type", "duration_bucket", "publish_hour",
              "visual_style")

# Minimum samples before a dimension value is allowed to influence generation.
MIN_SAMPLES = 3


def duration_bucket(seconds: float) -> str:
    if seconds <= 0:
        return "unknown"
    if seconds < 20:
        return "under_20s"
    if seconds < 35:
        return "20_35s"
    if seconds < 50:
        return "35_50s"
    if seconds < 65:
        return "50_65s"
    if seconds <= 180:
        return "65_180s"
    return "longform"


@dataclass
class StrategyInsight:
    dimension: str
    value: str
    samples: int
    score: float
    weight: float
    avg_views: float = 0.0
    avg_retention: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"dimension": self.dimension, "value": self.value,
                "samples": self.samples, "score": round(self.score, 4),
                "weight": round(self.weight, 4),
                "avg_views": round(self.avg_views, 1),
                "avg_retention": round(self.avg_retention, 2)}


class StrategyLearner:
    """Turns published performance into generation preferences."""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    # ------------------------------------------------------------------
    def _samples(self) -> list[dict[str, Any]]:
        """Join published videos with their newest analytics row."""
        rows = self.db.query("""
            SELECT p.youtube_video_id AS vid, p.hook_type, p.title_type,
                   p.duration, p.visual_style, p.published_time, p.payload,
                   a.views, a.avg_view_percentage, a.ctr, a.subscribers_gained,
                   a.likes, a.comments
            FROM published_videos p
            JOIN analytics a ON a.youtube_video_id = p.youtube_video_id
            JOIN (SELECT youtube_video_id, MAX(collected_at) AS latest
                  FROM analytics GROUP BY youtube_video_id) m
              ON m.youtube_video_id = a.youtube_video_id
             AND m.latest = a.collected_at
        """)
        out: list[dict[str, Any]] = []
        for r in rows:
            hour = ""
            try:
                from ..core.util import parse_rfc3339
                hour = f"{parse_rfc3339(r['published_time']).hour:02d}"
            except Exception:
                hour = "unknown"
            out.append({
                "video_id": r["vid"],
                "hook_type": r["hook_type"] or "unknown",
                "title_type": r["title_type"] or "unknown",
                "duration_bucket": duration_bucket(float(r["duration"] or 0)),
                "publish_hour": hour,
                "visual_style": (r["visual_style"] or "unknown")[:40],
                "views": int(r["views"] or 0),
                "retention": float(r["avg_view_percentage"] or 0.0),
                "ctr": float(r["ctr"] or 0.0),
                "subs": int(r["subscribers_gained"] or 0),
                "engagement": int(r["likes"] or 0) + int(r["comments"] or 0),
            })
        return out

    @staticmethod
    def _performance(sample: dict[str, Any], max_views: float) -> float:
        """Blend retention, reach and subscriber conversion into one 0..1 score.

        Retention is weighted highest: it is the signal YouTube itself rewards,
        and it is far less noisy than raw views on a small channel.
        """
        retention = clamp(sample["retention"] / 100.0)
        reach = clamp(sample["views"] / max_views) if max_views > 0 else 0.0
        subs = clamp(sample["subs"] / max(sample["views"], 1) * 100.0)
        engagement = clamp(sample["engagement"] / max(sample["views"], 1) * 20.0)
        return clamp(retention * 0.50 + reach * 0.24
                     + subs * 0.14 + engagement * 0.12)

    # ------------------------------------------------------------------
    def learn(self) -> list[StrategyInsight]:
        samples = self._samples()
        if len(samples) < MIN_SAMPLES:
            log_event("LEARN", "not enough published data yet",
                      samples=len(samples), needed=MIN_SAMPLES)
            return []

        max_views = max((s["views"] for s in samples), default=1.0) or 1.0
        for s in samples:
            s["performance"] = self._performance(s, max_views)
        overall = sum(s["performance"] for s in samples) / len(samples)

        insights: list[StrategyInsight] = []
        for dim in DIMENSIONS:
            groups: dict[str, list[dict[str, Any]]] = {}
            for s in samples:
                groups.setdefault(str(s[dim]), []).append(s)
            for value, members in groups.items():
                n = len(members)
                avg = sum(m["performance"] for m in members) / n
                # Shrink toward the overall mean when the sample is small
                # (a single lucky video must not rewrite the strategy).
                confidence = n / (n + 2.0)
                adjusted = overall + (avg - overall) * confidence
                weight = clamp(adjusted / max(overall, 1e-6), 0.25, 2.5)
                insight = StrategyInsight(
                    dimension=dim, value=value, samples=n, score=adjusted,
                    weight=weight,
                    avg_views=sum(m["views"] for m in members) / n,
                    avg_retention=sum(m["retention"] for m in members) / n)
                insights.append(insight)
                self.db.upsert_strategy(dim, value, weight, n, adjusted)

        insights.sort(key=lambda i: (i.dimension, -i.weight))
        log_event("LEARN", "strategy updated", samples=len(samples),
                  dimensions=len(DIMENSIONS), insights=len(insights))
        return insights

    # ------------------------------------------------------------------
    def hints(self, *, min_samples: int = MIN_SAMPLES) -> str:
        """Human-readable hints injected into the idea/script prompts."""
        lines: list[str] = []
        for dim in DIMENSIONS:
            rows = self.db.query(
                "SELECT value, weight, samples, score FROM strategy_weights "
                "WHERE dimension=? AND samples>=? ORDER BY weight DESC",
                (dim, min_samples))
            if len(rows) < 2:
                continue
            best, worst = rows[0], rows[-1]
            if best["weight"] - worst["weight"] < 0.20:
                continue          # no meaningful difference yet
            lines.append(
                f"- {dim}: '{best['value']}' outperforms '{worst['value']}' "
                f"({best['weight']:.2f}x vs {worst['weight']:.2f}x, "
                f"n={best['samples']}/{worst['samples']})")
        if not lines:
            return ""
        return ("LEARNED FROM THIS CHANNEL'S OWN PUBLISHED PERFORMANCE "
                "(weighted statistics over own-channel analytics):\n"
                + "\n".join(lines)
                + "\nPrefer the better-performing options where it does not hurt "
                  "the specific concept.")

    def report(self) -> dict[str, Any]:
        insights = [i.to_dict() for i in self.learn()]
        return {
            "method": ("weighted statistics with small-sample shrinkage; "
                       "not machine learning"),
            "min_samples_to_apply": MIN_SAMPLES,
            "insights": insights,
            "hints": self.hints(),
        }
