# TROUBLESHOOTING.md

Start here:

```bash
.venv/Scripts/python -m backend.cli doctor
```

It checks every dependency and credential and says what degrades if one is
missing.

---

## Setup

### `ffmpeg not found on PATH`
Install a **full** build. A minimal/static build without `libass` cannot burn
captions and without `libx264` cannot encode. Verify:

```bash
ffmpeg -filters | grep -E "zoompan|subtitles|xfade|sidechaincompress|loudnorm"
```

All five must be present. On Windows use the gyan.dev "full" build; on Debian /
Ubuntu `apt install ffmpeg`; on macOS `brew install ffmpeg`.

### `No time zone found with key Asia/Kolkata`
Windows has no system timezone database.

```bash
.venv/Scripts/python -m pip install tzdata
```

It is already in `requirements.txt`; this means the install did not complete.

### `no usable font found`
Put any `.ttf` in `assets/fonts/`. The engine tries to download Anton (SIL OFL)
automatically, then falls back to a system font — if both failed you are offline
and have no Arial/Impact/DejaVu.

---

## Research

### `YOUTUBE_API_KEY is not set - research cannot run`
Research is the one stage with a hard external dependency. See
[YOUTUBE_SETUP.md](YOUTUBE_SETUP.md) — free, no credit card.

### `YouTube API 403 (check key restrictions)`
Almost always an **Application restriction** on the API key. A server-side call
has no HTTP referrer and its IP may vary. Set Application restrictions to
**None** and use API restrictions (YouTube Data API v3 only) instead.

### `quotaExceeded`
You have spent 10,000 units today. Check with `backend.cli quota`. It resets at
midnight **US Pacific**, not your local midnight. Reduce
`research.max_queries` (each search costs 100 units) or wait.

### `research returned no videos for '<niche>'`
The filters are strict by design. Loosen them in `config.yaml`:

```yaml
research:
  published_within_days: 180   # was 90
  min_views: 1000              # was 5000
```

Also try a broader niche — "science" returns more than "quantum chromodynamics".

---

## Script and ideas

### Scripts read formulaic / `provider: template`
No LLM is configured, so the deterministic builder ran. That is the labelled
degraded path, not a bug. Set `GROQ_API_KEY` (free, no card) and re-run.

### `template builder could not fill the word budget`
The LLM-free builder has a finite pool of honest framing lines and cannot fill a
long video without repeating itself. Either shorten the target duration or
configure an LLM. Padding further would mean inventing content.

### Ollama times out
CPU-only inference is impractically slow — measured at over 10 minutes for one
call on a 7B model during development. Use Groq or Gemini for interactive work,
or run Ollama on a machine with a GPU.

### `all ideas were near-duplicates; keeping the most distinct one flagged`
Your research corpus is dominated by one framing. The kept idea is tagged
`originality_review` and will need approval. Broaden the niche or extend
`research.published_within_days` for more variety.

---

## Voice

### `edge-tts` returns HTTP 403
Microsoft rotated their token scheme; the client needs updating. This happened
during development with version 7.0.0.

```bash
.venv/Scripts/python -m pip install --upgrade edge-tts
```

If it stays broken, install Piper — offline, unlimited, and licence-clean:

```bash
.venv/Scripts/python -m pip install piper-tts
```

then drop a `.onnx` voice into `assets/piper/`.

### Captions drift out of sync
Check `exact=True` in the `[TTS]` log lines. With edge-tts, word timings come
from the engine and are exact. With Piper or gTTS they are **estimated** from
word length and punctuation — good, but not frame-accurate. Use edge-tts, or
accept the estimate.

### Narration sounds unnaturally slow or fast
The duration re-fit adjusted the speaking rate to hit your target. Look for
`re-fitting narration to target duration` in the log. It means the script's word
count did not match the requested length — usually the template builder on a long
target. A real LLM produces a correctly-sized script and no re-fit is needed.

---

## Visuals

### One scene looks abstract while the others are photographs
A network provider failed for that scene and the procedural fallback rendered it.
The engine retries transient failures (429, timeouts, 5xx) with backoff before
falling back, but a persistent failure still degrades gracefully rather than
aborting the render. Check the `[VISUAL]` log for the reason.

### `429 Too Many Requests` from Pollinations
It rate-limits on concurrency. `visuals.parallel` is already 2; lower it to 1 if
it persists. Getting a free Pixabay/Pexels key avoids this entirely and looks
better.

### AI images look soft
Pollinations caps output near 576x1024 regardless of the size requested, so it
must be upscaled about 2x to fill 1080x1920. The Lanczos + unsharp chain
compensates, but real stock photography is visibly sharper. Set
`PIXABAY_API_KEY` or `PEXELS_API_KEY`.

---

## Rendering

