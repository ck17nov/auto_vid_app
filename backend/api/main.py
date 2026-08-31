"""Backend HTTP API - the surface the Android app talks to (spec section 28).

Security (spec section 31):
  * every mutating endpoint requires the X-API-Key header (AUTOTUBE_API_TOKEN)
  * a simple per-IP token bucket rate limiter
  * no secret ever appears in a response or a log line
  * requests are validated by pydantic models, not hand-parsed

Heavy work never runs inside a request: jobs are queued to a background worker
thread, and the phone polls job status. That is what lets the Android app stay
responsive and survive being backgrounded by Android.
"""
from __future__ import annotations

import queue
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Literal

from fastapi import (BackgroundTasks, Depends, FastAPI, Header, HTTPException,
                     Query, Request)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.core.config import load_config                        # noqa: E402
from engine.core.logging import log_event, setup_logging          # noqa: E402
from engine.core.models import AutomationRequest, JobStatus       # noqa: E402
from engine.core.niche import build_profile, is_kids_niche        # noqa: E402
from engine.core.util import have_ffmpeg                          # noqa: E402

CFG = load_config()
setup_logging(jsonl=CFG.workspace / "logs" / "api.jsonl")

app = FastAPI(
    title="AutoTube AI backend",
    version="0.1.0",
    description="Research, produce and publish original YouTube videos.",
)

# The Android app talks over HTTPS to a host the user controls; CORS is only
# relevant for a browser dashboard, so keep it explicit and narrow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in str(CFG.get("api.cors_origins", "")).split(",") if o],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# ==========================================================================
# Auth + rate limiting
# ==========================================================================
def _expected_token() -> str:
    return CFG.secret("AUTOTUBE_API_TOKEN")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = _expected_token()
    if not expected:
        # Fail closed: an unauthenticated backend that can upload to someone's
        # YouTube channel is not an acceptable default.
        raise HTTPException(
            status_code=503,
            detail="AUTOTUBE_API_TOKEN is not set on the backend. "
                   "Set it in .env and restart (see docs/SECURITY.md).")
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


_BUCKETS: dict[str, deque] = defaultdict(deque)
_RATE_LOCK = threading.Lock()
RATE_LIMIT = int(CFG.get("api.rate_limit_per_minute", 60))


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    client = request.client.host if request.client else "unknown"
    now = time.time()
    with _RATE_LOCK:
        bucket = _BUCKETS[client]
        while bucket and now - bucket[0] > 60.0:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={"detail": f"rate limit: {RATE_LIMIT} requests/minute"})
        bucket.append(now)
    return await call_next(request)


# ==========================================================================
# Request models
# ==========================================================================
class AutomationBody(BaseModel):
    niche: str = Field(min_length=2, max_length=120)
    audience: str = Field(default="18-35", max_length=60)
    language: str = Field(default="en", max_length=12)
    video_format: Literal["SHORT", "LONGFORM"] = "SHORT"
    duration_seconds: int = Field(default=45, ge=8, le=3600)
    style: str = Field(default="fast-paced, curiosity-driven", max_length=200)
    count: int = Field(default=1, ge=1, le=10)
    mode: Literal["AUTO", "APPROVAL"] = "APPROVAL"
    frequency: Literal["once", "daily", "weekly", "days"] = "once"
    days: list[int] = Field(default_factory=list)
    upload_time: str = Field(default="", max_length=5)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    made_for_kids: bool = False
    keywords: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("upload_time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        if not v:
            return v
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError("upload_time must be HH:MM")
        if not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            raise ValueError("upload_time out of range")
        return v

    @field_validator("days")
    @classmethod
    def _check_days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days must be 0 (Mon) to 6 (Sun)")
        return v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(f"unknown timezone: {v}") from exc
        return v

    def to_request(self) -> AutomationRequest:
        return AutomationRequest(**self.model_dump())


class TokenBody(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=4096)
    # The Android OAuth client that minted the token. Optional for backwards
    # compatibility, but without it the backend has to guess, and refreshing an
    # Android-issued token with the desktop client's credentials fails.
    client_id: str = Field(default="", max_length=256)


class RejectBody(BaseModel):
    reason: str = Field(default="", max_length=400)


# ==========================================================================
# Job worker (spec sections 21, 22)
# ==========================================================================
class Worker:
    """Single background thread that drains the job queue.

    One at a time on purpose: rendering is CPU-bound, and the target user is
    running this on a laptop or a small free-tier box, not a render farm.
    """

    def __init__(self) -> None:
        self.queue: queue.Queue[AutomationRequest] = queue.Queue()
        self.thread: threading.Thread | None = None
        self.current: str | None = None
        self.pipeline = None
        self._lock = threading.Lock()
        self.history: deque[dict[str, Any]] = deque(maxlen=50)

    def _ensure_pipeline(self):
        if self.pipeline is None:
            from engine.pipeline import Pipeline
            self.pipeline = Pipeline(CFG)
        return self.pipeline

    def start(self) -> None:
        with self._lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, daemon=True,
                                           name="autotube-worker")
            self.thread.start()

    def submit(self, request: AutomationRequest) -> None:
        self.queue.put(request)
        self.start()

    def _loop(self) -> None:
        while True:
            try:
                request = self.queue.get(timeout=2.0)
            except queue.Empty:
                continue
            pipeline = self._ensure_pipeline()
            for _ in range(max(1, request.count)):
                try:
                    result = pipeline.run(request)
                    self.current = result.job.job_id
                    self.history.append({
                        "job_id": result.job.job_id,
                        "status": result.job.status,
                        "quality": (result.quality.score if result.quality else 0),
                        "at": time.time()})
                except Exception as exc:
                    log_event("WORKER", "job failed", error=str(exc)[:300])
                    self.history.append({"job_id": None, "status": "FAILED",
                                         "error": str(exc)[:300], "at": time.time()})
            self.current = None
            self.queue.task_done()

    @property
    def depth(self) -> int:
        return self.queue.qsize()


