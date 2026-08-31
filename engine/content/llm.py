"""Pluggable LLM providers, free-first (spec section 29).

  groq     - free tier, no credit card. Llama 3.3 70B. Fast, high quality.
  gemini   - Google AI Studio free tier, no credit card.
  ollama   - fully local, no key, no quota. Needs the ollama daemon running.
  template - deterministic, no network. A DEGRADED FALLBACK, not a mock: it
             produces a real, publishable-shape script from the research data,
             but without an LLM the prose is formulaic. Every script records
             which provider produced it and the quality gate penalises
             `template`, so this can never silently masquerade as AI output.

All providers implement the same `complete()` contract and JSON extraction is
shared, because every one of them occasionally wraps JSON in prose or fences.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from ..core.logging import log_event


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str = ""


class LLMProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult: ...


# --------------------------------------------------------------------------
# JSON extraction - shared, because every provider does this differently
# --------------------------------------------------------------------------
def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an LLM response.

    Handles: bare JSON, ```json fences, prose-wrapped JSON, and trailing
    commas.  Raises LLMError if nothing parseable is present.
    """
    if not text:
        raise LLMError("empty LLM response")
    raw = text.strip()

    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()

    start = raw.find("{")
    if start == -1:
        raise LLMError(f"no JSON object in response: {raw[:200]}")

    # Walk to the matching brace, respecting strings and escapes.
    depth, in_str, esc, end = 0, False, False, -1
    for i, ch in enumerate(raw[start:], start=start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        raise LLMError("unterminated JSON object in response")

    candidate = raw[start:end + 1]
    try:
        return json.loads(candidate)
    except ValueError:
        cleaned = re.sub(r",(\s*[}\]])", r"\1", candidate)      # trailing commas
        cleaned = cleaned.replace("\t", " ")
        try:
            return json.loads(cleaned)
        except ValueError as exc:
            raise LLMError(f"invalid JSON from LLM: {exc}") from exc


# --------------------------------------------------------------------------
# Model retirement
# --------------------------------------------------------------------------
# Hosted providers retire model IDs on their own schedule. This project shipped
# with `gemini-2.0-flash`, which Google shut down on 2026-06-01; every script
# request then failed with a 404 that looked like a broken API key. Treat
# "this model is gone" as a recoverable condition and fall forward to the next
# candidate, but never swallow a rate limit or an auth failure that way.
_MODEL_RETIRED = re.compile(
    r"(model[_ ]not[_ ]found|does not exist|is not found|not_found|"
    r"decommissioned|discontinued|no longer (available|supported)|"
    r"has been (shut down|retired|removed)|unsupported model|"
    r"invalid model|unknown model)", re.I)


# Conditions that clear on their own: rate limits (which are usually per
# minute), and upstream overload. Retrying these beats downgrading to a weaker
# provider. Auth failures and retired models are NOT here - no amount of
# waiting fixes a bad key.
#
# The status codes are word-anchored on purpose. A bare `50[234]` also matches
# inside "15034 tokens" and a bare `429` inside "14290", so an ordinary error
# mentioning a token count would be misread as an upstream outage.
_TRANSIENT_LLM = re.compile(
    r"(\b429\b|rate limit|too many requests|\b50[234]\b|overloaded|"
    r"unavailable|timed? ?out|timeout|temporarily|try again)", re.I)


def _is_transient_llm(message: str) -> bool:
    if re.search(r"\b(401|403)\b|invalid api key|permission denied", message, re.I):
        return False
    if _is_model_retired(message):
        return False
    return bool(_TRANSIENT_LLM.search(message))


def _is_model_retired(message: str) -> bool:
    """True if the error means "that model ID is gone", not "you are throttled"."""
    if re.search(r"\b(429|rate limit|quota|401|403|invalid api key|"
                 r"permission denied)\b", message, re.I):
        return False
    return bool(_MODEL_RETIRED.search(message)) or " 404" in message


# --------------------------------------------------------------------------
# Groq
# --------------------------------------------------------------------------
class GroqProvider:
    name = "groq"
    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
    # Tried in order. Hosted model IDs get decommissioned with little notice,
    # so a single hard-coded ID is a time bomb - see MODEL_RETIREMENT below.
    FALLBACK_MODELS = [
        "llama-3.3-70b-versatile",   # 131k context, best prose of the free set
        "openai/gpt-oss-120b",       # 200k tokens/day on the free tier
        "openai/gpt-oss-20b",
        "llama-3.1-8b-instant",      # 500k tokens/day, weakest prose
    ]

    def __init__(self, api_key: str = "", model: str = "", timeout: int = 120):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.model = model or os.environ.get("GROQ_MODEL", "") or self.FALLBACK_MODELS[0]
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def _candidates(self) -> list[str]:
        return [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult:
        last: LLMError | None = None
        for model in self._candidates():
            try:
                result = self._call(model, prompt, system, json_mode,
                                    temperature, max_tokens)
            except LLMError as exc:
                if not _is_model_retired(str(exc)):
                    raise
                log_event("LLM", "groq model unavailable, trying next",
                          model=model, error=str(exc)[:140])
                last = exc
                continue
            self.model = model          # stick with what worked
            return result
        raise last or LLMError("no usable groq model")

    def _call(self, model: str, prompt: str, system: str, json_mode: bool,
              temperature: float, max_tokens: int) -> LLMResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": ([{"role": "system", "content": system}] if system else [])
                        + [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.ENDPOINT, json=payload, headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"})
            if resp.status_code == 429:
                raise LLMError("groq rate limit reached (free tier)")
            if resp.status_code >= 400:
                raise LLMError(f"groq {resp.status_code}: {resp.text[:220]}")
            data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected groq response shape: {exc}") from exc
        return LLMResult(text=text, provider=self.name, model=model)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
class GeminiProvider:
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"
    # Google shut down gemini-2.0-flash on 2026-06-01. It was this project's
    # original default, and the pipeline died with a 404 until the list below
    # was introduced: never pin a single hosted model ID.
    FALLBACK_MODELS = [
        "gemini-3.7-flash",        # current stable Flash
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",   # cheapest/highest-volume
        "gemini-2.5-flash",
    ]

    def __init__(self, api_key: str = "", model: str = "", timeout: int = 120):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "") or self.FALLBACK_MODELS[0]
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def _candidates(self) -> list[str]:
        return [self.model] + [m for m in self.FALLBACK_MODELS if m != self.model]

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult:
        last: LLMError | None = None
        for model in self._candidates():
            try:
                result = self._call(model, prompt, system, json_mode,
                                    temperature, max_tokens)
            except LLMError as exc:
                if not _is_model_retired(str(exc)):
                    raise
                log_event("LLM", "gemini model unavailable, trying next",
                          model=model, error=str(exc)[:140])
                last = exc
                continue
            self.model = model          # stick with what worked
            return result
        raise last or LLMError("no usable gemini model")

    def _call(self, model: str, prompt: str, system: str, json_mode: bool,
              temperature: float, max_tokens: int) -> LLMResult:
        # Gemini 2.5+ and the 3.x family spend output tokens on internal
        # reasoning BEFORE emitting any text, and that spend counts against
        # maxOutputTokens. Asking for 4096 on a script call produced exactly
        # what you would expect once you know that: 2,169 characters of
        # truncated JSON on one attempt and a candidate with zero parts on the
        # next, after which the pipeline silently fell back to the template
        # builder. The caller's budget is therefore treated as "text I want",
        # not "total allowance", and thinking is capped so it cannot eat the lot.
        budget = max(int(max_tokens) * 3, 8192)
        gen: dict[str, Any] = {"temperature": temperature,
                               "maxOutputTokens": budget}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        gen["thinkingConfig"] = {"thinkingBudget": min(2048, budget // 4)}
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self.BASE}/{model}:generateContent"

        data = self._post(url, payload)
        if data is None:
            # Some models reject thinkingConfig outright. Retry without it
            # rather than losing the provider over an optional field.
            gen.pop("thinkingConfig", None)
            log_event("LLM", "gemini rejected thinkingConfig, retrying without",
                      model=model)
            data = self._post(url, payload, allow_retry=False)

        candidates = data.get("candidates") or []
        if not candidates:
            feedback = data.get("promptFeedback") or {}
            raise LLMError(f"gemini returned no candidates "
                           f"(promptFeedback={str(feedback)[:160]})")
        candidate = candidates[0]
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        if not text:
            # Surface finishReason and the token split. Without this the
            # failure reads as "empty LLM response" and tells you nothing
            # about which budget you actually exhausted.
            usage = data.get("usageMetadata") or {}
            raise LLMError(
                f"gemini produced no text (finishReason="
                f"{candidate.get('finishReason')}, "
                f"thoughts={usage.get('thoughtsTokenCount')}, "
                f"output={usage.get('candidatesTokenCount')}, "
                f"budget={budget}) - raise max_tokens or lower thinkingBudget")
        return LLMResult(text=text, provider=self.name, model=model)

    def _post(self, url: str, payload: dict[str, Any],
              allow_retry: bool = True) -> dict[str, Any] | None:
        """POST once. Returns None if `thinkingConfig` was the problem."""
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload,
                               headers={"x-goog-api-key": self.api_key,
                                        "Content-Type": "application/json"})
            if resp.status_code == 429:
                raise LLMError("gemini rate limit reached (free tier)")
            if resp.status_code >= 400:
                body = resp.text[:400]
                if (allow_retry and resp.status_code == 400
                        and "thinking" in body.lower()):
                    return None
                raise LLMError(f"gemini {resp.status_code}: {body[:220]}")
            return resp.json()


# --------------------------------------------------------------------------
# Ollama (local)
# --------------------------------------------------------------------------
class OllamaProvider:
    """Local inference. Free and unlimited, but CPU-only hosts are SLOW:
    expect minutes per call for a 7B model without a GPU. Prefer groq/gemini
    for interactive use and keep ollama as the offline fallback.
    """

    name = "ollama"

    # 900s was the original default and it made the pipeline indistinguishable
    # from a hang: a CPU-only 7B model can sit there for a quarter of an hour
    # with nothing on stdout. 300s still allows a slow-but-working local model
    # while failing over to the template builder in a bearable time. Raise it
    # with OLLAMA_TIMEOUT if you have a GPU and want the headroom.
    DEFAULT_TIMEOUT = 300

    def __init__(self, host: str = "", model: str = "", timeout: int = 0):
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
                     ).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
        self.timeout = timeout or int(
            os.environ.get("OLLAMA_TIMEOUT", self.DEFAULT_TIMEOUT))
        self._probe: tuple[float, bool] | None = None

    def available(self) -> bool:
        # A daemon busy generating a previous request answers /api/tags slowly.
        # A short timeout therefore reports a working provider as unavailable,
        # which silently downgrades the pipeline to the template builder - so
        # allow real time here and cache the answer briefly.
        import time as _time
        if self._probe and _time.time() - self._probe[0] < 30.0:
            return self._probe[1]
        ok = False
        try:
            with httpx.Client(timeout=12.0) as client:
                resp = client.get(f"{self.host}/api/tags")
                ok = resp.status_code == 200
        except Exception:
            ok = False
        self._probe = (_time.time(), ok)
        return ok

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model, "prompt": prompt, "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if system:
            payload["system"] = system
        if json_mode:
            payload["format"] = "json"
        # Say so before blocking. Local inference on CPU can take minutes, and
        # a silent wait is the single most common "it hung" report.
        log_event("LLM", "waiting on local ollama (can take minutes on CPU)",
                  model=self.model, timeout=f"{self.timeout}s")
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.host}/api/generate", json=payload)
            if resp.status_code >= 400:
                raise LLMError(f"ollama {resp.status_code}: {resp.text[:220]}")
            data = resp.json()
        text = data.get("response", "")
        if not text:
            raise LLMError("ollama returned no text")
        return LLMResult(text=text, provider=self.name, model=self.model)


