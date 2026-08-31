# AutoTube AI

Automated YouTube research, production and publishing, driven from an Android
app on a Samsung Galaxy M34 5G.

The phone is the **control centre**, not the renderer: it collects settings,
queues jobs, shows progress, and approves uploads. All heavy work (LLM, TTS,
image generation, FFmpeg rendering) happens in a Python backend you run on your
own machine or a free-tier box.

**Target cost: Rs 0/month.** The binding limit is not money, it is YouTube's API
quota — about **5 uploads/day** on a default project. See
[docs/SERVICE_COSTS.md](docs/SERVICE_COSTS.md) for every service, its real free
tier, and what it cannot do.

---

## What it actually does

```
niche + length + language + upload time
        |
        v
 research (YouTube Data API)      -> recent, fast-growing, breakout videos
        v
 viral + gap analysis             -> which angle nobody has covered
        v
 original concept                 -> scored, de-duplicated against your history
        v
 script                           -> word-budgeted, banned openers stripped
        v
 retention pass                   -> scored, then auto-improved
        v
 fact check                       -> claims graded, risky ones flagged
        v
 voice (per scene, exact timings) -> re-fitted to hit the target duration
        v
 visuals (one image per scene)    -> stock photo / AI / procedural fallback
        v
 captions (karaoke ASS + SRT)     -> word-accurate, inside mobile safe areas
        v
 music + SFX (synthesised)        -> side-chain ducked under the voice
        v
 render (FFmpeg, one lossy pass)  -> 1080x1920, -14 LUFS, Ken Burns motion
        v
 thumbnail (3 variants, scored)
        v
 quality gate (0-100, hard blocks) -> never uploads a broken video
        v
 approve  ->  upload / schedule (YouTube publishAt, your timezone)
        v
 analytics -> learned strategy -> better next video
```

---

## Quick start

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Add a `YOUTUBE_API_KEY` (free, no credit card) and at least one LLM key, then:

```bash
.venv/Scripts/python -m backend.cli doctor
```

`doctor` tells you exactly what is missing and what still works without it.

Try the whole pipeline without touching YouTube:

```bash
.venv/Scripts/python -m backend.cli run --niche "science" --length 45 --dry-run
```

That writes a complete job folder: `research.json`, `idea.json`, `script.json`,
`assets/`, `voice.wav`, `captions.ass`, `captions.srt`, `music.wav`, `sfx.wav`,
`master.wav`, `video.mp4`, `thumbnails/`, `metadata.json`,
`originality_report.json`, `factcheck_report.json`, `quality_report.json`.

Then start the backend the app talks to:

```bash
.venv/Scripts/python -m backend.cli serve --host 0.0.0.0 --port 8099
```

