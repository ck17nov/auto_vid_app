"""YouTube research via the official Data API v3 (spec section 4).

No scraping.  Only `search.list`, `videos.list` and `channels.list`, all of
which are documented public endpoints.

QUOTA IS THE REAL CONSTRAINT, not money.  A new Google Cloud project gets
10,000 units/day:
    search.list      = 100 units   <- the expensive one
    videos.list      =   1 unit    (up to 50 ids per call)
    channels.list    =   1 unit    (up to 50 ids per call)
    videos.insert    = 1600 units  <- an upload
So a research run of 3 searches costs ~302 units and an upload costs 1600.
`QuotaGuard` tracks spend against the Pacific-midnight reset and refuses calls
that would exceed the budget, reserving room for the day's uploads.
"""
from __future__ import annotations

from typing import Any, Iterable

import httpx

from ..core.config import Config
from ..core.db import Database
from ..core.logging import log_event
from ..core.models import ResearchVideo
from ..core.niche import NicheProfile
from ..core.util import pacific_day, retry, utc_now
from .scoring import score_all

API_BASE = "https://www.googleapis.com/youtube/v3"


class QuotaExceeded(RuntimeError):
    pass


class QuotaGuard:
    """Tracks YouTube API unit spend per Pacific day."""

    def __init__(self, cfg: Config, db: Database | None = None):
        self.cfg = cfg
        self.db = db
        self.limit = int(cfg.get("youtube.daily_quota_units", 10000))
        self.costs: dict[str, int] = dict(cfg.get("youtube.quota_costs", {}) or {})
        # Keep room for the day's uploads so research cannot starve publishing.
        per_upload = self.costs.get("video_insert", 1600)
        self.reserve = per_upload * int(cfg.get("automation.daily_video_limit", 3))
        self._local = 0

    def cost(self, op: str) -> int:
        return int(self.costs.get(op, 1))

    def used(self) -> int:
        if self.db is not None:
            return self.db.quota_used(pacific_day())
        return self._local

    def remaining(self, *, respect_reserve: bool = True) -> int:
        cap = self.limit - (self.reserve if respect_reserve else 0)
        return max(cap - self.used(), 0)

    def check(self, op: str, *, respect_reserve: bool = True) -> None:
        need = self.cost(op)
        if need > self.remaining(respect_reserve=respect_reserve):
            raise QuotaExceeded(
                f"YouTube quota: {op} needs {need} units, "
                f"{self.remaining(respect_reserve=respect_reserve)} available "
                f"(used {self.used()}/{self.limit} today"
                + (f", {self.reserve} reserved for uploads)" if respect_reserve else ")"))

    def spend(self, op: str) -> int:
        units = self.cost(op)
        if self.db is not None:
            return self.db.add_quota(pacific_day(), units, op)
        self._local += units
        return self._local


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_iso8601_duration(value: str) -> int:
    """PT1M30S -> 90.  Handles hours/minutes/seconds and bare days."""
    import re
    if not value:
        return 0
    m = re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value.strip())
    if not m:
        return 0
    days, hours, minutes, seconds = (float(x) if x else 0.0 for x in m.groups())
    return int(days * 86400 + hours * 3600 + minutes * 60 + seconds)