### Rendering is very slow
A 45-second Short is a few minutes of CPU. It gets dramatically worse under
contention — during development, six scene clips took 40 s idle and 8.5 minutes
while large downloads ran. Do not render and do something else CPU-heavy at once.

To trade quality for speed in `config.yaml`:

```yaml
video:
  preset: fast     # medium -> fast is roughly 2x quicker
  crf: 21          # 19 -> 21 is smaller and slightly softer
```

### `command failed (143)`
Exit 143 is SIGTERM — something killed FFmpeg, normally an outer timeout or the
OOM killer. Look for a wrapping `timeout` and check available RAM. Not a code
fault.

### Captions do not appear in the output
1. Confirm `libass`: `ffmpeg -filters | grep subtitles`
2. Confirm a font exists in `assets/fonts/`
3. On Windows, filter paths need the drive-colon escaped (`C\:/path`); the code
   does this in `_ffmpeg_path`. If you moved a path into a filter by hand, escape
   it.

### `tremolo` / filter parameter errors
Filter parameter ranges differ between FFmpeg versions. `tremolo` rejects
frequencies below 0.1 Hz, which is why the music moods are clamped to that floor.
If you edit `music.py`, respect the documented ranges for your build.

---

## Quality gate

### `QUALITY SCORE` below the minimum, upload refused
That is the gate working. Read `quality_report.json` — every check has a
`detail` field with the measured value. Common causes:

| Blocker | Meaning | Fix |
|---|---|---|
| `audio_not_silent` | mean volume below the floor | TTS produced silence — check the `[TTS]` log |
| `duration_correct` | drifted past tolerance | script length wrong; configure an LLM |
| `resolution` | not 1080x1920 | someone changed `video.default_resolution` |
| `originality` | too similar to existing text | genuine duplicate; regenerate |
| `encoding_compatible` | wrong codec/pix_fmt | a modified FFmpeg command |

Lower `quality.minimum_score` if you disagree with the weighting — but the
**blocking** checks are not score-based and will still stop a broken upload.

### `no_long_silence` fails
A gap longer than `quality.max_silence_seconds` (1.6 s). Usually a scene whose
narration failed to synthesise. Check the per-scene `[TTS]` lines.

---

## Upload

### `YouTube account not connected`
```bash
.venv/Scripts/python -m backend.cli auth login
```

### Nothing uploads, no error
Three switches must all be right:

```yaml
dry_run: false
youtube:
  upload_enabled: true
```

`dry_run` also honours the `DRY_RUN` environment variable, which overrides the
file. `GET /health` shows the effective values.

### `thumbnail not set` warning
Custom thumbnails require a phone-verified channel. The video still uploaded;
this is a warning, not a failure.

### Scheduled video published immediately
`publishAt` is only honoured while `privacyStatus` is `private`. The uploader
forces this when a schedule is present — if you bypassed `build_body`, that is
the cause.

### Refresh token stops working after 7 days
Your Google OAuth consent screen is in **Testing** mode, where refresh tokens
expire weekly. Publish the app to stop it. See
[YOUTUBE_SETUP.md](YOUTUBE_SETUP.md#3-oauth-consent-screen).

---

## Android app

### `Cannot reach the backend at ...`
1. Phone and PC on the same Wi-Fi.
2. Backend bound to `0.0.0.0`, not `127.0.0.1`:
   `serve --host 0.0.0.0 --port 8099`
3. Windows Firewall is prompting or blocking — allow Python on private networks.
4. Use the LAN IP (`192.168.x.x`), not `localhost`, from a physical phone.
   `10.0.2.2` is only for the emulator.

### `Backend rejected the API key`
The app's key must match `AUTOTUBE_API_TOKEN` exactly. Re-paste it; a trailing
space from clipboard is the usual culprit (the store trims, but check anyway).

### Cleartext HTTP blocked
Only `localhost`, `127.0.0.1`, `10.0.2.2` and `192.168.x.x` are permitted. For
anything else use HTTPS. Do not add a public host to the cleartext allow-list.

### Preview will not play
The media endpoint is authenticated and the player sends `X-API-Key`. If the key
is wrong the stream fails. Also confirm the job actually has a video
(`has_video: true` in `/jobs`).

### No approval notifications
Android 13+ requires the runtime notification permission. The app asks on first
launch; if it was denied, enable it in Android system settings for AutoTube AI.

---

## Still stuck

Reproduce with full logging and read the structured log:

```bash
AUTOTUBE_LOG_LEVEL=DEBUG .venv/Scripts/python -m backend.cli run --niche "science" --dry-run
```

Every stage tags its output (`[RESEARCH]`, `[SCRIPT]`, `[TTS]`, `[VISUAL]`,
`[VIDEO]`, `[QUALITY]`, `[YOUTUBE]`), and the job directory contains the
intermediate artifact for every stage, so you can see exactly which one produced
the wrong thing. Secrets are redacted, so the log is safe to inspect.