Full instructions: [docs/SETUP.md](docs/SETUP.md).
Android build: [docs/SETUP.md](docs/SETUP.md#android-app).
YouTube credentials: [docs/YOUTUBE_SETUP.md](docs/YOUTUBE_SETUP.md).

---

## Layout

```
/android      Kotlin + Jetpack Compose app (8 screens, Room, WorkManager)
/backend      FastAPI HTTP API + CLI
/engine       the pipeline
  /research   YouTube Data API, viral scoring, clustering, content gaps
  /content    LLM providers, ideas, scripts, metadata, retention, originality
  /tts        edge-tts / Piper / gTTS, per-scene synthesis with word timings
  /visuals    Pixabay / Pexels / Pollinations / procedural generator
  /video      FFmpeg composition, animated captions, synthesised music + SFX
  /thumbnail  Pillow thumbnail variants + scoring
  /quality    the pre-publish gate
  /youtube    OAuth 2.0, resumable upload, scheduling
  /analytics  own-channel analytics + the learning loop
/tests        pytest suite
/docs         setup, costs, security, content policy, troubleshooting
/assets       fonts (Anton, OFL), your music library, Piper voices
/workspace    job output, database, logs (gitignored)
```

---

## Design decisions worth knowing

**Per-scene TTS, not one long clip.** Each scene is synthesised separately, so
the visual cut lands exactly on the narration boundary instead of being guessed
from a words-per-minute estimate.

**edge-tts is the default because it returns per-word timings.** That is what
makes the karaoke captions frame-accurate. Piper is the licence-clean offline
fallback; its captions are estimated from word length and punctuation.

**Duration is enforced against measured audio.** If the synthesised narration
misses the target, the speaking rate is recomputed and it is re-synthesised. A
"45 second" video that runs 78 seconds is a failed video.

**Two-pass render, exactly one lossy encode.** Per-scene motion clips first
(parallel, near-lossless), then a single pass that cross-fades, burns captions,
mixes audio and encodes. Cross-fades are padded so total video length equals
total audio length exactly.

**Originality compares angles, not subjects.** Every black-hole video shares the
words "black holes"; that is the topic, not plagiarism. Topic tokens are
stripped before similarity is measured.

**Competitor CTR is never claimed.** YouTube does not expose it. The research
engine reports a clearly-named `ctr_potential_score` heuristic instead. Real CTR
appears only for your own authenticated channel.

**Approval mode is the default.** Auto mode is opt-in, and the quality gate's
hard blockers apply in both.

---

## Status

Verified on this machine (Windows 11, Python 3.12, FFmpeg 9.0, JDK 17, Android SDK 35):

| Area | State |
|---|---|
| Engine: research -> script -> voice -> visuals -> render -> quality | Run end-to-end, twice, with every artifact verified |
| Python test suite | **303 tests passing** |
| Static analysis | pyflakes clean (bar 3 documented import-probes) |
| Backend API + CLI | 18 endpoints, `doctor` / `run` / `research` / `quota` exercised |
| Android app | **Compiles and packages: 23.8 MB debug APK** (`com.autotube.ai.debug`, minSdk 26, targetSdk 35) |
| YouTube upload + analytics | Implemented against the official APIs; needs your OAuth credentials to exercise for real |

### What an end-to-end run actually produced

Two full dry runs, both with **no LLM key and no image key** — i.e. the
worst-case free path (template scripts, procedural visuals):

| | Science Short | Kids Short |
|---|---|---|
| Quality score | 97.5/100, 0 blockers | **100/100, 0 blockers, 0 warnings** |
| Target vs actual duration | 40s -> 42.8s (7% off) | 30s -> 29.7s (**1% off**) |
| Audio master | -14.02 LUFS / -1.73 dBTP | -14.01 LUFS / -4.65 dBTP |
| Style template | SCIENCE_EXPLAINER, karaoke captions | KIDS_STORY, block captions |
| Originality | passed, 0.00 similarity to research | passed |
| Artifacts | all 16 required + thumbnails | all 16 required + thumbnails |

Both correctly stopped at `AWAITING_APPROVAL` rather than uploading, because
approval mode is the default.

With a Groq key (free) the scripts are written by Llama 3.3 70B instead of the
template builder, which is the single biggest quality difference available.

### Known limits, stated plainly

- **~4-5 uploads/day** on a default YouTube quota. Hard ceiling, not a bug.
- **Render time** is a few minutes of CPU per 45s Short, and much worse under
  contention. This is why rendering is not on the phone.
- **edge-tts is an unofficial endpoint.** It broke once during this build (403
  on 7.0.0) and needed a version bump. Piper is the licence-clean fallback.
- **Free AI images cap near 576x1024**, so they are upscaled and look softer
  than stock photography. A free Pixabay/Pexels key fixes this.
- Without an LLM key, scripts are structurally correct but formulaic, and are
  labelled `provider: template` everywhere so this is never hidden.

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) when something breaks,
and [docs/CONTENT_POLICY.md](docs/CONTENT_POLICY.md) for the rules the system
enforces on itself.
