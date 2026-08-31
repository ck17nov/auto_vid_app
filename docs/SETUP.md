# SETUP.md

## 1. Requirements

| Thing | Version | Why |
|---|---|---|
| Python | 3.11+ | backend + engine |
| FFmpeg + ffprobe | 6.0+ (7/8/9 fine) | all rendering. **Non-negotiable.** |
| Android Studio | Ladybug (2024.2) or newer | to build the app |
| JDK | 17 | required by AGP 8.7 |

Verify FFmpeg has the filters the pipeline needs:

```bash
ffmpeg -filters | grep -E "zoompan|subtitles|xfade|sidechaincompress|loudnorm"
```

All five must appear. A minimal/static FFmpeg build without `libass` cannot burn
captions — install a full build (on Windows, the gyan.dev "full" build).

## 2. Backend

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

On Linux/macOS use `.venv/bin/python` throughout instead.

`tzdata` is in requirements because Windows has no system timezone database and
scheduling would fail without it.

```bash
cp .env.example .env
```

Fill in `.env` (see [API_SETUP.md](API_SETUP.md) and
[YOUTUBE_SETUP.md](YOUTUBE_SETUP.md)), then:

```bash
.venv/Scripts/python -m backend.cli doctor
```

This prints a table of every dependency and credential, marks each OK / MISSING
/ OPTIONAL, and tells you what degrades if something is absent.

### Minimum to produce a video

- FFmpeg installed
- `YOUTUBE_API_KEY` set (research needs it)

With no LLM key, scripts fall back to the deterministic template builder — real
output, but formulaic prose, and clearly labelled as such. Add `GROQ_API_KEY`
(free, no card) for genuinely good scripts.

### First run, no upload

```bash
.venv/Scripts/python -m backend.cli run --niche "science" --length 45 --dry-run
```

Inspect the job folder printed at the end. Everything is there.

### Start the API the phone talks to

Set `AUTOTUBE_API_TOKEN` in `.env` first — the backend refuses to serve
authenticated endpoints without it, by design.

```bash
.venv/Scripts/python -m backend.cli serve --host 0.0.0.0 --port 8099
```

Interactive docs at `http://localhost:8099/docs`.

Find the LAN address the phone should use:

```bash
ipconfig
```

Use the IPv4 address of the adapter your phone shares Wi-Fi with, e.g.
`http://192.168.1.20:8099/`.

## 3. Android app

Open the `android/` folder in Android Studio and let it sync. It will download
the Gradle distribution named in `gradle/wrapper/gradle-wrapper.properties` and
generate the wrapper scripts on first sync.

To build from the command line you need `gradlew`. Generate it once:

```bash
gradle wrapper --gradle-version 8.9
```

Then:

```bash
cd android && ./gradlew assembleDebug
```

The APK lands in `android/app/build/outputs/apk/debug/`.

Install to a connected phone:

```bash
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

### In the app, first launch

1. **Settings** -> Backend URL (`http://192.168.1.20:8099/`) and the API key
   (the same `AUTOTUBE_API_TOKEN`). Tap **Test connection**.
2. **Settings** -> paste your **Android OAuth client ID**, tap
   **Connect YouTube**, complete Google sign-in. The app sends only the refresh
   token to your backend; your password is never seen by the app.
3. **Create** -> niche, length, language, schedule -> **START AUTOMATION**.
4. **Dashboard** -> watch progress; approve when it asks.

Cleartext HTTP is permitted only for `localhost`, `10.0.2.2` and private
`192.168.x.x` ranges (see `network_security_config.xml`). Anything on the public
internet must be HTTPS — put the backend behind a reverse proxy with TLS.

## 4. Optional quality upgrades

**Sharper visuals** — get a free Pixabay and/or Pexels API key (no credit card)
and set `PEXELS_API_KEY` / `PIXABAY_API_KEY`. Real photographs at 3000-6000 px
are visibly sharper than free AI generation, which caps near 576x1024.

**Offline, licence-clean voice** — install Piper and drop a voice model in
`assets/piper/`:

```bash
.venv/Scripts/python -m pip install piper-tts
```

Download a `.onnx` + `.onnx.json` voice pair from the Piper releases page into
`assets/piper/`. The chain then works with no network at all.

**Your own music** — drop public-domain or properly licensed `.mp3`/`.wav` files
into `assets/music/`. They are preferred over the synthesised bed automatically,
looped and loudness-matched. Nothing is ever downloaded from a music site.

## 5. Configuration

`config.yaml` holds every tunable. The ones that matter most:

```yaml
youtube:
  upload_enabled: false      # master safety switch - keep false until happy
  default_privacy: private
quality:
  minimum_score: 80
automation:
  approval_required: true    # APPROVAL mode is the default
  daily_video_limit: 3
dry_run: true                # no upload, full artifacts
```

Environment variables override the file: `DRY_RUN`, `UPLOAD_ENABLED`,
`AUTOTUBE_WORKSPACE`, `AUTOTUBE_CONFIG`.

**Automatic public publishing is off by default and requires three separate
changes** (`dry_run: false`, `upload_enabled: true`, `default_privacy: public`).
That is deliberate.

## 6. Scheduling without keeping the phone online

Set an upload time and the backend produces the video ahead of the slot, then
hands YouTube a `publishAt` timestamp. YouTube publishes it. Your phone does not
need to be awake, online, or even switched on at 8 PM.

On the phone, WorkManager re-checks status every 15 minutes and survives reboots.
There is no long-running foreground service, because Android does not guarantee
one.

## 7. Tests

```bash
.venv/Scripts/python -m pytest tests/ -v
```
