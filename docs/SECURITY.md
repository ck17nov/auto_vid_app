# SECURITY.md

## What is protected and how

| Secret | Where it lives | Protection |
|---|---|---|
| YouTube API key, LLM keys, stock keys | backend `.env` | gitignored, never logged, never returned by any endpoint |
| YouTube OAuth **refresh token** (backend) | `workspace/secrets/youtube_token.json` | 0600 on POSIX; Windows ACL restricted to your user; outside the normal database |
| YouTube OAuth refresh token (phone) | EncryptedSharedPreferences | AES-256-GCM, master key in the Android Keystore, excluded from backup and device transfer |
| Backend API token | `.env` + phone's encrypted store | compared with `secrets.compare_digest` |
| Your Google password | **nowhere** | never requested, never seen, never stored — OAuth only |

---

## Authentication

**The backend fails closed.** If `AUTOTUBE_API_TOKEN` is unset, every
authenticated endpoint returns 503 with an explanation instead of serving
unauthenticated requests. A backend that can upload to someone's YouTube channel
must not be open by default.

The token is compared with `secrets.compare_digest`, not `==`, so a wrong key
cannot be recovered by timing the response.

`GET /health` is deliberately unauthenticated: it reports capability flags
(is ffmpeg present, which providers are configured, is dry-run on) and no
secrets, so you can check a deployment without holding the key.

---

## Transport

HTTPS everywhere, with one deliberate exception: the Android
`network_security_config.xml` permits cleartext **only** for `localhost`,
`127.0.0.1`, `10.0.2.2` (the emulator's host) and private `192.168.x.x` ranges.
Everything else is blocked by the platform.

If you expose the backend beyond your LAN, put it behind a reverse proxy with a
real certificate. Do not add your public host to the cleartext list.

---

## Input validation

Every request body and query parameter is validated by a pydantic model, not
parsed by hand:

- `niche` — length-bounded string
- `duration_seconds` — 8 to 3600
- `upload_time` — must be `HH:MM` with in-range values
- `days` — 0 to 6 only
- `timezone` — must resolve through `zoneinfo`, so an invalid zone is rejected at
  the boundary rather than throwing deep inside the scheduler
- `video_format`, `mode`, `frequency` — closed enums

## Path traversal

`GET /jobs/{id}/file/{kind}` serves produced artifacts. The path is resolved and
checked for containment inside the workspace before the file is opened, and
`kind` is a fixed allow-list (`video`, `thumbnail`, `subtitle`, `voice`) rather
than a caller-supplied filename. A job cannot be tricked into serving
`../../.env`.

## Rate limiting

A per-IP token bucket, 60 requests/minute by default
(`api.rate_limit_per_minute`). Exceeding it returns 429. This is a guard against
a runaway client or a script loop, not a defence against a determined attacker —
for that, put a real proxy in front.

---

## Logging

Structured, and redacted before anything is written:

- Any key whose **name** matches `api_key`, `token`, `secret`, `password`,
  `client_secret`, `refresh_token` or `authorization` becomes `***REDACTED***`.
- Any **value** shaped like a credential — `AIza...`, `gsk_...`, `sk-...`,
  `ya29....` — is replaced by pattern, so a key pasted into an unexpected field
  is still caught.

Logs go to stdout and optionally to `workspace/logs/*.jsonl`. Grep them before
sharing anyway; that is good practice regardless of what the code promises.

---

## What the code will not do

These are enforced, not just documented:

- Never stores a password.
- Never downloads another creator's video, audio or transcript.
- Never clones a voice. Every TTS voice is the provider's own synthetic voice.
- Never auto-downloads music from a music site.
- Never uploads a video that fails a blocking quality check, regardless of mode.
- Never publishes publicly by default — it takes three separate configuration
  changes (`dry_run: false`, `youtube.upload_enabled: true`,
  `default_privacy: public`).
- Never claims to know another channel's CTR, because no API exposes it.

---

## Threat model, honestly

**In scope and handled:** an unauthenticated caller on your LAN; a leaked log
file; a stolen phone backup; a malformed request; path traversal through the
media endpoint; a runaway client.

**In scope and your responsibility:** exposing the backend to the internet
without TLS; running it on a shared machine where another user can read
`workspace/`; committing `.env`.

**Out of scope:** a compromised host. If an attacker has code execution on the
machine running the backend, they have your tokens. Nothing in an application can
fix that.

---

## If a credential leaks

1. **YouTube OAuth** — revoke it at
   <https://myaccount.google.com/permissions>, then
   `backend.cli auth logout` and re-authorise.
2. **API key** — delete it in the Google Cloud console, create a new one, update
   `.env`, restart.
3. **Backend token** — generate a new one, update `.env` and the app's Settings,
   restart. Anyone holding the old token loses access immediately.
4. **LLM / stock keys** — rotate in the provider console. These are the least
   damaging: worst case, someone spends your free tier.

## Checklist before exposing anything publicly

- [ ] `AUTOTUBE_API_TOKEN` set to a 32+ character random string
- [ ] TLS terminating in front of the backend
- [ ] `.env` not in git (`git check-ignore -v .env` confirms it)
- [ ] `youtube.upload_enabled` still `false` until you have reviewed real output
- [ ] `workspace/secrets/` permissions verified
- [ ] Reviewed one `originality_report.json` and one `quality_report.json`
      yourself, so you know what the automation is actually publishing
