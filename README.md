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

**Duration is enforced against measured audio, and the estimate self-corrects.**
The niche profiles guess a words-per-second rate, and the guess ran ~20% fast: a
45-second request produced 53 seconds of narration, which was then fixed by
speaking 28% faster. Speaking faster is the wrong lever. The pipeline now
records what each voice actually delivers and budgets words against that, so the
script is the right length before a word is spoken. Rate correction is still
there, as the fine adjustment it should have been.

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

**Long videos are built section by section.** A 20-minute script is ~3,100
words, which does not fit in one free-tier LLM response - it gets truncated
mid-JSON. Past `content.section_threshold` scenes the pipeline plans an outline
first, then writes one section per call, each given the headings already covered
so it cannot repeat itself. The section headings become the YouTube chapters.

**Nothing generates video, so nothing caps its length.** Visuals are stills plus
Ken Burns motion composed by FFmpeg on your machine. There is no text-to-video
service in the chain to impose a 10-second clip limit or a monthly credit
budget - see [docs/SERVICE_COSTS.md](docs/SERVICE_COSTS.md#6a-free-unlimited-ai-video-generation--what-is-actually-true).

**Never pin a hosted model ID.** This project shipped with `gemini-2.0-flash`;
Google shut it down on 2026-06-01 and every script request began failing with a
404 that read like a bad API key. Both LLM providers now carry a fallback list
and fall forward when a model is retired.

---

## Video length

| | |
|---|---|
| Rendering | no limit - bounded by CPU time only |
| YouTube, unverified account | **15 minutes.** This is the real ceiling for most people |
| YouTube, verified account | 12 hours / 256 GB |
| Scenes per video | `content.max_scenes`, default 400 (one scene = one image + one TTS call) |
| Script generation | free-tier tokens/day; roughly 4 long videos/day |

Lifting the 15-minute cap is free: **YouTube Studio → Settings → Channel →
Feature eligibility → Upload videos longer than 15 minutes.**

```bash
.venv/Scripts/python -m backend.cli run --niche "history" --length 600 --format LONGFORM --dry-run
```

---

## Status

Verified on this machine (Windows 11, Python 3.12, FFmpeg 9.0, JDK 17, Android SDK 35):

| Area | State |
|---|---|
| Engine: research -> script -> voice -> visuals -> render -> quality | Run end-to-end on Shorts and on a 4-minute long-form video, every artifact verified |
| Python test suite | **377 tests passing** |
| Static analysis | pyflakes clean (bar 3 documented import-probes) |
| Backend API + CLI | 18 endpoints, `doctor` / `run` / `research` / `quota` exercised |
| Android app | **Compiles and packages: 24.0 MB debug APK** (`com.autotube.ai.debug`, minSdk 26, targetSdk 35) |
| YouTube upload + analytics | Implemented against the official APIs; needs your OAuth credentials to exercise for real |

### What an end-to-end run actually produced

Two full dry runs, both with **no LLM key and no image key** — i.e. the
worst-case free path (template scripts, procedural visuals):

| | Science | Kids | Deep ocean | Glaciers | Coral reefs |
|---|---|---|---|---|---|
| Quality | 97.5 | **100** | 97.5 | **100** | **100** |
| 45s target, actual | 40->42.8s | 30->29.7s | 49.3s (+9.6%) | 45.3s (**+0.7%**) | 43.0s (-4.4%) |
| Audio LUFS | -14.02 | -14.01 | -14.06 | -14.11 | -13.99 |
| Rate correction needed | - | - | yes, +28% | yes, +26% | **none** |

The last three are the same 45-second request run in sequence, and they show the
speech-rate calibration converging: the first missed by 9.6% because the word
budget was wrong, the second was rescued by re-synthesising at a faster rate,
and the third budgeted 107 words instead of 130 and landed inside tolerance at a
natural speaking rate with no correction at all.

Both correctly stopped at `AWAITING_APPROVAL` rather than uploading, because
approval mode is the default.

With a Groq key (free) the scripts are written by Llama 3.3 70B instead of the
template builder, which is the single biggest quality difference available.

### And a long-form run

240s, 16:9, `history`. The LLM was **stubbed** (`scripts/longform_e2e.py`) so
the sectioned assembly, TTS, visuals, captions and batched render could be
verified without a key; script *quality* still needs a real model.

| | |
|---|---|
| Script | 12 sections from 1 outline call + 12 section calls, 0 degraded |
| Scenes | 63 (one image and one TTS call each) |
| Chapters | 12, from the outline's real section headings |
| Render | 5 pre-stitch segments, **0 duration-drift warnings** |
| Output | 1920x1080 h264, 147 MB (4.6 Mbps) |
| Audio | -14.00 LUFS / -2.65 dBTP |
| Captions | 161 karaoke groups across 610 words |
| Quality | 93.3/100 |
| Wall clock | **64.7 min for a 4-minute video** (~16x realtime) |
| Visuals | 25 real AI images, 38 procedural, after the rate-limit breaker fired |
| Verdict | **REJECTED** - correctly, because it was the third identical stub run and the self-similarity check caught it |

That rejection is the system working. The duplicate check compares each new
script against your own history, and three identical runs is exactly what it
exists to stop.

### Known limits, stated plainly

- **~4-5 uploads/day** on a default YouTube quota. Hard ceiling, not a bug.
- **YouTube refuses videos over 15 minutes** until your channel is verified.
  Verifying is free and takes a minute; the app warns you above 15 minutes.
- **Long-form needs an LLM key.** The template builder cannot honestly fill
  more than about two minutes of narration, and `doctor` says so up front
  rather than letting you find out after an hour of rendering.
- **Render time is roughly 15x realtime** on a laptop - measured: 64.7 minutes
  for a 4-minute video. This is why rendering is not on the phone.
- **Free image endpoints rate-limit hard at long-form scale.** Pollinations
  refused half the requests on a 63-scene job; a breaker drops a provider once
  it is failing more than half the time, so the job stops paying backoff. A
  free Pexels or Pixabay key avoids this entirely and looks better.
- **edge-tts is an unofficial endpoint.** It broke once during this build (403
  on 7.0.0) and needed a version bump. Piper is the licence-clean fallback.
- **Free AI images cap near 576x1024**, so they are upscaled and look softer
  than stock photography. A free Pixabay/Pexels key fixes this.
- Without an LLM key, scripts are structurally correct but formulaic, and are
  labelled `provider: template` everywhere so this is never hidden.

See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) when something breaks,
and [docs/CONTENT_POLICY.md](docs/CONTENT_POLICY.md) for the rules the system
enforces on itself.
