# ARCHITECTURE.md

## Why the work is split this way

A Samsung Galaxy M34 5G is a capable phone and a poor render farm. It has mid-
range silicon, a battery, thermal limits, and an operating system that will
suspend your process the moment the screen turns off. Encoding a 1080x1920 H.264
video on it is possible and a bad idea.

So the phone does what phones are good at — collecting intent, showing state,
asking for approval — and a backend does what needs a real CPU.

```
   Android app  (Kotlin, Compose, Room, WorkManager)
        |
        |  HTTPS + X-API-Key
        v
   Backend API  (FastAPI)  ---->  Worker thread  ---->  Pipeline
        |                                                  |
        |                          +-----------------------+
        v                          v
   SQLite (jobs, research, analytics, quota, strategy)
                                   |
                        +----------+-----------+
                        v                      v
                  Engine modules          FFmpeg / Pillow
```

Everything crossing the network is small: JSON job state, and video only when
the user opens a preview. Video bytes never travel to the phone during
production, and never leave the backend on the way to YouTube.

---

## Deployment modes

`app.mode` in `config.yaml`:

| Mode | Where the engine runs | When to use it |
|---|---|---|
| `LOCAL` | your PC | default; free, fast, full control |
| `CLOUD` | a VM you control | phone-independent, always on |
| `HYBRID` | split | research/scripting cloud, rendering local |

The backend is replaceable because the app only depends on the HTTP contract in
`backend/api/main.py` — 18 endpoints, all documented at `/docs`.

---

## Request lifecycle

1. App POSTs `/automations`. Validation happens in pydantic models, not by hand.
2. The endpoint returns **202 immediately** and puts the request on a queue.
3. A single background worker thread drains it. One at a time on purpose:
   rendering is CPU-bound and the target host is a laptop, not a cluster.
4. Each stage persists to SQLite before and after it runs.
5. The app polls `/jobs`; WorkManager also polls every 15 minutes so the
   dashboard is current when the user opens the app.

A crash, a reboot or a killed worker resumes from the last persisted stage
rather than losing the job.

---

## The pipeline

`engine/pipeline.py` is the orchestrator. Each stage is individually retryable
with exponential backoff and writes its artifact into the job directory, which
*is* the dry-run output.

```
research  -> research.json           YouTube Data API, scored + clustered
idea      -> idea.json               gap analysis -> original concept
script    -> script.json             LLM (or template), retention pass
voice     -> voice.wav               per-scene TTS, word timings, duration re-fit
visuals   -> assets/, asset_manifest.json
render    -> captions.ass/.srt, music.wav, sfx.wav, master.wav, video.mp4
finalize  -> metadata.json, thumbnails/, originality_report.json,
             factcheck_report.json, quality_report.json
publish   -> upload_result.json      or AWAITING_APPROVAL
```

### Job states

```
IDEA -> RESEARCH -> SCRIPT -> VOICE -> VISUALS -> RENDERING -> QUALITY_CHECK
     -> AWAITING_APPROVAL -> READY -> SCHEDULED -> PUBLISHED -> ANALYZING
                                            \-> FAILED / REJECTED
```

---

## Decisions that shaped the code

### Per-scene TTS instead of one long clip

A single narration file forces you to *estimate* where each scene boundary falls,
and the visual cut drifts against the voice. Synthesising each scene separately
means the scene's real measured duration drives the timeline. Every cut lands on
a sentence boundary because it is defined by one.

### edge-tts as the default provider

Not because it sounds best, but because it returns **exact per-word timing
offsets**. That is what makes the karaoke captions frame-accurate rather than
interpolated. Piper is the licence-clean offline fallback; without word
boundaries its captions are estimated by weighting each word by character count
plus punctuation pauses, which tracks natural speech well but is not exact.

The trade-off is honest: edge-tts is an unofficial consumer endpoint with no SLA.
It returned HTTP 403 on version 7.0.0 during this build and needed 7.2.8. The
version is pinned and the fallback chain exists for exactly this reason.

### Duration is enforced against measured audio

The word budget comes from `duration x words_per_second`. After synthesis the
real length is measured, and if it misses the target by more than ~80% of the
configured tolerance, the speaking rate is recomputed and the narration is
re-synthesised. A "45 second" video that runs 78 seconds is a failed video, so
this is not optional.

### Two-pass render, exactly one lossy encode

- **Pass A** renders one near-lossless clip per scene with its Ken Burns motion.
  Parallel across cores; one bad image cannot abort the whole render.
- **Pass B** is a single FFmpeg invocation that cross-fades the clips, burns the
  captions, mixes voice + music + SFX, and does the one final encode.

One giant `filter_complex` with 13 `zoompan` chains, 12 `xfade`s and a
`subtitles` filter is possible, fragile and undebuggable. Three passes would
re-encode the video twice and cost quality. Two is the balance.

**Timeline maths.** An `xfade` chain outputs `sum(L) - (n-1)*T`. Clip lengths are
padded — `L_first = d + T/2`, `L_last = d + T/2`, `L_middle = d + T` — so total
video length equals total audio length *exactly*, and each transition is centred
on its narration boundary rather than starting after it.

### Ken Burns without softness

