"""YouTube OAuth 2.0 (spec sections 19, 30, 31).

Never stores a password.  Stores only the OAuth refresh/access token, in a file
with 0600 permissions, OUTSIDE the normal database (spec section 27).

Scopes:
  youtube.upload          - insert videos
  youtube                 - set thumbnails, manage playlists
  yt-analytics.readonly   - own-channel analytics only

The Android app performs its own OAuth via AppAuth and posts the resulting
refresh token to the backend, or the backend runs the desktop flow directly -
both paths land in the same TokenStore.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from ..core.config import Config
from ..core.logging import log_event

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


class AuthError(RuntimeError):
    pass


class TokenStore:
    """Token persistence with restrictive permissions."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 10

    def read(self) -> dict[str, Any]:
        if not self.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self._lock_down()

    def _lock_down(self) -> None:
        """0600 on POSIX; on Windows, restrict the ACL to the current user."""
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        if os.name == "nt":
            try:
                import subprocess
                user = os.environ.get("USERNAME", "")
                if user:
                    subprocess.run(
                        ["icacls", str(self.path), "/inheritance:r",
                         "/grant:r", f"{user}:F"],
                        capture_output=True, timeout=30, check=False)
            except Exception:
                pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class YouTubeAuth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client_id = cfg.secret("YOUTUBE_CLIENT_ID")
        self.client_secret = cfg.secret("YOUTUBE_CLIENT_SECRET")
        self.store = TokenStore(cfg.workspace / "secrets" / "youtube_token.json")

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def authorized(self) -> bool:
        return self.store.exists() and bool(self.store.read().get("refresh_token"))

    # ------------------------------------------------------------------
    def _client_config(self) -> dict[str, Any]:
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }

    def credentials(self):
        """Return live google-auth Credentials, refreshing if needed."""
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not self.configured:
            raise AuthError(
                "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET are not set. "
                "See docs/YOUTUBE_SETUP.md")
        data = self.store.read()
        if not data.get("refresh_token"):
            raise AuthError("not authorized yet - run: autotube auth login")

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data["refresh_token"],
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=self.client_id,
            client_secret=self.client_secret,
            scopes=data.get("scopes", SCOPES),
        )
        if not creds.valid:
            creds.refresh(Request())
            self._persist(creds)
            log_event("YOUTUBE", "access token refreshed")
        return creds

    def _persist(self, creds) -> None:
        self.store.write({
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "scopes": list(creds.scopes or SCOPES),
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
        })

    # ------------------------------------------------------------------
    def login_local_server(self, port: int = 8765) -> dict[str, Any]:
        """Desktop OAuth flow: opens a browser, captures the redirect."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.configured:
            raise AuthError("OAuth client not configured - see docs/YOUTUBE_SETUP.md")
        flow = InstalledAppFlow.from_client_config(self._client_config(), SCOPES)
        creds = flow.run_local_server(
            port=port, prompt="consent", access_type="offline",
            authorization_prompt_message=(
                "Open this URL to authorise AutoTube AI:\n{url}"),
            success_message=("Authorised. You can close this tab and return to "
                             "the terminal."))
        self._persist(creds)
        log_event("YOUTUBE", "authorised", scopes=len(SCOPES))
        return self.store.read()

    def import_refresh_token(self, refresh_token: str) -> None:
        """Accept a refresh token obtained by the Android app (AppAuth)."""
        if not refresh_token.strip():
            raise AuthError("empty refresh token")
        self.store.write({
            "token": None,
            "refresh_token": refresh_token.strip(),
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": SCOPES,
        })
        log_event("YOUTUBE", "refresh token imported from device")

    def logout(self) -> None:
        self.store.clear()
        log_event("YOUTUBE", "credentials cleared")

    # ------------------------------------------------------------------
    def service(self, name: str = "youtube", version: str = "v3"):
        from googleapiclient.discovery import build
        return build(name, version, credentials=self.credentials(),
                     cache_discovery=False)

    def channels(self) -> list[dict[str, Any]]:
        """The authorised user's channels (for channel selection in the app)."""
        yt = self.service()
        resp = yt.channels().list(part="id,snippet,statistics,status",
                                  mine=True, maxResults=50).execute()
        out = []
        for item in resp.get("items", []):
            snippet = item.get("snippet", {}) or {}
            stats = item.get("statistics", {}) or {}
            out.append({
                "channel_id": item.get("id", ""),
                "title": snippet.get("title", ""),
                "custom_url": snippet.get("customUrl", ""),
                "thumbnail": (snippet.get("thumbnails", {})
                              .get("default", {}).get("url", "")),
                "subscribers": int(stats.get("subscriberCount", 0) or 0),
                "videos": int(stats.get("videoCount", 0) or 0),
                "views": int(stats.get("viewCount", 0) or 0),
            })
        return out
