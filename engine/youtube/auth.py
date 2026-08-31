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
            self._lock_down_windows()

    @staticmethod
    def _current_sid() -> str:
        """The current user's SID, or "" if it cannot be determined.

        Granting by NAME is not safe. On a machine where the account name
        contains a hyphen and matches the host name - "Sushma-Chandan" on
        "SUSHMA-CHANDAN" - icacls resolved `Sushma-Chandan:F` to the principal
        `SUSHMA-CHANDAN\\` with an EMPTY account, so the grant landed nowhere,
        inheritance had already been stripped, and the token file became
        unreadable by everyone including the process that wrote it. Uploads
        then failed with a bare PermissionError.

        A SID has no parsing ambiguity, so that is what we grant to.
        """
        import subprocess
        try:
            proc = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, timeout=30)
        except Exception:
            return ""
        for field in reversed((proc.stdout or "").strip().split(",")):
            candidate = field.strip().strip('"')
            if candidate.upper().startswith("S-1-"):
                return candidate
        return ""

    def _lock_down_windows(self) -> None:
        """Harden the ACL, then CHECK it did not lock us out.

        Security hardening that breaks the feature it protects is worse than no
        hardening, so this verifies readability afterwards and reverts if the
        file became inaccessible.
        """
        import subprocess

        sid = self._current_sid()
        if not sid:
            log_event("YOUTUBE", "token file ACL left at inherited permissions",
                      reason="could not resolve the current user SID")
            return
        try:
            subprocess.run(
                ["icacls", str(self.path), "/inheritance:r",
                 "/grant:r", f"*{sid}:F"],
                capture_output=True, timeout=30, check=False)
        except Exception as exc:
            log_event("YOUTUBE", "could not harden token file ACL",
                      error=str(exc)[:120])
            return

        try:
            self.path.read_text(encoding="utf-8")
            return                          # hardened and still readable
        except OSError as exc:
            log_event("YOUTUBE", "ACL hardening locked out the owner, reverting",
                      error=str(exc)[:120])
        try:
            subprocess.run(["icacls", str(self.path), "/reset"],
                           capture_output=True, timeout=30, check=False)
            subprocess.run(["icacls", str(self.path), "/grant",
                            f"*{sid}:F"],
                           capture_output=True, timeout=30, check=False)
        except Exception as exc:
            log_event("YOUTUBE", "could not restore token file access",
                      error=str(exc)[:120],
                      hint=f"fix manually: icacls \"{self.path}\" /reset")

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
        """True if a DESKTOP flow can be started here (needs id + secret)."""
        return bool(self.client_id and self.client_secret)

    @property
    def authorized(self) -> bool:
        """True if we hold a refresh token AND a client that can refresh it.

        Two shapes are valid, and the distinction matters:

        * Desktop flow (`autotube auth login`) - a confidential client, so the
          stored token is refreshed with YOUTUBE_CLIENT_ID + SECRET from .env.
        * Phone flow (AppAuth on Android) - a PUBLIC client using PKCE, which
          has no secret at all. Its refresh token is bound to the Android
          client ID, so it can only be refreshed with THAT id and no secret.

        Refreshing an Android-issued token with the desktop client's
        credentials fails with `unauthorized_client`, which is why the issuing
        client id is stored alongside the token rather than assumed.
        """
        if not self.store.exists():
            return False
        data = self.store.read()
        if not data.get("refresh_token"):
            return False
        return bool(data.get("client_id") or self.configured)

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

        data = self.store.read()
        if not data.get("refresh_token"):
            raise AuthError(
                "not authorized yet - run `autotube auth login`, or connect "
                "YouTube from the Android app")

        # Refresh with the client that ISSUED the token. A phone-issued token
        # comes from a public Android client (PKCE, no secret); a desktop token
        # comes from the confidential client in .env. Mixing them fails with
        # `unauthorized_client`, and the error does not say which half is wrong.
        stored_client = str(data.get("client_id") or "").strip()
        if stored_client:
            client_id = stored_client
            client_secret = None if data.get("public_client") else self.client_secret
        else:
            if not self.configured:
                raise AuthError(
                    "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET are not set, and "
                    "the stored token did not record its own client id. Either "
                    "set both in .env, or reconnect from the Android app so it "
                    "can send its client id. See docs/YOUTUBE_SETUP.md")
            client_id = self.client_id
            client_secret = self.client_secret

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data["refresh_token"],
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=client_id,
            client_secret=client_secret,
            scopes=data.get("scopes", SCOPES),
        )
        if not creds.valid:
            creds.refresh(Request())
            self._persist(creds)
            log_event("YOUTUBE", "access token refreshed")
        return creds

    def _persist(self, creds) -> None:
        """Write refreshed credentials back, KEEPING the issuing client.

        This used to replace the whole record, which silently discarded the
        `client_id` / `public_client` fields written by a phone authorisation.
        The effect was that connecting from the Android app worked exactly
        once: the first refresh succeeded, wiped the client id, and every
        refresh after that fell back to the desktop credentials in .env and
        failed with `unauthorized_client`.
        """
        previous = self.store.read() if self.store.exists() else {}
        record = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "scopes": list(creds.scopes or SCOPES),
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
        }
        for carried in ("client_id", "public_client"):
            if previous.get(carried):
                record[carried] = previous[carried]
        self.store.write(record)

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

    def import_refresh_token(self, refresh_token: str,
                             client_id: str = "") -> None:
        """Accept a refresh token obtained by the Android app (AppAuth).

        `client_id` is the Android OAuth client that minted the token. Record
        it: the token can ONLY be refreshed by that same client, with no
        secret, because an Android client is a public PKCE client. Without it
        the backend fell back to YOUTUBE_CLIENT_ID/SECRET from .env and Google
        rejected the refresh with `unauthorized_client` - so authorising on the
        phone appeared to succeed and then never worked.
        """
        if not refresh_token.strip():
            raise AuthError("empty refresh token")
        record: dict[str, Any] = {
            "token": None,
            "refresh_token": refresh_token.strip(),
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": SCOPES,
        }
        if client_id.strip():
            record["client_id"] = client_id.strip()
            record["public_client"] = True
        self.store.write(record)
        log_event("YOUTUBE", "refresh token imported from device",
                  client=("device client recorded" if client_id.strip()
                          else "no client id sent - will use .env credentials"))

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