Source images are rendered at **1.18x** the frame. `zoompan` at `z=1.0` shows the
whole image; at `z=1.18` it is a native-resolution crop. Zooming inside that
range never interpolates beyond real pixels. Motion style cycles per scene so no
two consecutive scenes move the same way — a static frame is the single largest
retention loss in a Short.

Easing uses smoothstep (`p*p*(3-2p)`) rather than linear, which is the difference
between "a slideshow" and "a video".

### Captions: per-word events, not ASS karaoke

ASS `\k` only sweeps SecondaryColour to PrimaryColour. It can express "words
already spoken change colour" but not "only the word being spoken is highlighted
and scaled". Emitting one Dialogue event per word-state gives exact control over
colour, scale and outline of the active word — the look every high-retention
Short uses. libass handles thousands of events without complaint.

Fonts are resolved to an explicit **file** in `assets/fonts/` and passed via
`fontsdir`, because libass name lookup through fontconfig behaves differently on
every platform and would silently substitute a font.

### Audio gain staging

Music is normalised to a known **-23 LUFS** before it reaches the mix, so the
mixer can position it with one predictable trim instead of guessing. The voice is
the side-chain key for `sidechaincompress`, so the bed drops under narration and
fills the gaps between lines. The master gets **two-pass** `loudnorm` to
-14 LUFS / -1.5 dBTP — measured, then applied — because single-pass dynamic mode
is materially less accurate.

### Scoring that refuses to lie

`ViralScore` is a weighted sum of observable signals, fully configurable in
`config.yaml`. Recency decays exponentially with a 21-day half-life, which is why
a four-year-old 50M-view video correctly ranks below a three-day-old one.

`PerformanceRatio = views / expected-views-for-channel`, where expected is the
channel's lifetime average per video (or 20% of subscriber count when the channel
hides its totals). That is the only fair way to spot a breakout from public data.

There is deliberately **no** `ctr` field on a researched video. YouTube does not
expose other channels' CTR, impressions or retention at any price. The heuristic
is named `ctr_potential_score` so it cannot be mistaken for a measurement.

### Originality compares angles, not subjects

Every video about black holes shares the words "black holes". Including subject
nouns in the similarity check rejected every legitimate idea during testing, so
topic tokens are stripped before measuring. What remains is the framing — which
is what originality actually means here. A true copy still scores 1.0.

Similarity uses 4-gram Jaccard over content words, against both the researched
corpus (titles and descriptions — no third-party video or transcript is ever
downloaded) and our own previous scripts, which is the anti-spam check.

### Structural gap analysis before the LLM

Twelve angle templates (mechanism, correction, consequence, origin, hidden, ...)
are detected in existing titles by regex. The angles *absent* from a topic
cluster are the gap. This runs with no API and no key, so gap detection never
blocks on a model being available; the LLM then turns a gap into a concept.

Cluster labels are multi-word: a bare keyword produced copy like "The Correction
Behind Black", so the most frequent recurring phrase containing the keyword
becomes the label ("black holes"), with plural variants merged.

### Quality gate measures, it does not assert

Twenty-two checks, each weighted, some **blocking**. Blocking checks veto the
upload regardless of score. They are real measurements: `ffprobe` for
resolution/codec/duration, `volumedetect` for a silent track, `silencedetect` for
dead air, two-pass `loudnorm` for loudness, SRT cue parsing for caption
alignment. Minimum score is configurable (default 80); the blockers are not.

### The learning loop is statistics, and says so

Published performance is grouped by hook type, title type, duration bucket,
publish hour and visual style. Each value gets a weight, shrunk toward the mean by
`n/(n+2)` so one lucky video cannot rewrite the strategy, and a minimum of 3
samples is required before it influences generation. It is reported as "weighted
statistics with small-sample shrinkage", not as machine learning, because that is
what it is.

---

## Android internals

**Room mirrors backend state** so the dashboard, queue and schedule render
instantly and work offline. The backend remains the source of truth for jobs.

**WorkManager, not a foreground service.** Every worker run is short,
network-bound and idempotent. Periodic sync every 15 minutes (the platform
minimum), analytics every 12 hours, plus per-automation triggers. A
`BootReceiver` re-arms after reboot and after app update.

**Secrets in EncryptedSharedPreferences**, master key in the Android Keystore,
excluded from cloud backup and device transfer. A corrupted keystore entry causes
the store to be recreated — you re-enter the values — rather than silently
falling back to plaintext.

**OAuth via AppAuth with PKCE.** No client secret in the APK. The app requests
`access_type=offline` with `prompt=consent` because without both, Google returns
only a short-lived access token and unattended scheduled uploads would stop
working within the hour. The app forwards the refresh token to the backend and
never uploads video itself.

**Charts are Compose Canvas.** Two shapes were needed; a charting dependency was
not.

---

## Extending it

Every provider is a small protocol. To add one, implement the interface and add
it to the order list in `config.yaml`:

| Interface | File | Contract |
|---|---|---|
| LLM | `engine/content/llm.py` | `available()`, `complete()` |
| TTS | `engine/tts/base.py` | `available()`, `synthesize()` -> `SceneAudio` |
| Visual | `engine/visuals/base.py` | `available()`, `fetch()` -> `Asset` |

The fallback chain, retry/backoff and transient-error classification are handled
by the engines, so a new provider only has to do its one job and raise on
failure.
