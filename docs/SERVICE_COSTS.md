# SERVICE_COSTS.md

Every external service this project can use, with an honest answer to
"is it free?" (spec sections 3 and 41).

**Summary: the system runs at Rs 0/month for normal low-volume use.**
The binding constraint is not money — it is the **YouTube API quota**, which
caps you at about **5 uploads/day** on a default Google Cloud project.

Prices verified against provider documentation at the time of writing. Free
tiers change; re-check before relying on one for production.

---

## 1. Required services

### YouTube Data API v3 — research, upload, thumbnails, captions
| | |
|---|---|
| **FREE?** | **YES** — permanently free, no billing account, no credit card |
| Limit | 10,000 quota units per day, per Google Cloud project |
| Auth | API key (read-only research) + OAuth 2.0 client (upload/analytics) |
| Commercial YouTube use | Yes — this is YouTube's own official API |
| Sufficient for production? | **For low volume, yes. See the quota maths below.** |

**Quota costs (from the official documentation):**

| Operation | Units | Notes |
|---|---:|---|
| `search.list` | 100 | the expensive one |
| `videos.list` | 1 | up to 50 IDs per call |
| `channels.list` | 1 | up to 50 IDs per call |
| `videos.insert` (upload) | **1,600** | |
| `thumbnails.set` | 50 | |
| `captions.insert` | 400 | |

**What that actually means:**

- One research run = 3 searches + hydration ≈ **302 units**.
- One published video with thumbnail + captions = 1600 + 50 + 400 = **2,050 units**.
- 10,000 units/day ⇒ **~4 fully-featured uploads/day**, or 6 uploads with no
  thumbnail/captions, *including* research.

The code enforces this rather than discovering it at runtime: `QuotaGuard`
tracks spend against the midnight-US-Pacific reset and **reserves**
`daily_video_limit × 1600` units so research can never starve publishing.
Check it any time with `autotube quota`.

You can request a quota increase from Google (free, but requires an audit and
is not guaranteed). Do not design around getting one.

### YouTube Analytics API v2 — own-channel performance
| | |
|---|---|
| **FREE?** | **YES** |
| Limit | shares the same 10,000-unit project quota; report calls are cheap |
| Auth | OAuth 2.0 (`yt-analytics.readonly`) |
| Important | Returns data for **your own authenticated channel only.** Competitor CTR, impressions and retention are **not available from any API**, at any price. The app never claims otherwise — see `ctr_potential_score`. |

---

## 2. LLM (script + idea generation) — pick at least one

### Groq — RECOMMENDED PRIMARY
| | |
|---|---|
| **FREE?** | **LIMITED** — generous free tier, no credit card required |
| Limit | per-minute and per-day request/token limits that vary by model; sufficient for a handful of videos a day |
| Credit card | **No** |
| Payment after trial | No — the free tier is ongoing, with a separate paid tier if you want higher limits |
| Model used | `llama-3.3-70b-versatile` |
| Commercial use | Yes |
| Sufficient for production? | Yes at 1–5 videos/day. Not for bulk generation. |

### Google Gemini (AI Studio) — RECOMMENDED FALLBACK
| | |
|---|---|
| **FREE?** | **LIMITED** — free tier, no credit card |
| Limit | per-minute/per-day request limits; `gemini-2.0-flash` free tier is ample here |
| Credit card | **No** for AI Studio keys |
| Data note | Free-tier prompts may be used by Google to improve their products. Do not send anything confidential. |
| Sufficient for production? | Yes at low volume |

### Ollama (local) — OFFLINE FALLBACK
| | |
|---|---|
| **FREE?** | **YES** — fully local, no key, no quota, unlimited |
| Real cost | Your hardware. **Measured on this project's dev machine: a CPU-only 7B model took over 10 minutes for a single script call and timed out.** With a GPU it is fast; without one it is impractical for interactive use. |
| Sufficient for production? | Only with a GPU |

### Template builder — LAST RESORT, NO NETWORK
Built in, always available, zero cost. It produces a real, structured script
from the gap analysis, but the prose is formulaic. It is **not a mock**: it is
recorded as `provider: "template"`, the fact-checker flags it, and the quality
gate penalises it, so it can never silently pass as AI-written output.

---

## 3. Text-to-speech

### edge-tts — PRIMARY
| | |
|---|---|
| **FREE?** | **YES** — no key, no account, no quota published |
| How | Uses the same public endpoint as Microsoft Edge's "Read aloud" feature |
| Voices | 300+ across many languages, including `en-IN-NeerjaNeural` (Indian English) |
| Why it is the primary | It returns **exact per-word timings**, which is what makes frame-accurate karaoke captions possible. Nothing else free does this. |
| **Risk — be aware** | This is an **unofficial** use of a consumer endpoint. It is not a supported API, there is no SLA, and Microsoft has broken it before: version 7.0.0 of the client returned HTTP 403 during this build and had to be upgraded to 7.2.8. **Pin the version, expect occasional breakage, and keep Piper installed as a fallback.** |
| Commercial use | Not covered by a commercial licence agreement. If you monetise seriously, move to Piper (below) or a paid TTS. |

