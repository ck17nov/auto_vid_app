# YOUTUBE_SETUP.md

Everything here is free. No credit card. No billing account.

You need two separate credentials:

| Credential | Used for | Where it lives |
|---|---|---|
| **API key** | read-only research (`search`, `videos`, `channels`) | backend `.env` |
| **OAuth client** | upload, thumbnails, captions, your own analytics | backend `.env` + the app |

---

## 1. Create the project and enable the APIs

1. Open <https://console.cloud.google.com/> and sign in with the Google account
   that owns the YouTube channel.
2. Create a project, e.g. `autotube-ai`.
3. **APIs and Services -> Library**, enable both:
   - **YouTube Data API v3**
   - **YouTube Analytics API**

## 2. API key (research)

1. **APIs and Services -> Credentials -> Create credentials -> API key**.
2. Copy it into `.env`:

   ```
   YOUTUBE_API_KEY=AIza...
   ```

3. Recommended: **Edit API key -> API restrictions -> Restrict key ->** YouTube
   Data API v3 only. Leave *Application* restrictions as **None** — an IP or
   HTTP-referrer restriction will make the backend's server-side calls fail
   with 403.

## 3. OAuth consent screen

1. **APIs and Services -> OAuth consent screen**.
2. User type **External**. (Internal only exists for Workspace organisations.)
3. Fill in app name, support email, developer email.
4. **Scopes** — add exactly these three:
   - `.../auth/youtube.upload`
   - `.../auth/youtube`
   - `.../auth/yt-analytics.readonly`
5. **Test users** — add your own Google account.
6. Leave the app in **Testing** if you like.

   **Important:** while the app is in Testing, refresh tokens expire after
   **7 days**, so you will have to reconnect weekly. Clicking **Publish app**
   stops that. Because you are only ever accessing your own channel's data,
   publishing a personal-use app is usually straightforward — but if Google asks
   for verification of these scopes, you either complete it or accept the weekly
   reconnect. This is Google policy, not a limitation of this project.

## 4. OAuth client for the BACKEND (desktop flow)

1. **Credentials -> Create credentials -> OAuth client ID**.
2. Application type: **Desktop app**.
3. Copy both values into `.env`:

   ```
   YOUTUBE_CLIENT_ID=....apps.googleusercontent.com
   YOUTUBE_CLIENT_SECRET=GOCSPX-...
   ```

4. Authorise from the CLI:

   ```bash
   .venv/Scripts/python -m backend.cli auth login
   ```

   A browser window opens; approve access. The refresh token is written to
   `workspace/secrets/youtube_token.json` with restrictive permissions (0600 on
   POSIX; the ACL is tightened to your user on Windows).

5. Confirm it worked:

   ```bash
   .venv/Scripts/python -m backend.cli auth channels
   ```

## 5. OAuth client for the ANDROID APP

The app can authorise on the phone instead, then hand the refresh token to your
backend.

1. **Credentials -> Create credentials -> OAuth client ID**.
2. Application type: **Android**.
3. Package name: `com.autotube.ai` — use `com.autotube.ai.debug` if you are
   installing the debug build, because the debug build sets
   `applicationIdSuffix = ".debug"`.
4. SHA-1 of your signing certificate. For the debug keystore:

   ```bash
   keytool -list -v -keystore ~/.android/debug.keystore -alias androiddebugkey -storepass android -keypass android
   ```

   On Windows the keystore is at `%USERPROFILE%\.android\debug.keystore`.

5. There is **no client secret** for an Android client. The app uses PKCE, which
   is the correct pattern for a public client. Never put a client secret in an
   APK — it is trivially extractable.
6. In the app: **Settings -> Android OAuth client ID**, paste it, then tap
   **Connect YouTube**.

The app stores the refresh token in EncryptedSharedPreferences (backed by the
Android Keystore) and POSTs it once to `/youtube/token`. The backend writes it to
its own restricted token store. Neither ever logs it, and no endpoint ever
returns it.

---

## 6. Quota: the real constraint

Default: **10,000 units/day per project**, resetting at midnight US Pacific.

| Call | Units |
|---|---:|
| `search.list` | 100 |
| `videos.list` (up to 50 ids) | 1 |
| `channels.list` (up to 50 ids) | 1 |
| **`videos.insert` (upload)** | **1,600** |
| `thumbnails.set` | 50 |
| `captions.insert` | 400 |

A full research run costs about **302 units**. A published video with a
thumbnail and captions costs **2,050**. So the practical ceiling is about
**four fully-featured uploads per day**, research included.

Check your spend at any time:

```bash
.venv/Scripts/python -m backend.cli quota
```

`QuotaGuard` reserves `daily_video_limit x 1600` units so research can never eat
the budget you need for publishing, and refuses a call that would exceed the cap
rather than letting Google return an opaque 403 mid-upload.

You can apply to Google for additional quota (free, requires an audit, not
guaranteed). Do not plan around receiving it.

---

## 7. Things that will bite you

**Custom thumbnails require a verified channel.** If your channel has not been
phone-verified, `thumbnails.set` fails. The pipeline records this as a warning,
not a failure — the video still uploads.

**`publishAt` is only honoured while the video is private.** Setting a schedule
together with `privacyStatus: public` publishes immediately. The uploader forces
`private` whenever a schedule is present. Do not "fix" that.

**`selfDeclaredMadeForKids` must be set at upload time** and cannot be inferred
later by the API. Getting it wrong has legal consequences in some jurisdictions.
The app asks explicitly, and the quality gate blocks a mismatch between the niche
profile and the flag.

**New channels may be restricted to private uploads** until verified. That is an
account state, not an API error.

**Uploads are resumable** in 4 MB chunks, because mobile connections drop. A 5xx
on a chunk is retried with exponential backoff rather than restarting the upload.

**Deleting and re-uploading the same video repeatedly** looks like spam to
YouTube. The anti-duplicate checks exist to stop the automation doing that
accidentally; do not disable them.
