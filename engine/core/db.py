"""SQLite persistence.

The Android app owns its own Room/SQLite database; this is the backend mirror.
Both use the same table names and column semantics so the two stay in sync.

Credentials are deliberately NOT stored here (spec section 27) - OAuth tokens
live in a separate 0600 token store, see engine/youtube/auth.py.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .models import JobStatus, VideoJob

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS niche_profiles (
    name        TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,          -- JSON
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS research_videos (
    video_id        TEXT PRIMARY KEY,
    niche           TEXT NOT NULL,
    title           TEXT NOT NULL,
    channel_id      TEXT,
    channel_title   TEXT,
    published_at    TEXT,
    views           INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    comments        INTEGER DEFAULT 0,
    duration_seconds INTEGER DEFAULT 0,
    is_short        INTEGER DEFAULT 0,
    view_velocity   REAL DEFAULT 0,
    engagement_rate REAL DEFAULT 0,
    performance_ratio REAL DEFAULT 0,
    is_breakout     INTEGER DEFAULT 0,
    viral_score     REAL DEFAULT 0,
    payload         TEXT NOT NULL,      -- full JSON
    fetched_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_research_niche ON research_videos(niche, fetched_at);

CREATE TABLE IF NOT EXISTS content_ideas (
    idea_id     TEXT PRIMARY KEY,
    niche       TEXT NOT NULL,
    topic       TEXT,
    working_title TEXT,
    hook_type   TEXT,
    opportunity_score REAL DEFAULT 0,
    used        INTEGER DEFAULT 0,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scripts (
    script_id   TEXT PRIMARY KEY,
    idea_id     TEXT,
    language    TEXT,
    provider    TEXT,
    retention_score REAL DEFAULT 0,
    text_hash   TEXT,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_scripts_hash ON scripts(text_hash);

CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    asset       TEXT NOT NULL,
    source      TEXT NOT NULL,
    license     TEXT NOT NULL,
    prompt      TEXT,
    attribution TEXT,
    url         TEXT,
    scene_index INTEGER DEFAULT -1,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_assets_job ON assets(job_id);

CREATE TABLE IF NOT EXISTS video_jobs (
    job_id      TEXT PRIMARY KEY,
    automation_id TEXT,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    error       TEXT DEFAULT '',
    retry_count INTEGER DEFAULT 0,
    scheduled_for TEXT DEFAULT '',
    youtube_video_id TEXT DEFAULT '',
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON video_jobs(status, updated_at);

CREATE TABLE IF NOT EXISTS published_videos (
    youtube_video_id TEXT PRIMARY KEY,
    job_id      TEXT,
    title       TEXT,
    niche       TEXT,
    topic       TEXT,
    hook_type   TEXT,
    title_type  TEXT,
    duration    REAL,
    visual_style TEXT,
    published_time TEXT,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_video_id TEXT NOT NULL,
    collected_at REAL NOT NULL,
    views       INTEGER DEFAULT 0,
    watch_time_minutes REAL DEFAULT 0,
    avg_view_duration REAL DEFAULT 0,
    avg_view_percentage REAL DEFAULT 0,
    likes       INTEGER DEFAULT 0,
    comments    INTEGER DEFAULT 0,
    subscribers_gained INTEGER DEFAULT 0,
    impressions INTEGER DEFAULT 0,
    ctr         REAL DEFAULT 0,          -- own channel only
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_analytics_video ON analytics(youtube_video_id, collected_at);

CREATE TABLE IF NOT EXISTS schedules (
    id          TEXT PRIMARY KEY,
    automation_id TEXT NOT NULL,
    job_id      TEXT DEFAULT '',
    publish_at  TEXT NOT NULL,          -- RFC3339 UTC
    local_time  TEXT NOT NULL,
    timezone    TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'PENDING',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_schedules_state ON schedules(state, publish_at);

CREATE TABLE IF NOT EXISTS service_configs (
    name        TEXT PRIMARY KEY,
    enabled     INTEGER DEFAULT 1,
    settings    TEXT NOT NULL,          -- JSON, never secrets
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_usage (
    day         TEXT PRIMARY KEY,       -- YYYY-MM-DD (Pacific, per Google reset)
    units       INTEGER NOT NULL DEFAULT 0,
    detail      TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS strategy_weights (
    dimension   TEXT NOT NULL,          -- hook_type | title_type | duration_bucket ...
    value       TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    samples     INTEGER NOT NULL DEFAULT 0,
    score       REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (dimension, value)
);
"""