class YouTubeResearch:
    def __init__(self, cfg: Config, db: Database | None = None,
                 quota: QuotaGuard | None = None):
        self.cfg = cfg
        self.db = db
        self.api_key = cfg.secret("YOUTUBE_API_KEY")
        self.quota = quota or QuotaGuard(cfg, db)
        self.timeout = 45

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {**params, "key": self.api_key}
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{API_BASE}/{path}", params=params)
        if resp.status_code == 403:
            body = resp.text[:400]
            if "quotaExceeded" in body:
                raise QuotaExceeded("YouTube API reports quotaExceeded for today")
            raise RuntimeError(f"YouTube API 403 (check key restrictions): {body}")
        if resp.status_code >= 400:
            raise RuntimeError(f"YouTube API {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------------
    def build_queries(self, niche: str, profile: NicheProfile,
                      extra_keywords: list[str] | None = None,
                      limit: int = 3) -> list[str]:
        base = niche.strip()
        queries = [base]
        for mod in profile.search_modifiers:
            queries.append(f"{base} {mod}")
        for kw in (extra_keywords or []):
            queries.append(f"{base} {kw}")
        # De-duplicate, preserve order, respect the quota-driven limit.
        seen: set[str] = set()
        out: list[str] = []
        for q in queries:
            key = q.lower().strip()
            if key and key not in seen:
                seen.add(key)
                out.append(q.strip())
        return out[:max(1, limit)]

    def search_ids(self, query: str, *, max_results: int = 25,
                   published_within_days: int = 90,
                   video_duration: str = "any",
                   region_code: str = "IN",
                   relevance_language: str = "en",
                   order: str = "viewCount") -> list[str]:
        from datetime import timedelta
        self.quota.check("search_list")
        published_after = (utc_now() - timedelta(days=published_within_days))
        params = {
            "part": "id",
            "q": query,
            "type": "video",
            "order": order,
            "maxResults": min(max(max_results, 1), 50),
            "publishedAfter": published_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "regionCode": region_code,
            "relevanceLanguage": relevance_language,
            "safeSearch": "moderate",
        }
        if video_duration in {"short", "medium", "long"}:
            params["videoDuration"] = video_duration
        data = retry(lambda: self._get("search", params), attempts=3, backoff=3,
                     tag="RESEARCH", what=f"search '{query}'")
        self.quota.spend("search_list")
        ids = [item["id"]["videoId"] for item in data.get("items", [])
               if item.get("id", {}).get("videoId")]
        log_event("RESEARCH", "search complete", query=query, results=len(ids),
                  quota_used=self.quota.used())
        return ids

    def hydrate(self, video_ids: list[str]) -> list[ResearchVideo]:
        """videos.list + channels.list for the full public signal set."""
        if not video_ids:
            return []
        videos: list[ResearchVideo] = []
        channel_ids: set[str] = set()

        for batch in _chunks(list(dict.fromkeys(video_ids)), 50):
            self.quota.check("videos_list")
            data = retry(lambda b=batch: self._get("videos", {
                "part": "snippet,statistics,contentDetails,status",
                "id": ",".join(b), "maxResults": 50}),
                attempts=3, backoff=3, tag="RESEARCH", what="videos.list")
            self.quota.spend("videos_list")
            for item in data.get("items", []):
                snippet = item.get("snippet", {}) or {}
                stats = item.get("statistics", {}) or {}
                details = item.get("contentDetails", {}) or {}
                duration = parse_iso8601_duration(details.get("duration", ""))
                thumbs = snippet.get("thumbnails", {}) or {}
                best_thumb = (thumbs.get("maxres") or thumbs.get("standard")
                              or thumbs.get("high") or thumbs.get("medium") or {})
                video = ResearchVideo(
                    video_id=item.get("id", ""),
                    title=snippet.get("title", ""),
                    channel_id=snippet.get("channelId", ""),
                    channel_title=snippet.get("channelTitle", ""),
                    published_at=snippet.get("publishedAt", ""),
                    duration_seconds=duration,
                    views=int(stats.get("viewCount", 0) or 0),
                    likes=int(stats.get("likeCount", 0) or 0),
                    comments=int(stats.get("commentCount", 0) or 0),
                    description=(snippet.get("description", "") or "")[:1500],
                    tags=list(snippet.get("tags", []) or [])[:25],
                    category_id=str(snippet.get("categoryId", "")),
                    thumbnail_url=best_thumb.get("url", ""),
                    # YouTube does not expose a "is Short" flag; duration <= 180s
                    # plus a vertical-friendly length is the practical proxy.
                    is_short=duration > 0 and duration <= 180,
                )
                if video.video_id:
                    videos.append(video)
                    if video.channel_id:
                        channel_ids.add(video.channel_id)

        stats_by_channel = self._channel_stats(sorted(channel_ids))
        for v in videos:
            cs = stats_by_channel.get(v.channel_id, {})
            v.channel_subscribers = cs.get("subscribers", 0)
            v.channel_video_count = cs.get("videos", 0)
            v.channel_total_views = cs.get("views", 0)
        return videos

    def _channel_stats(self, channel_ids: list[str]) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for batch in _chunks(channel_ids, 50):
            try:
                self.quota.check("channels_list")
            except QuotaExceeded:
                log_event("RESEARCH", "skipping channel stats - quota guard")
                break
            data = retry(lambda b=batch: self._get("channels", {
                "part": "statistics", "id": ",".join(b), "maxResults": 50}),
                attempts=3, backoff=3, tag="RESEARCH", what="channels.list")
            self.quota.spend("channels_list")
            for item in data.get("items", []):
                s = item.get("statistics", {}) or {}
                out[item.get("id", "")] = {
                    "subscribers": int(s.get("subscriberCount", 0) or 0),
                    "videos": int(s.get("videoCount", 0) or 0),
                    "views": int(s.get("viewCount", 0) or 0),
                }
        return out

    # ------------------------------------------------------------------
    def research(self, niche: str, profile: NicheProfile, *,
                 video_format: str = "SHORT",
                 extra_keywords: list[str] | None = None,
                 use_cache: bool = True) -> list[ResearchVideo]:
        """Full research run: cache -> search -> hydrate -> score."""
        cache_ttl = float(self.cfg.get("research.cache_ttl_hours", 12))
        if use_cache and self.db is not None:
            cached = self.db.recent_research(niche, cache_ttl)
            min_needed = int(self.cfg.get("research.max_results_per_query", 25))
            if len(cached) >= min_needed:
                log_event("RESEARCH", "using cached corpus", videos=len(cached),
                          niche=niche, ttl_hours=cache_ttl)
                return score_all(
                    [ResearchVideo.from_dict(c) for c in cached],
                    self.cfg.get("scoring.weights"),
                    float(self.cfg.get("scoring.breakout_ratio_threshold", 2.5)))

        if not self.configured:
            raise RuntimeError(
                "YOUTUBE_API_KEY is not set - research cannot run. "
                "See docs/YOUTUBE_SETUP.md (free, no credit card).")

        log_event("RESEARCH", "started", niche=niche, format=video_format)
        max_queries = int(self.cfg.get("research.max_queries", 3))
        # Never spend more than half the remaining budget on one research run.
        affordable = max(1, self.quota.remaining() // (2 * self.quota.cost("search_list")))
        queries = self.build_queries(niche, profile, extra_keywords,
                                     limit=min(max_queries, affordable))

        duration_filter = "short" if video_format != "LONGFORM" else "medium"
        all_ids: list[str] = []
        for q in queries:
            try:
                all_ids += self.search_ids(
                    q,
                    max_results=int(self.cfg.get("research.max_results_per_query", 25)),
                    published_within_days=int(self.cfg.get("research.published_within_days", 90)),
                    video_duration=duration_filter,
                    region_code=str(self.cfg.get("research.region_code", "IN")),
                    relevance_language=str(self.cfg.get("research.relevance_language", "en")),
                )
            except QuotaExceeded as exc:
                log_event("RESEARCH", "stopping searches", reason=str(exc)[:160])
                break

        videos = self.hydrate(all_ids)
        min_views = int(self.cfg.get("research.min_views", 5000))
        videos = [v for v in videos if v.views >= min_views]
        videos = score_all(
            videos, self.cfg.get("scoring.weights"),
            float(self.cfg.get("scoring.breakout_ratio_threshold", 2.5)))

        if self.db is not None and videos:
            self.db.save_research(niche, videos)
        breakout_count = sum(1 for v in videos if v.is_breakout)
        log_event("RESEARCH", f"{len(videos)} videos found", niche=niche,
                  quota_used=self.quota.used())
        log_event("RESEARCH", f"{breakout_count} breakout videos")
        if not videos:
            raise RuntimeError(
                f"research returned no videos for '{niche}' above "
                f"{min_views} views in the last "
                f"{self.cfg.get('research.published_within_days')} days")
        return videos
