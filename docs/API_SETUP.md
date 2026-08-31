# API_SETUP.md

Which keys to get, in priority order. All free; none require a credit card.
Cost detail and honest limitations: [SERVICE_COSTS.md](SERVICE_COSTS.md).

---

## Required

### 1. YouTube (research + upload)

See [YOUTUBE_SETUP.md](YOUTUBE_SETUP.md). Research cannot run without the API
key — it is the one hard dependency besides FFmpeg.

---

## Strongly recommended

### 2. Groq — best free script quality

1. <https://console.groq.com/keys> — sign in, create an API key.
2. In `.env`:

   ```
   GROQ_API_KEY=gsk_...
   GROQ_MODEL=llama-3.3-70b-versatile
   ```

No credit card. The free tier has per-minute and per-day limits that are ample
for a few videos a day. If a limit is hit, the router falls through to the next
provider automatically and logs which one answered.

### 3. Google Gemini — fallback

1. <https://aistudio.google.com/apikey> — create an API key.
2. In `.env`:

   ```
   GEMINI_API_KEY=AIza...
   GEMINI_MODEL=gemini-2.0-flash
   ```

No credit card for AI Studio keys. Note that free-tier prompts may be used by
Google to improve their products, so do not send anything confidential.

---

## Optional, with a real quality gain

### 4. Pixabay and/or Pexels — sharper visuals

- Pixabay: <https://pixabay.com/api/docs/> (your key is shown on that page once
  you are signed in) -> `PIXABAY_API_KEY=`
- Pexels: <https://www.pexels.com/api/new/> -> `PEXELS_API_KEY=`

Both licences permit commercial use, including monetised YouTube, with no
attribution required (the pipeline records attribution anyway, in
`asset_manifest.json`).

Why this matters: real photographs arrive at 3000-6000 px and stay sharp after
being cropped to 1080x1920 and panned. Free AI generation caps near 576x1024, so
it has to be upscaled about 2x and looks noticeably soft. When a stock key is
present, stock is tried first.

### 5. Ollama — local, unlimited, no key

```bash
ollama pull llama3.1:8b
```

```
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=llama3.1:8b
```

**Reality check:** on a CPU-only machine this is impractically slow. Measured
during development on this project: a 7B model took over 10 minutes for one
script call and then timed out. With a GPU it is fast and genuinely free. Keep it
as the offline fallback, not the primary.

---

## Backend auth (not a third party)

```
AUTOTUBE_API_TOKEN=<long random string>
```

Generate one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The backend **refuses** to serve authenticated endpoints when this is unset — an
open backend that can upload to your YouTube channel is not an acceptable
default. The Android app sends it in the `X-API-Key` header.

---

## What works with no keys at all

| Stage | Behaviour without any key |
|---|---|
| Research | **blocked** — requires `YOUTUBE_API_KEY` |
| Ideas | structural gap analysis, no LLM needed |
| Script | deterministic template builder, recorded as `provider: template` |
| Voice | edge-tts — no key, no account |
| Visuals | procedural generator — always available |
| Video, captions, music, SFX | FFmpeg, entirely local |
| Thumbnail | Pillow, local |
| Quality gate | fully functional |
| Upload | requires OAuth |

The template script path is a genuine degraded mode, not a mock. It is recorded
on the script, flagged by the fact checker, and penalised by the quality gate, so
it can never quietly pass as AI-written output. For real prose quality, set at
least one LLM key.

---

## Key hygiene

- Keys live in `.env`, which is gitignored. Never commit it.
- `.env.example` is the template and contains no real values.
- Logs are redacted: anything matching an `api_key` / `token` / `secret` style
  key name, and anything shaped like `AIza...`, `gsk_...`, `sk-...` or `ya29....`,
  is replaced with `***REDACTED***` before it is written.
- On the phone, secrets are held in EncryptedSharedPreferences (Android
  Keystore), excluded from cloud backup and device transfer.
- Rotate a key by editing `.env` and restarting the backend; nothing caches it.
