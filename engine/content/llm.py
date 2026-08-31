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
# Groq
# --------------------------------------------------------------------------
class GroqProvider:
    name = "groq"
    ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str = "", model: str = "", timeout: int = 120):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.model = model or os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
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
        return LLMResult(text=text, provider=self.name, model=self.model)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
class GeminiProvider:
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str = "", model: str = "", timeout: int = 120):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def complete(self, prompt: str, *, system: str = "", json_mode: bool = False,
                 temperature: float = 0.8, max_tokens: int = 4096) -> LLMResult:
        gen: dict[str, Any] = {"temperature": temperature,
                               "maxOutputTokens": max_tokens}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self.BASE}/{self.model}:generateContent"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json=payload,
                               headers={"x-goog-api-key": self.api_key,
                                        "Content-Type": "application/json"})
            if resp.status_code == 429:
                raise LLMError("gemini rate limit reached (free tier)")
            if resp.status_code >= 400:
                raise LLMError(f"gemini {resp.status_code}: {resp.text[:220]}")
            data = resp.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError) as exc:
            reason = str(data)[:200]
            raise LLMError(f"unexpected gemini response ({exc}): {reason}") from exc
        return LLMResult(text=text, provider=self.name, model=self.model)


# --------------------------------------------------------------------------
# Ollama (local)
# --------------------------------------------------------------------------
class OllamaProvider:
    """Local inference. Free and unlimited, but CPU-only hosts are SLOW:
    expect minutes per call for a 7B model without a GPU. Prefer groq/gemini
    for interactive use and keep ollama as the offline fallback.
    """

    name = "ollama"

    def __init__(self, host: str = "", model: str = "", timeout: int = 900):
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
                     ).rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
        self.timeout = timeout
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
            try:
                if not provider.available():
                    errors.append(f"{provider.name}: not configured")
                    continue
                result = provider.complete(
                    prompt, system=system, json_mode=json_mode,
                    temperature=temperature, max_tokens=max_tokens)
                log_event("LLM", "completion ok", provider=provider.name,
                          model=result.model, chars=len(result.text))
                return result
            except Exception as exc:
                errors.append(f"{provider.name}: {str(exc)[:180]}")
                log_event("LLM", "provider failed, falling back",
                          provider=provider.name, error=str(exc)[:180])
        raise LLMError("no LLM provider succeeded -> " + " | ".join(errors))

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