# --------------------------------------------------------------------------
# Deterministic fallback
# --------------------------------------------------------------------------
class TemplateProvider:
    """No-network fallback.  Signals its own limitation instead of pretending.

    It does not attempt to answer arbitrary prompts: the callers that need a
    guaranteed result (idea generation, script generation) have dedicated
    deterministic builders.  For anything else it raises, so a caller never
    receives plausible-looking nonsense.
    """

    name = "template"
    DEGRADED = True

    def available(self) -> bool:
        return True

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult:
        raise LLMError(
            "template provider cannot answer free-form prompts; "
            "the caller must use its deterministic builder instead")


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
class LLMRouter:
    """Tries providers in configured order; reports which one answered."""

    def __init__(self, order: list[str], cfg=None):
        self.cfg = cfg
        self.providers: list[LLMProvider] = []
        registry = {
            "groq": lambda: GroqProvider(
                (cfg.secret("GROQ_API_KEY") if cfg else ""),
                (cfg.secret("GROQ_MODEL") if cfg else "")),
            "gemini": lambda: GeminiProvider(
                (cfg.secret("GEMINI_API_KEY") if cfg else ""),
                (cfg.secret("GEMINI_MODEL") if cfg else "")),
            "ollama": lambda: OllamaProvider(
                (cfg.secret("OLLAMA_HOST") if cfg else ""),
                (cfg.secret("OLLAMA_MODEL") if cfg else "")),
            "template": TemplateProvider,
        }
        for name in order:
            factory = registry.get(name)
            if factory:
                self.providers.append(factory())
        # Free-tier limits reset per minute, so the default waits are long
        # enough to actually clear one.
        self.transient_retries = int(
            cfg.get("content.transient_retries", 3) if cfg else 3)
        self.retry_backoff = float(
            cfg.get("content.retry_backoff_seconds", 25) if cfg else 25)

    @property
    def usable(self) -> list[LLMProvider]:
        return [p for p in self.providers
                if not isinstance(p, TemplateProvider) and p.available()]

    def has_real_llm(self) -> bool:
        return bool(self.usable)

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult:
        errors: list[str] = []
        for provider in self.providers:
            if isinstance(provider, TemplateProvider):
                continue
            if not provider.available():
                errors.append(f"{provider.name}: not configured")
                continue
            try:
                result = self._with_backoff(
                    provider, prompt, system, json_mode, temperature, max_tokens)
            except Exception as exc:
                errors.append(f"{provider.name}: {str(exc)[:180]}")
                log_event("LLM", "provider failed, falling back",
                          provider=provider.name, error=str(exc)[:180])
                continue
            log_event("LLM", "completion ok", provider=provider.name,
                      model=result.model, chars=len(result.text))
            return result
        raise LLMError("no LLM provider succeeded -> " + " | ".join(errors))

    def _with_backoff(self, provider, prompt: str, system: str, json_mode: bool,
                      temperature: float, max_tokens: int) -> LLMResult:
        """Retry ONE provider through transient conditions before giving up.

        Free-tier limits are mostly per MINUTE, so falling straight through to
        the next provider is the wrong move: waiting 30 seconds gets you the
        good model, while moving on gets you a weaker one - or, at the end of
        the chain, CPU-only ollama at minutes per call.

        Seen against real keys in one run: Groq's gpt-oss-120b allows 8,000
        tokens/minute and a single ideas call spent it, then Gemini answered
        503 (transient overload). Both were treated as permanent failures and
        the pipeline degraded all the way to local inference. Neither had to.

        Only transient conditions are retried. A bad key or a retired model
        fails immediately, because waiting cannot fix either.
        """
        attempts = max(1, self.transient_retries)
        delay = self.retry_backoff
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return provider.complete(
                    prompt, system=system, json_mode=json_mode,
                    temperature=temperature, max_tokens=max_tokens)
            except Exception as exc:
                last = exc
                if attempt >= attempts or not _is_transient_llm(str(exc)):
                    raise
                log_event("LLM", "transient, waiting rather than downgrading",
                          provider=provider.name, attempt=f"{attempt}/{attempts}",
                          wait=f"{delay:.0f}s", error=str(exc)[:120])
                time.sleep(delay)
                delay *= 2
        raise last or LLMError(f"{provider.name} failed")

    def complete_json(self, prompt: str, *, system: str = "",
                      temperature: float = 0.8, max_tokens: int = 4096,
                      attempts: int = 2) -> tuple[dict[str, Any], str]:
        """Completion that must yield JSON. Retries once with a stricter nudge."""
        last: Exception | None = None
        for i in range(attempts):
            nudge = ("" if i == 0 else
                     "\n\nYour previous reply was not valid JSON. "
                     "Reply with ONE JSON object and nothing else.")
            try:
                result = self.complete(prompt + nudge, system=system,
                                       json_mode=True, temperature=temperature,
                                       max_tokens=max_tokens)
                return extract_json(result.text), result.provider
            except LLMError as exc:
                last = exc
                log_event("LLM", "JSON parse failed", attempt=i + 1,
                          error=str(exc)[:160])
        raise LLMError(f"could not obtain JSON from any provider: {last}")
