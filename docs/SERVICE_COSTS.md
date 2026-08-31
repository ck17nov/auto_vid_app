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

> **Model IDs expire.** This project originally pinned `gemini-2.0-flash`,
> which Google **shut down on 2026-06-01**; every script request then failed
> with a 404 that read like a broken API key. Both providers now carry a
> fallback list and fall forward automatically when a model ID is retired
> (`_is_model_retired` in `engine/content/llm.py`). Never pin a single ID.

### Groq — RECOMMENDED PRIMARY
| | |
|---|---|
| **FREE?** | **LIMITED** — generous free tier, no credit card required |
| Credit card | **No** |
| Payment after trial | No — the free tier is ongoing, with a separate paid tier for higher limits |
| Commercial use | Yes |

Free-tier limits, per **organisation** (not per key), verified 2026-08:

| Model | Req/min | Req/day | Tokens/min | Tokens/day |
|---|---:|---:|---:|---:|
| `llama-3.3-70b-versatile` (default) | 30 | 1,000 | 12,000 | 100,000 |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 |
| `llama-3.1-8b-instant` | 30 | 14,400 | 6,000 | 500,000 |

You hit whichever limit arrives first, and cached tokens do not count.

**What that means for long videos.** A 20-minute script is ~3,100 words. Written
section by section that is roughly 14 calls and ~25,000 tokens, so the 100,000
tokens/day ceiling on the 70B model allows about **4 long videos/day** — which
happens to match the YouTube upload quota, so it is not the binding limit.

### Google Gemini (AI Studio) — RECOMMENDED FALLBACK, AND BETTER FOR LONG-FORM
| | |
|---|---|
| **FREE?** | **LIMITED** — free tier, no credit card, does not expire |
| Limit | ~1,500 requests/day on the Flash tier, 15 req/min, up to 1M tokens/min |
| Credit card | **No** for AI Studio keys |
| Model used | `gemini-3.7-flash`, falling back through 3.6 / 3.5 / 3.5-lite / 2.5 |
| Data note | Free-tier prompts may be used by Google to improve their products. Do not send anything confidential. |
| Sufficient for production? | Yes, and its token allowance is far more comfortable than Groq's for long-form |

Limits are **per project**, not per key — extra keys in the same project add
nothing. Check yours at <https://aistudio.google.com/rate-limit>.

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
| Runway / Pika / Kling / Veo / Sora (text-to-video) | **NO** — see section 6a | Every "free unlimited AI video" offer is a metered trial. Section 6a has the detail. |
| Stability AI API | **NO** — paid credits | Pollinations covers the same need for free |
| YouTube revenue reporting | Free but gated | Requires monetisation and additional scopes; the dashboard shows Revenue as an explicit placeholder |

---

## 6a. "Free unlimited AI video generation" — what is actually true

Checked again 2026-08. Short answer: **no cloud service gives you unlimited,
watermark-free, commercially-usable AI video for free.** Every free plan is a
metered trial. The common patterns are:

- **Credit-metered free tiers** (Kling, Hailuo, Pika, Luma, Runway): a handful
  of clips, then payment. Clips are typically 5–10 seconds.
- **Watermarks** on free output, which YouTube viewers see and which look like
  someone else's branding on your channel.
- **Non-commercial free tiers**: several providers restrict free output to
  personal use, which a monetised YouTube channel is not.
- **Browser-only free tiers with no API**, so they cannot be automated at all.

**The two things that genuinely are unlimited and free:**

1. **Local rendering — what this project does.** Images plus Ken Burns motion,
   cross-fades and burnt-in captions, composed by FFmpeg on your own machine.
   No service is involved in making the video, so there is no quota and no
   length limit. This is why AutoTube AI can produce a 40-minute video for ₹0
   while a text-to-video API would refuse past 10 seconds.
2. **Self-hosted open-weight video models** (e.g. Mochi 1, Apache 2.0).
   Genuinely unlimited with full commercial rights — but they need a ~24 GB
   GPU, and they generate *seconds* per clip, not minutes. They would slot in
   as another visual provider, not as a replacement for the renderer.

**So what actually limits video length here?** Not a video-generation service,
because there isn't one in the chain:

| Limit | Value | Notes |
|---|---|---|
| Local render | none | bounded only by CPU time |
| YouTube upload, unverified account | **15 minutes** | the real ceiling for most people |
| YouTube upload, verified account | **12 hours / 256 GB** | verify your account to lift the 15-minute cap |
| Scene count | `content.max_scenes` (400) | one scene = one image + one TTS call |
| Script generation | free-tier tokens/day | ~4 long videos/day, see section 2 |
| Music bed | none | synthesised to length, not sampled from a library |

Verifying your YouTube account is the single change that raises the ceiling
most, and it is free: **YouTube Studio → Settings → Channel → Feature
eligibility → Upload videos longer than 15 minutes.**

---

## 7. Bottom line

**Truly free, no credit card, indefinitely:**
YouTube Data + Analytics API, Groq *or* Gemini, edge-tts *or* Piper,
Pixabay/Pexels *or* Pollinations *or* procedural, FFmpeg, everything local.

**What will actually stop you first, in order:**
1. **YouTube's 15-minute cap** if your account is not verified. Free to lift.
2. **YouTube upload quota** — ~4–5 uploads/day. Hard ceiling.
3. **Your own anti-spam limits** — `automation.daily_video_limit` defaults to 3.
4. **LLM free-tier tokens/day** — the first thing you notice on long-form.
5. **Render time** — a few minutes of CPU per 45 s Short, and it scales with
   length. This is why rendering is not on the phone.

**What costs money only if you choose it:**
Better TTS, paid LLMs, real text-to-video, cloud rendering.

**Not a cost but a real risk:** the two unofficial endpoints (edge-tts, gTTS)
can break without notice. Piper is the licence-clean, offline answer, and the
provider chain falls through to it automatically.