WORKER = Worker()


def _db():
    from engine.core.db import Database
    return Database(CFG.workspace / "autotube.db")


# ==========================================================================
# Public endpoints
# ==========================================================================
@app.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness + capability probe."""
    from engine.content.llm import LLMRouter
    from engine.tts.providers import build_providers
    router = LLMRouter(list(CFG.get("content.llm_provider_order", [])), CFG)
    return {
        "ok": True,
        "version": app.version,
        "ffmpeg": have_ffmpeg(),
        "dry_run": CFG.dry_run,
        "upload_enabled": bool(CFG.get("youtube.upload_enabled")),
        "approval_required": bool(CFG.get("automation.approval_required")),
        "llm_providers": [p.name for p in router.usable],
        "tts_providers": [p.name for p in build_providers(
            list(CFG.get("tts.provider_order", []))) if p.available()],
        "research_configured": CFG.has_secret("YOUTUBE_API_KEY"),
        "auth_required": bool(_expected_token()),
        "queue_depth": WORKER.depth,
    }


@app.get("/config", dependencies=[Depends(require_api_key)])
def get_config() -> dict[str, Any]:
    """Non-secret configuration, for the Settings screen."""
    return {
        "quality": CFG.get("quality", {}),
        "video": CFG.get("video", {}),
        "captions": CFG.get("captions", {}),
        "automation": CFG.get("automation", {}),
        "timezone": CFG.get("timezone", {}),
        "youtube": {k: v for k, v in (CFG.get("youtube", {}) or {}).items()
                    if k != "quota_costs"},
        "tts": {"provider_order": CFG.get("tts.provider_order"),
                "voice_gender": CFG.get("tts.voice_gender")},
        "dry_run": CFG.dry_run,
    }


@app.get("/niche/preview", dependencies=[Depends(require_api_key)])
def niche_preview(niche: str = Query(min_length=2, max_length=120),
                  audience: str = "18-35", style: str = "",
                  duration: int = Query(45, ge=8, le=3600)) -> dict[str, Any]:
    """Show how a niche will be interpreted before starting an automation."""
    profile = build_profile(niche, audience=audience, style=style,
                            duration_seconds=duration)
    return {"profile": profile.to_dict(),
            "kids_niche_detected": is_kids_niche(niche),
            "requires_kids_confirmation": is_kids_niche(niche)}


@app.post("/automations", dependencies=[Depends(require_api_key)],
          status_code=202)
def create_automation(body: AutomationBody) -> dict[str, Any]:
    """Queue an automation run. Returns immediately; poll /jobs for progress."""
    request = body.to_request()

    # Kids content must be explicitly confirmed (spec section 9).
    if is_kids_niche(request.niche) and not request.made_for_kids:
        raise HTTPException(
            status_code=409,
            detail={"error": "kids_confirmation_required",
                    "message": ("This niche looks child-directed. Confirm the "
                                "'Made for Kids' classification before "
                                "publishing."),
                    "niche": request.niche})

    pipeline_problems: list[str] = []
    if not have_ffmpeg():
        pipeline_problems.append("ffmpeg not installed on the backend")
    if not CFG.has_secret("YOUTUBE_API_KEY"):
        pipeline_problems.append("YOUTUBE_API_KEY not configured on the backend")
    if pipeline_problems:
        raise HTTPException(status_code=503,
                           detail={"error": "backend_not_ready",
                                   "problems": pipeline_problems})

    WORKER.submit(request)
    log_event("API", "automation queued", niche=request.niche,
              count=request.count, mode=request.mode)
    return {"accepted": True, "automation_id": request.id,
            "queued": WORKER.depth,
            "note": "poll GET /jobs for progress"}


@app.get("/jobs", dependencies=[Depends(require_api_key)])
def list_jobs(status: str = "", limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    db = _db()
    try:
        jobs = db.list_jobs(status.upper() or None, limit=limit)
        return {
            "queue_depth": WORKER.depth,
            "recent_worker_results": list(WORKER.history)[-10:],
            "jobs": [_job_summary(j) for j in jobs],
        }
    finally:
        db.close()


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: str) -> dict[str, Any]:
    db = _db()
    try:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        data = job.to_dict()
        # Point the app at the download endpoints rather than raw disk paths.
        data["media"] = {
            "video": f"/jobs/{job_id}/file/video" if job.video_path else None,
            "thumbnail": f"/jobs/{job_id}/file/thumbnail" if job.thumbnail_path else None,
            "subtitle": f"/jobs/{job_id}/file/subtitle" if job.subtitle_path else None,
            "voice": f"/jobs/{job_id}/file/voice" if job.voice_path else None,
        }
        return data
    finally:
        db.close()


@app.get("/jobs/{job_id}/file/{kind}", dependencies=[Depends(require_api_key)])
def get_job_file(job_id: str, kind: str):
    """Stream a produced artifact (video preview, thumbnail, captions)."""
    db = _db()
    try:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        mapping = {
            "video": (job.video_path, "video/mp4"),
            "thumbnail": (job.thumbnail_path, "image/jpeg"),
            "subtitle": (job.subtitle_path, "text/plain"),
            "voice": (job.voice_path, "audio/wav"),
        }
        if kind not in mapping:
            raise HTTPException(status_code=400, detail="unknown file kind")
        raw, media_type = mapping[kind]
        if not raw:
            raise HTTPException(status_code=404, detail=f"{kind} not produced")
        path = Path(raw).resolve()
        # Path containment check: never serve outside the workspace.
        workspace = CFG.workspace.resolve()
        if workspace not in path.parents and path != workspace:
            raise HTTPException(status_code=403, detail="path outside workspace")
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"{kind} file missing")
        return FileResponse(str(path), media_type=media_type,
                            filename=path.name)
    finally:
        db.close()


@app.post("/jobs/{job_id}/approve", dependencies=[Depends(require_api_key)])
def approve_job(job_id: str, background: BackgroundTasks) -> dict[str, Any]:
    """Approve a job awaiting review; upload/schedule runs in the background."""
    from engine.pipeline import Pipeline, PipelineError

    def do_approve() -> None:
        pipe = Pipeline(CFG)
        try:
            pipe.approve(job_id)
        except PipelineError as exc:
            log_event("API", "approval failed", job=job_id, error=str(exc)[:200])
        finally:
            pipe.close()

    db = _db()
    try:
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        if job.status != JobStatus.AWAITING_APPROVAL.value:
            raise HTTPException(
                status_code=409,
                detail=f"job is {job.status}, not awaiting approval")
    finally:
        db.close()

    background.add_task(do_approve)
    return {"accepted": True, "job_id": job_id}


@app.post("/jobs/{job_id}/reject", dependencies=[Depends(require_api_key)])
def reject_job(job_id: str, body: RejectBody) -> dict[str, Any]:
    from engine.pipeline import Pipeline
    pipe = Pipeline(CFG)
    try:
        job = pipe.reject(job_id, body.reason)
        return {"job_id": job.job_id, "status": job.status}
    finally:
        pipe.close()


# ==========================================================================
@app.get("/research", dependencies=[Depends(require_api_key)])
def research(niche: str = Query(min_length=2, max_length=120),
             video_format: Literal["SHORT", "LONGFORM"] = "SHORT",
             limit: int = Query(20, ge=1, le=50)) -> dict[str, Any]:
    """Run (or serve cached) research for the Research screen."""
    from engine.pipeline import Pipeline
    from engine.research.gaps import cluster_videos, find_gaps
    pipe = Pipeline(CFG)
    try:
        profile = build_profile(niche)
        videos = pipe.research_engine.research(niche, profile,
                                              video_format=video_format)
        clusters = cluster_videos(videos)
        gaps = find_gaps(clusters, videos)
        return {
            "niche": niche,
            "quota_used_today": pipe.quota.used(),
            "quota_limit": pipe.quota.limit,
            "videos": [v.to_dict() for v in videos[:limit]],
            "breakouts": [v.video_id for v in videos if v.is_breakout],
            "clusters": [c.to_dict() for c in clusters],
            "gaps": [g.to_dict() for g in gaps],
            "disclaimer": ("ctr_potential_score is a heuristic over public "
                           "signals. YouTube does not expose other channels' "
                           "real CTR."),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:400]) from exc
    finally:
        pipe.close()


@app.get("/analytics", dependencies=[Depends(require_api_key)])
def analytics(days: int = Query(28, ge=1, le=365),
              collect: bool = False) -> dict[str, Any]:
    """Own-channel analytics + the learned strategy."""
    from engine.pipeline import Pipeline
    pipe = Pipeline(CFG)
    try:
        if collect:
            return pipe.collect_analytics(days=days)
        rows = pipe.db.query(
            "SELECT youtube_video_id, MAX(collected_at) AS at, views, "
            "avg_view_percentage, ctr, subscribers_gained, likes, comments "
            "FROM analytics GROUP BY youtube_video_id "
            "ORDER BY at DESC LIMIT 100")
        return {
            "videos": [dict(r) for r in rows],
            "strategy": pipe.learner.report(),
            "note": "CTR is available only for your own authenticated channel.",
        }
    finally:
        pipe.close()


@app.get("/quota", dependencies=[Depends(require_api_key)])
def quota() -> dict[str, Any]:
    from engine.pipeline import Pipeline
    pipe = Pipeline(CFG)
    try:
        return {
            "used_today": pipe.quota.used(),
            "limit": pipe.quota.limit,
            "reserved_for_uploads": pipe.quota.reserve,
            "available_for_research": pipe.quota.remaining(),
            "costs": pipe.quota.costs,
            "max_uploads_per_day": pipe.quota.limit // pipe.quota.cost("video_insert"),
            "resets": "midnight US Pacific",
        }
    finally:
        pipe.close()


# ==========================================================================
@app.get("/youtube/status", dependencies=[Depends(require_api_key)])
def youtube_status() -> dict[str, Any]:
    from engine.youtube.auth import YouTubeAuth
    auth = YouTubeAuth(CFG)
    out: dict[str, Any] = {"configured": auth.configured,
                           "authorized": auth.authorized, "channels": []}
    if auth.authorized:
        try:
            out["channels"] = auth.channels()
        except Exception as exc:
            out["error"] = str(exc)[:240]
    return out


@app.post("/youtube/token", dependencies=[Depends(require_api_key)])
def import_token(body: TokenBody) -> dict[str, Any]:
    """Receive the refresh token the Android app obtained via AppAuth.

    The token is written to the 0600 token store; it is never logged and never
    returned by any endpoint. The client id is stored with it because an
    Android client is a public PKCE client and only that same client can
    refresh the token.
    """
    from engine.youtube.auth import YouTubeAuth
    auth = YouTubeAuth(CFG)
    auth.import_refresh_token(body.refresh_token, body.client_id)
    return {"stored": True, "authorized": auth.authorized,
            "refreshable": bool(body.client_id or auth.configured)}


def _job_summary(job) -> dict[str, Any]:
    meta = job.metadata or {}
    quality = job.quality or {}
    request = job.request or {}
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "niche": request.get("niche", ""),
        "title": meta.get("title", ""),
        "quality_score": quality.get("score", 0),
        "quality_passed": quality.get("passed", False),
        "blockers": quality.get("blockers", []),
        "retention_score": (job.script or {}).get("retention_score", 0),
        "duration": (job.script or {}).get("estimated_duration", 0),
        "youtube_video_id": job.youtube_video_id,
        "scheduled_for": job.scheduled_for,
        "error": job.error,
        "retry_count": job.retry_count,
        "has_video": bool(job.video_path),
        "has_thumbnail": bool(job.thumbnail_path),
    }


@app.on_event("startup")
def on_startup() -> None:
    log_event("API", "backend started", dry_run=CFG.dry_run,
              upload_enabled=bool(CFG.get("youtube.upload_enabled")),
              auth_configured=bool(_expected_token()))
    # Recover anything interrupted by the last shutdown (spec section 22).
    try:
        from engine.pipeline import Pipeline
        pipe = Pipeline(CFG)
        recovered = pipe.resume_pending()
        pipe.close()
        if recovered:
            log_event("API", "interrupted jobs found", count=len(recovered))
    except Exception as exc:
        log_event("API", "recovery scan failed", error=str(exc)[:200])