class Database:
    """Thread-safe thin wrapper over sqlite3 (WAL, one shared connection)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---- low level ---------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- settings ----------------------------------------------------
    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO user_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM user_settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    # ---- jobs --------------------------------------------------------
    def save_job(self, job: VideoJob) -> None:
        job.updated_at = time.time()
        self.execute(
            "INSERT INTO video_jobs(job_id,automation_id,status,created_at,updated_at,"
            "error,retry_count,scheduled_for,youtube_video_id,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "status=excluded.status, updated_at=excluded.updated_at, error=excluded.error,"
            "retry_count=excluded.retry_count, scheduled_for=excluded.scheduled_for,"
            "youtube_video_id=excluded.youtube_video_id, payload=excluded.payload",
            (job.job_id, job.automation_id, job.status, job.created_at, job.updated_at,
             job.error, job.retry_count, job.scheduled_for, job.youtube_video_id,
             json.dumps(job.to_dict(), ensure_ascii=False)),
        )

    def get_job(self, job_id: str) -> VideoJob | None:
        row = self.query_one("SELECT payload FROM video_jobs WHERE job_id=?", (job_id,))
        return VideoJob.from_dict(json.loads(row["payload"])) if row else None

    def list_jobs(self, status: str | None = None, limit: int = 100) -> list[VideoJob]:
        if status:
            rows = self.query(
                "SELECT payload FROM video_jobs WHERE status=? "
                "ORDER BY updated_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.query(
                "SELECT payload FROM video_jobs ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [VideoJob.from_dict(json.loads(r["payload"])) for r in rows]

    def pending_jobs(self, limit: int = 50) -> list[VideoJob]:
        terminal = (JobStatus.PUBLISHED.value, JobStatus.FAILED.value,
                    JobStatus.REJECTED.value, JobStatus.AWAITING_APPROVAL.value,
                    JobStatus.SCHEDULED.value, JobStatus.READY.value)
        placeholders = ",".join("?" * len(terminal))
        rows = self.query(
            f"SELECT payload FROM video_jobs WHERE status NOT IN ({placeholders}) "
            "ORDER BY created_at ASC LIMIT ?", (*terminal, limit))
        return [VideoJob.from_dict(json.loads(r["payload"])) for r in rows]

    def count_jobs_since(self, since_ts: float, statuses: tuple[str, ...]) -> int:
        placeholders = ",".join("?" * len(statuses))
        row = self.query_one(
            f"SELECT COUNT(*) c FROM video_jobs WHERE updated_at >= ? "
            f"AND status IN ({placeholders})", (since_ts, *statuses))
        return int(row["c"]) if row else 0

    # ---- research ----------------------------------------------------
    def save_research(self, niche: str, videos: list[Any]) -> None:
        ts = time.time()
        for v in videos:
            d = v.to_dict() if hasattr(v, "to_dict") else dict(v)
            self.execute(
                "INSERT INTO research_videos(video_id,niche,title,channel_id,channel_title,"
                "published_at,views,likes,comments,duration_seconds,is_short,view_velocity,"
                "engagement_rate,performance_ratio,is_breakout,viral_score,payload,fetched_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(video_id) DO UPDATE SET views=excluded.views,"
                "likes=excluded.likes, comments=excluded.comments,"
                "view_velocity=excluded.view_velocity, viral_score=excluded.viral_score,"
                "payload=excluded.payload, fetched_at=excluded.fetched_at",
                (d["video_id"], niche, d["title"], d.get("channel_id"), d.get("channel_title"),
                 d.get("published_at"), d.get("views", 0), d.get("likes", 0),
                 d.get("comments", 0), d.get("duration_seconds", 0),
                 int(bool(d.get("is_short"))), d.get("view_velocity", 0.0),
                 d.get("engagement_rate", 0.0), d.get("performance_ratio", 0.0),
                 int(bool(d.get("is_breakout"))), d.get("viral_score", 0.0),
                 json.dumps(d, ensure_ascii=False), ts),
            )

    def recent_research(self, niche: str, max_age_hours: float) -> list[dict[str, Any]]:
        cutoff = time.time() - max_age_hours * 3600
        rows = self.query(
            "SELECT payload FROM research_videos WHERE niche=? AND fetched_at>=? "
            "ORDER BY viral_score DESC", (niche, cutoff))
        return [json.loads(r["payload"]) for r in rows]

    # ---- ideas / scripts --------------------------------------------
    def save_idea(self, niche: str, idea: Any) -> None:
        d = idea.to_dict() if hasattr(idea, "to_dict") else dict(idea)
        self.execute(
            "INSERT INTO content_ideas(idea_id,niche,topic,working_title,hook_type,"
            "opportunity_score,used,payload,created_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(idea_id) DO UPDATE SET payload=excluded.payload",
            (d["idea_id"], niche, d.get("topic"), d.get("working_title"),
             d.get("hook_type"), d.get("opportunity_score", 0.0), 0,
             json.dumps(d, ensure_ascii=False), time.time()),
        )

    def mark_idea_used(self, idea_id: str) -> None:
        self.execute("UPDATE content_ideas SET used=1 WHERE idea_id=?", (idea_id,))

    def used_topics(self, niche: str, limit: int = 200) -> list[str]:
        rows = self.query(
            "SELECT topic FROM content_ideas WHERE niche=? AND used=1 "
            "ORDER BY created_at DESC LIMIT ?", (niche, limit))
        return [r["topic"] or "" for r in rows]

    def save_script(self, script: Any, text_hash: str) -> None:
        d = script.to_dict() if hasattr(script, "to_dict") else dict(script)
        self.execute(
            "INSERT INTO scripts(script_id,idea_id,language,provider,retention_score,"
            "text_hash,payload,created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(script_id) DO UPDATE SET payload=excluded.payload",
            (d["script_id"], d.get("idea_id"), d.get("language"), d.get("provider"),
             d.get("retention_score", 0.0), text_hash,
             json.dumps(d, ensure_ascii=False), time.time()),
        )

    def recent_script_texts(self, limit: int = 50) -> list[tuple[str, str]]:
        rows = self.query(
            "SELECT script_id, payload FROM scripts ORDER BY created_at DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            try:
                out.append((r["script_id"], json.loads(r["payload"]).get("script", "")))
            except (ValueError, TypeError):
                continue
        return out

    # ---- assets ------------------------------------------------------
    def save_assets(self, job_id: str, assets: list[Any]) -> None:
        for a in assets:
            d = a.to_dict() if hasattr(a, "to_dict") else dict(a)
            self.execute(
                "INSERT INTO assets(job_id,asset,source,license,prompt,attribution,url,"
                "scene_index,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, d["asset"], d["source"], d["license"], d.get("prompt", ""),
                 d.get("attribution", ""), d.get("url", ""), d.get("scene_index", -1),
                 time.time()),
            )

    # ---- published + analytics --------------------------------------
    def save_published(self, job: VideoJob) -> None:
        script = job.script or {}
        idea = job.idea or {}
        meta = job.metadata or {}
        self.execute(
            "INSERT INTO published_videos(youtube_video_id,job_id,title,niche,topic,"
            "hook_type,title_type,duration,visual_style,published_time,payload) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(youtube_video_id) DO UPDATE SET "
            "payload=excluded.payload",
            (job.youtube_video_id, job.job_id, meta.get("title", ""),
             (job.request or {}).get("niche", ""), idea.get("topic", ""),
             idea.get("hook_type", ""), _title_type(meta.get("title", "")),
             script.get("estimated_duration", 0.0),
             (job.request or {}).get("style", ""),
             job.published_at or job.scheduled_for,
             json.dumps(job.to_dict(), ensure_ascii=False)),
        )

    def save_analytics(self, video_id: str, row: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO analytics(youtube_video_id,collected_at,views,watch_time_minutes,"
            "avg_view_duration,avg_view_percentage,likes,comments,subscribers_gained,"
            "impressions,ctr,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (video_id, time.time(), row.get("views", 0),
             row.get("watch_time_minutes", 0.0), row.get("avg_view_duration", 0.0),
             row.get("avg_view_percentage", 0.0), row.get("likes", 0),
             row.get("comments", 0), row.get("subscribers_gained", 0),
             row.get("impressions", 0), row.get("ctr", 0.0),
             json.dumps(row, ensure_ascii=False)),
        )

    # ---- quota -------------------------------------------------------
    def add_quota(self, day: str, units: int, op: str) -> int:
        row = self.query_one("SELECT units, detail FROM quota_usage WHERE day=?", (day,))
        detail = json.loads(row["detail"]) if row else {}
        detail[op] = detail.get(op, 0) + 1
        total = (int(row["units"]) if row else 0) + units
        self.execute(
            "INSERT INTO quota_usage(day,units,detail) VALUES(?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET units=excluded.units, detail=excluded.detail",
            (day, total, json.dumps(detail)))
        return total

    def quota_used(self, day: str) -> int:
        row = self.query_one("SELECT units FROM quota_usage WHERE day=?", (day,))
        return int(row["units"]) if row else 0

    # ---- strategy ----------------------------------------------------
    def upsert_strategy(self, dimension: str, value: str, weight: float,
                        samples: int, score: float) -> None:
        self.execute(
            "INSERT INTO strategy_weights(dimension,value,weight,samples,score,updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(dimension,value) DO UPDATE SET "
            "weight=excluded.weight, samples=excluded.samples, score=excluded.score,"
            "updated_at=excluded.updated_at",
            (dimension, value, weight, samples, score, time.time()))

    def strategy(self, dimension: str) -> dict[str, float]:
        rows = self.query(
            "SELECT value, weight FROM strategy_weights WHERE dimension=?", (dimension,))
        return {r["value"]: float(r["weight"]) for r in rows}


def _title_type(title: str) -> str:
    t = (title or "").lower()
    if t.startswith(("why", "how", "what", "who", "when", "can ", "is ", "do ")) or "?" in t:
        return "question"
    if any(t.startswith(f"{n} ") or f" {n} " in t[:14] for n in
           ("3", "5", "7", "10", "top")):
        return "listicle"
    if any(w in t for w in ("found", "discovered", "revealed", "hidden", "secret")):
        return "discovery"
    if any(w in t for w in ("never", "stop", "don't", "mistake", "wrong")):
        return "warning"
    return "statement"