### Piper — OFFLINE FALLBACK, SAFEST LICENCE
| | |
|---|---|
| **FREE?** | **YES** — open source (MIT), runs locally, unlimited |
| Setup | `pip install piper-tts` + download a voice `.onnx` into `assets/piper/` |
| Quality | Good neural TTS; below edge-tts prosody |
| Timing | No word boundaries — captions fall back to length-and-punctuation weighted estimation, which is good but not exact |
| Commercial use | **Yes, unambiguously.** This is the recommended choice for a monetised channel. |

### gTTS — LAST RESORT
Free, no key, unofficial Google Translate endpoint. Flattest prosody of the
three. Same unofficial-endpoint caveat as edge-tts.

### Android native TTS
Free, on-device, no network. Used by the phone for local playback only, not
for production audio.

---

## 4. Visuals

### Pixabay / Pexels — RECOMMENDED FOR PHOTOREAL
| | Pixabay | Pexels |
|---|---|---|
| **FREE?** | **YES** (free API key) | **YES** (free API key) |
| Credit card | No | No |
| Rate limit | ~100 requests / 60s | 200/hour, 20,000/month |
| Resolution | up to several thousand px | up to original camera resolution |
| Commercial use incl. monetised YouTube | **Yes**, no attribution required | **Yes**, no attribution required |
| Why first in the chain | Real photographs at 3000–6000 px stay genuinely sharp after cropping to 1080×1920 and holding up under a Ken Burns zoom |

Attribution is still recorded in `asset_manifest.json` even though it is not
required — it makes the originality report complete.

### Pollinations.ai — AI GENERATION, NO KEY
| | |
|---|---|
| **FREE?** | **YES** — no key, no account |
| **Measured limitations (verified during this build)** | Caps output at about **576×1024** regardless of the requested size; responses took 8–60 s; concurrent requests returned **HTTP 429**. |
| Consequence | A 576×1024 source upscaled to 1080×1920 is soft. The code compensates with a Lanczos + unsharp-mask chain and limits concurrency to 2 with retry/backoff, but stock photos are visibly sharper. |
| Sufficient for production? | Usable for concepts stock libraries cannot cover. Not a reliable primary. |
| Commercial use | Generated images; no third-party rights asserted. Verify current terms yourself before monetising. |

### Procedural generator (built in) — ALWAYS WORKS
Zero cost, zero key, zero network, no licence risk, sharp at any resolution.
Renders a designed abstract frame: palette derived from scene keywords,
additive light sources, particle/ray texture, geometric structure, film grain,
vignette. Roughly 2.5 s per 1274×2265 image. This is the reason the pipeline
can never fail for lack of an image.

---

## 5. Audio, video and everything local

| Component | Cost | Notes |
|---|---|---|
| **FFmpeg** | Free (GPL/LGPL) | Does all rendering, mixing, caption burn-in, loudness normalisation. The single most important dependency. |
| **Music** | Free | Procedurally synthesised ambient beds (ffmpeg oscillators + noise + reverb), or your own public-domain/licensed files dropped into `assets/music/`. **Nothing is ever auto-downloaded from a music site.** |
| **Sound effects** | Free | Synthesised from filtered noise envelopes. No third-party samples. |
| **Thumbnails** | Free | Pillow, local. |
| **Fonts** | Free | Anton (SIL Open Font License 1.1), auto-downloaded once to `assets/fonts/`. Redistributable, commercial use allowed. Falls back to a system font. |
| **Database** | Free | SQLite (backend) + Room (Android). |
| **Backend hosting** | Free | Runs on your own PC. Any free-tier VM works, but rendering is CPU-heavy — a 512 MB free container will struggle. |

---

## 6. Optional / not used by default

| Service | Free? | Why it is not the default |
|---|---|---|
| OpenAI / Anthropic APIs | **NO** — paid, credit card required | Groq and Gemini free tiers cover this workload |
| ElevenLabs TTS | **LIMITED** — small monthly character quota, then paid | Free quota is exhausted in a few videos; credit card needed for anything real |
| Runway / Pika / Sora (text-to-video) | **NO** — paid, and expensive | Spec section 42 is explicit: do not assume free cinematic AI video exists. It does not. The image + motion + caption approach here looks good and costs nothing. |
| Stability AI API | **NO** — paid credits | Pollinations covers the same need for free |
| YouTube revenue reporting | Free but gated | Requires monetisation and additional scopes; the dashboard shows Revenue as an explicit placeholder |

---

## 7. Bottom line

**Truly free, no credit card, indefinitely:**
YouTube Data + Analytics API, Groq *or* Gemini, edge-tts *or* Piper,
Pixabay/Pexels *or* Pollinations *or* procedural, FFmpeg, everything local.

**What will actually stop you first, in order:**
1. **YouTube upload quota** — ~4–5 uploads/day. Hard ceiling.
2. **Your own anti-spam limits** — `automation.daily_video_limit` defaults to 3.
3. **LLM free-tier rate limits** — only if you generate in bursts.
4. **Render time** — a 45 s Short takes a few minutes of CPU on a laptop.

**What costs money only if you choose it:**
Better TTS, paid LLMs, real text-to-video, cloud rendering.

**Not a cost but a real risk:** the two unofficial endpoints (edge-tts, gTTS)
can break without notice. Piper is the licence-clean, offline answer, and the
provider chain falls through to it automatically.
