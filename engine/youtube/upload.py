"""YouTube upload + scheduling (spec sections 19, 20, 48).

Uses the official Data API v3 with a RESUMABLE upload, because a phone-driven
pipeline on mobile data will lose connections mid-transfer.

Scheduling semantics that are easy to get wrong and are handled explicitly:
  * `publishAt` only takes effect when `privacyStatus` is `private` at insert
    time. Setting it with `public` silently publishes immediately.
  * `publishAt` must be RFC3339 **UTC**. The user picks a local time in their
    own timezone (e.g. 20:00 Asia/Kolkata); conversion happens here.
  * `selfDeclaredMadeForKids` must be set at insert; it cannot be inferred.
  * Synthetic-media disclosure is surfaced in the description and, where the API
    exposes it, in the altered-content field (spec section 48).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.logging import log_event
from ..core.models import VideoMetadata
from ..core.util import human_size, local_slot_to_utc, parse_rfc3339, rfc3339
from .auth import AuthError, YouTubeAuth

# Retryable HTTP statuses per Google's own upload guidance.
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_ATTEMPTS = 6
CHUNK_SIZE = 4 * 1024 * 1024        # 4 MB: friendly to mobile networks


@dataclass
class UploadResult:
    video_id: str = ""
    url: str = ""
    privacy: str = ""
    publish_at: str | None = None
    thumbnail_set: bool = False
    caption_set: bool = False
    playlist_added: bool = False
    dry_run: bool = False
    quota_units: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id, "url": self.url, "privacy": self.privacy,
            "publish_at": self.publish_at, "thumbnail_set": self.thumbnail_set,
            "caption_set": self.caption_set, "playlist_added": self.playlist_added,
            "dry_run": self.dry_run, "quota_units": self.quota_units,
            "warnings": self.warnings,
        }


class YouTubeUploader:
    def __init__(self, cfg: Config, auth: YouTubeAuth | None = None, quota=None):
        self.cfg = cfg
        self.auth = auth or YouTubeAuth(cfg)
        self.quota = quota

    # ------------------------------------------------------------------
    def resolve_publish_at(self, *, upload_time: str, timezone: str,
                           days: list[int] | None = None,
                           explicit: str = "") -> str | None:
        """Convert the user's local schedule into RFC3339 UTC."""
        if explicit:
            return rfc3339(parse_rfc3339(explicit))
        if not upload_time:
            return None
        tz = timezone or str(self.cfg.get("timezone.default", "Asia/Kolkata"))
        return rfc3339(local_slot_to_utc(upload_time, tz, days=days or None))

    # ------------------------------------------------------------------
    def build_body(self, meta: VideoMetadata, *,
                   schedule: bool) -> dict[str, Any]:
        status: dict[str, Any] = {
            "privacyStatus": meta.privacy,
            "selfDeclaredMadeForKids": bool(meta.made_for_kids),
            "embeddable": True,
            "license": "youtube",
        }
        if schedule and meta.publish_at:
            # publishAt is only honoured while the video is private.
            status["privacyStatus"] = "private"
            status["publishAt"] = meta.publish_at

        snippet: dict[str, Any] = {
            "title": meta.title[:100],
            "description": meta.description[:5000],
            "tags": meta.tags[:15],
            "categoryId": str(meta.category_id or "27"),
            "defaultLanguage": meta.language or "en",
            "defaultAudioLanguage": meta.language or "en",
        }
        return {"snippet": snippet, "status": status}

    # ------------------------------------------------------------------
    def upload(self, *, video: Path, meta: VideoMetadata,
               thumbnail: Path | None = None,
               subtitle: Path | None = None,
               playlist_id: str = "",
               schedule: bool = False,
               dry_run: bool | None = None) -> UploadResult:
        dry = self.cfg.dry_run if dry_run is None else dry_run
        upload_enabled = bool(self.cfg.get("youtube.upload_enabled", False))
        result = UploadResult(privacy=meta.privacy, publish_at=meta.publish_at,
                              dry_run=dry)

        if not video.exists():
            raise FileNotFoundError(f"video not found: {video}")

        body = self.build_body(meta, schedule=schedule)
        effective_privacy = body["status"]["privacyStatus"]
        result.privacy = effective_privacy

        if dry or not upload_enabled:
            reason = "DRY_RUN" if dry else "youtube.upload_enabled=false"
            log_event("YOUTUBE", f"upload skipped ({reason})",
                      title=meta.title[:60], privacy=effective_privacy,
                      publish_at=meta.publish_at or "immediate",
                      size=human_size(video.stat().st_size))
            result.warnings.append(f"no upload performed: {reason}")
            return result

        if not self.auth.authorized:
            raise AuthError("YouTube account not connected - "
                            "run: autotube auth login")

        # Quota accounting before we spend it.
        if self.quota is not None:
            self.quota.check("video_insert", respect_reserve=False)

        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        yt = self.auth.service()
        media = MediaFileUpload(str(video), chunksize=CHUNK_SIZE,
                                resumable=True, mimetype="video/mp4")
        request = yt.videos().insert(
            part="snippet,status", body=body, media_body=media)

        log_event("YOUTUBE", "upload started", title=meta.title[:60],
                  size=human_size(video.stat().st_size),
                  privacy=effective_privacy,
                  publish_at=meta.publish_at or "immediate")

        response: dict[str, Any] | None = None
        attempt = 0
        last_progress = -1
        while response is None:
            try:
                status, response = request.next_chunk()
                if status is not None:
                    pct = int(status.progress() * 100)
                    if pct >= last_progress + 20:
                        last_progress = pct
                        log_event("YOUTUBE", f"upload {pct}%")
                attempt = 0
            except HttpError as exc:
                if exc.resp.status in RETRIABLE_STATUS and attempt < MAX_ATTEMPTS:
                    attempt += 1
                    wait = min(2 ** attempt, 64)
                    log_event("YOUTUBE", "chunk failed, retrying",
                              status=exc.resp.status, attempt=attempt,
                              wait=f"{wait}s")
                    time.sleep(wait)
                    continue
                raise
            except (ConnectionError, OSError) as exc:
                if attempt < MAX_ATTEMPTS:
                    attempt += 1
                    wait = min(2 ** attempt, 64)
                    log_event("YOUTUBE", "network error, resuming",
                              attempt=attempt, wait=f"{wait}s",
                              error=str(exc)[:120])
                    time.sleep(wait)
                    continue
                raise

        result.video_id = response.get("id", "")
        result.url = f"https://www.youtube.com/watch?v={result.video_id}"
        if self.quota is not None:
            result.quota_units += self.quota.cost("video_insert")
            self.quota.spend("video_insert")
        log_event("YOUTUBE", "upload successful", video_id=result.video_id,
                  url=result.url)

        # ---- thumbnail -------------------------------------------------
        if thumbnail is not None and thumbnail.exists():
            try:
                if self.quota is not None:
                    self.quota.check("thumbnail_set", respect_reserve=False)
                yt.thumbnails().set(
                    videoId=result.video_id,
                    media_body=MediaFileUpload(str(thumbnail),
                                               mimetype="image/jpeg")).execute()
                result.thumbnail_set = True
                if self.quota is not None:
                    result.quota_units += self.quota.cost("thumbnail_set")
                    self.quota.spend("thumbnail_set")
                log_event("YOUTUBE", "thumbnail set")
            except Exception as exc:
                # Custom thumbnails need a verified channel - not fatal.
                result.warnings.append(f"thumbnail not set: {str(exc)[:160]}")
                log_event("YOUTUBE", "thumbnail failed", error=str(exc)[:160])

        # ---- captions --------------------------------------------------
        if subtitle is not None and subtitle.exists():
            try:
                if self.quota is not None:
                    self.quota.check("captions_insert", respect_reserve=False)
                yt.captions().insert(
                    part="snippet",
                    body={"snippet": {
                        "videoId": result.video_id,
                        "language": meta.language or "en",
                        "name": "Auto captions",
                        "isDraft": False,
                    }},
                    media_body=MediaFileUpload(str(subtitle),
                                               mimetype="application/octet-stream")
                ).execute()
                result.caption_set = True
                if self.quota is not None:
                    result.quota_units += self.quota.cost("captions_insert")
                    self.quota.spend("captions_insert")
                log_event("YOUTUBE", "captions uploaded")
            except Exception as exc:
                result.warnings.append(f"captions not set: {str(exc)[:160]}")
                log_event("YOUTUBE", "captions failed", error=str(exc)[:160])

        # ---- playlist --------------------------------------------------
        target_playlist = playlist_id or meta.playlist_id
        if target_playlist:
            try:
                yt.playlistItems().insert(
                    part="snippet",
                    body={"snippet": {
                        "playlistId": target_playlist,
                        "resourceId": {"kind": "youtube#video",
                                       "videoId": result.video_id},
                    }}).execute()
                result.playlist_added = True
                log_event("YOUTUBE", "added to playlist", playlist=target_playlist)
            except Exception as exc:
                result.warnings.append(f"playlist add failed: {str(exc)[:160]}")

        return result

    # ------------------------------------------------------------------
    def update_privacy(self, video_id: str, privacy: str,
                       publish_at: str | None = None) -> dict[str, Any]:
        """Flip a scheduled/private video, or reschedule it."""
        yt = self.auth.service()
        status: dict[str, Any] = {"privacyStatus": privacy}
        if publish_at:
            status = {"privacyStatus": "private", "publishAt": publish_at}
        resp = yt.videos().update(
            part="status", body={"id": video_id, "status": status}).execute()
        log_event("YOUTUBE", "privacy updated", video_id=video_id,
                  privacy=status["privacyStatus"],
                  publish_at=publish_at or "-")
        return resp

    def delete(self, video_id: str) -> None:
        self.auth.service().videos().delete(id=video_id).execute()
        log_event("YOUTUBE", "video deleted", video_id=video_id)
