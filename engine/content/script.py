"""Script generation (spec section 10).

Two things make or break Shorts retention, and both are enforced here rather
than left to the LLM's goodwill:

  1. The word budget.  Narration length is computed from the target duration and
     the niche's words-per-second, then the generated script is measured and
     re-fitted.  A "45 second" video that runs 78 seconds is a failed video.
  2. The opening.  Banned openers ("hey guys", "welcome back", "in this video")
     are stripped and the hook is forced to land in the first sentence.

Research is passed in as verified context and the model is explicitly forbidden
from paraphrasing any single source (spec section 7).
"""
from __future__ import annotations

import re
from typing import Any

from ..core.config import Config
from ..core.logging import log_event
from ..core.models import ContentIdea, Scene, Script
from ..core.niche import NicheProfile
from ..core.util import agree, count_words, sentences, truncate
from .llm import LLMError, LLMRouter

# Openings that waste the first seconds. Retention dies here (spec section 15).
BANNED_OPENERS = [
    r"^hey,?\s+(guys|everyone|friends|there)\b",
    r"^hi,?\s+(guys|everyone|friends|there)\b",
    r"^what'?s up,?\s+\w+",
    r"^welcome (back )?(to|everyone)?\b",
    r"^in (this|today'?s) (video|short|episode)\b",
    r"^today (we|i)('| a)?(re| am)? going to\b",
    r"^before we (start|begin)\b",
    r"^don'?t forget to (like|subscribe)\b",
    r"^let'?s (get started|dive in|jump in)\b",
    r"^so,? (basically|yeah)\b",
    r"^have you ever wondered\b",
]

SYSTEM_PROMPT = """You are a senior short-form video writer whose scripts are \
known for extremely high audience retention. You write ORIGINAL scripts.

Absolute rules:
- Never copy, paraphrase, or restructure any specific existing script. Research \
tells you what a topic is about; the writing must be your own.
- No greeting, no channel intro, no "in this video". The first sentence must \
already be the hook.
- Every sentence must earn the next one. Cut any sentence that only sets up \
another sentence.
- Concrete over abstract: specific numbers, names, places, consequences.
- Short sentences. Spoken rhythm, not written prose.
- No clickbait that the script does not pay off.
- Never invent statistics, studies, dates or quotes. If you are not confident in \
a fact, write it in a way that is still true (e.g. "researchers think") and mark \
it in the claims array with a lower confidence.
- Output valid JSON only."""


def _budget(profile: NicheProfile, duration: int) -> tuple[int, int, int]:
    """Return (target_words, min_words, scene_count)."""
    wps = max(profile.words_per_second, 1.2)
    target = int(duration * wps)
    scenes = max(3, min(int(round(duration / max(profile.scene_seconds, 1.5))), 24))
    return target, int(target * 0.82), scenes


def _structure(duration: int, is_short: bool) -> list[tuple[str, str, float]]:
    """(role, purpose, fraction-of-duration). Dynamic, not hard-coded seconds."""
    if is_short:
        return [
            ("hook", "one sentence that creates an unresolved question or shock", 0.07),
            ("context", "the minimum background needed to care", 0.16),
            ("value", "the substance: what happened / how it works, escalating", 0.55),
            ("payoff", "resolve the hook with the most interesting fact", 0.17),
            ("cta", "one short line, or a loop back to the hook", 0.05),
        ]
    # Fractions must sum to 1.0 - anything less leaves part of the target
    # duration unallocated, which is how a "10 minute" video comes out at 9:24.
    return [
        ("hook", "the strongest moment, stated cold", 0.04),
        ("context", "why this matters now", 0.10),
        ("promise", "what the viewer will know by the end", 0.06),
        ("value", "the main sections, each with a concrete example", 0.60),
        ("payoff", "the conclusion that reframes the opening", 0.16),
        ("cta", "one specific next action", 0.04),
    ]


class ScriptGenerator:
    def __init__(self, cfg: Config, router: LLMRouter | None = None):
        self.cfg = cfg
        self.router = router or LLMRouter(
            list(cfg.get("content.llm_provider_order", ["groq", "gemini", "ollama", "template"])),
            cfg)
        self.temperature = float(cfg.get("content.temperature", 0.85))

    # ------------------------------------------------------------------
    def generate(self, idea: ContentIdea, profile: NicheProfile, *,
                 duration: int, language: str = "en",
                 video_format: str = "SHORT",
                 research_context: str = "",
                 strategy_hints: str = "") -> Script:
        is_short = video_format != "LONGFORM"
        target_words, min_words, scene_count = _budget(profile, duration)
        structure = _structure(duration, is_short)

        prompt = self._build_prompt(
            idea, profile, duration, language, is_short, target_words,
            scene_count, structure, research_context, strategy_hints)

        try:
            data, provider = self.router.complete_json(
                prompt, system=SYSTEM_PROMPT, temperature=self.temperature,
                max_tokens=4096 if is_short else 8192)
            script = self._parse(data, idea, profile, language, provider)
        except LLMError as exc:
            log_event("SCRIPT", "LLM unavailable, using deterministic builder",
                      error=str(exc)[:180])
            script = build_template_script(
                idea, profile, duration, language, structure, target_words)

        script = self._post_process(script, profile, target_words, min_words,
                                    duration, is_short)
        log_event("SCRIPT", "script generated", provider=script.provider,
                  words=count_words(script.script), scenes=len(script.scenes),
                  target_words=target_words,
                  est_seconds=f"{script.estimated_duration:.1f}")
        return script

    # ------------------------------------------------------------------
    def _build_prompt(self, idea: ContentIdea, profile: NicheProfile,
                      duration: int, language: str, is_short: bool,
                      target_words: int, scene_count: int,
                      structure: list[tuple[str, str, float]],
                      research_context: str, strategy_hints: str) -> str:
        beats = "\n".join(
            f"- {role.upper()} (~{int(frac * duration)}s): {purpose}"
            for role, purpose, frac in structure)
        lang_line = ("Write the narration in English."
                     if language.startswith("en") else
                     f"Write the narration in this language code: {language}. "
                     f"Natural native phrasing, not translated English.")
        kids_line = ""
        if profile.made_for_kids:
            kids_line = (
                "\nTHIS IS CHILD-DIRECTED CONTENT. Simple words, one idea per "
                "sentence, warm and calm. Nothing scary, no danger, no conflict, "
                "no romance, no pressure to act.\n")

        return f"""Write an original {duration}-second {'YouTube Short' if is_short else 'YouTube video'} script.

{profile.prompt_block()}
{kids_line}
CONCEPT (already validated - do not change the topic):
  Topic: {idea.topic}
  Angle: {idea.angle}
  Hook concept: {idea.hook_concept}
  Hook type: {idea.hook_type}
  Why now: {idea.why_now}

{lang_line}

LENGTH IS A HARD CONSTRAINT:
  Total narration must be {target_words} words (+/- 8%). Count them.
  Split into exactly {scene_count} scenes of roughly equal spoken length.

STRUCTURE:
{beats}

{research_context or 'No external research was available; stay with widely established facts only.'}

{strategy_hints}

VISUALS: for each scene give a `visual_prompt` describing ONE image - a literal,
photographable subject, camera framing and lighting. No text, no words, no logos
in the image. Also give 2-4 `visual_keywords` (plain nouns) for stock search.

ON-SCREEN TEXT: `on_screen_text` is optional, max 4 words, only where a number
or name deserves emphasis. Leave it empty otherwise - spoken captions already
cover the narration.

Return this exact JSON shape:
{{
  "title_ideas": ["8-12 words each, 5 options, no ALL CAPS, no false claims"],
  "hook": "the first sentence, max 12 words",
  "scenes": [
    {{"role": "hook|context|promise|value|payoff|cta",
      "narration": "spoken words only",
      "visual_prompt": "one image description",
      "visual_keywords": ["noun", "noun"],
      "on_screen_text": ""}}
  ],
  "voice_style": "energetic|calm|serious|storytelling|excited|gentle",
  "cta": "one short line",
  "claims": [
    {{"claim": "a factual statement made in the script",
      "confidence": "high|medium|low",
      "basis": "what this rests on"}}
  ],
  "sources": [{{"title": "", "note": "what it supports"}}]
}}"""

    # ------------------------------------------------------------------
    def _parse(self, data: dict[str, Any], idea: ContentIdea,
               profile: NicheProfile, language: str, provider: str) -> Script:
        raw_scenes = data.get("scenes") or []
        if not raw_scenes:
            raise LLMError("LLM returned no scenes")

        scenes: list[dict[str, Any]] = []
        for i, s in enumerate(raw_scenes):
            if not isinstance(s, dict):
                continue
            narration = str(s.get("narration") or "").strip()
            if not narration:
                continue
            kws = s.get("visual_keywords") or []
            if isinstance(kws, str):
                kws = [k.strip() for k in kws.split(",") if k.strip()]
            scenes.append(Scene(
                index=len(scenes),
                narration=narration,
                visual_prompt=str(s.get("visual_prompt") or narration).strip(),
                visual_keywords=[str(k) for k in kws][:5],
                on_screen_text=truncate(str(s.get("on_screen_text") or ""), 28),
                role=str(s.get("role") or "value").lower(),
            ).to_dict())
        if not scenes:
            raise LLMError("no usable scenes after parsing")

        titles = [str(t).strip() for t in (data.get("title_ideas") or []) if str(t).strip()]
        claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
        sources = [s for s in (data.get("sources") or []) if isinstance(s, dict)]

        return Script(
            idea_id=idea.idea_id,
            title_ideas=titles[:10],
            hook=str(data.get("hook") or scenes[0]["narration"]).strip(),
            script=" ".join(s["narration"] for s in scenes),
            scenes=scenes,
            visual_plan=[s["visual_prompt"] for s in scenes],
            voice_style=str(data.get("voice_style") or "energetic").lower(),
            cta=str(data.get("cta") or "").strip(),
            language=language,
            sources=[{"title": str(s.get("title", "")), "note": str(s.get("note", ""))}
                     for s in sources][:8],
            claims=claims[:20],
            provider=provider,
        )

    # ------------------------------------------------------------------
    def _post_process(self, script: Script, profile: NicheProfile,
                      target_words: int, min_words: int, duration: int,
                      is_short: bool) -> Script:
        scenes = script.scene_objects()

        # 1. Strip banned openers from the very first scene.
        if scenes:
            cleaned = strip_banned_opener(scenes[0].narration)
            if cleaned != scenes[0].narration:
                log_event("SCRIPT", "removed weak opener")
                scenes[0].narration = cleaned or scenes[0].narration

        # 2. Drop empty scenes and renumber.
        scenes = [s for s in scenes if s.narration.strip()]
        for i, s in enumerate(scenes):
            s.index = i
            if not s.visual_prompt:
                s.visual_prompt = s.narration
            if not s.visual_keywords:
                from ..core.util import keywords as kw_extract
                s.visual_keywords = kw_extract(s.narration, limit=3)
            # Visual style from the niche profile, applied consistently.
            if profile.visual_style and profile.visual_style not in s.visual_prompt:
                s.visual_prompt = f"{s.visual_prompt}, {profile.visual_style}"

        # 3. Trim to the word budget if the model overran badly.
        words_total = sum(count_words(s.narration) for s in scenes)
        if words_total > target_words * 1.28 and len(scenes) > 3:
            scenes = _trim_to_budget(scenes, int(target_words * 1.12))
            log_event("SCRIPT", "trimmed overlong script",
                      from_words=words_total,
                      to_words=sum(count_words(s.narration) for s in scenes))

        script.scenes = [s.to_dict() for s in scenes]
        script.script = " ".join(s.narration for s in scenes)
        script.visual_plan = [s.visual_prompt for s in scenes]
        script.hook = strip_banned_opener(script.hook) or (
            scenes[0].narration if scenes else "")
        script.estimated_duration = round(
            count_words(script.script) / max(profile.words_per_second, 1.2), 2)
        return script


def strip_banned_opener(text: str) -> str:
    """Remove filler openings, then re-capitalise."""
    out = (text or "").strip()
    for _ in range(3):
        before = out
        for pattern in BANNED_OPENERS:
            out = re.sub(pattern + r"[,!.]?\s*", "", out, flags=re.I).strip()
        # Also drop a leading standalone filler clause.
        out = re.sub(r"^(so|well|okay|alright|now),?\s+", "", out, flags=re.I).strip()
        if out == before:
            break
    if out:
        out = out[0].upper() + out[1:]
    return out


def _trim_to_budget(scenes: list[Scene], budget: int) -> list[Scene]:
    """Shorten from the middle 'value' scenes first; never cut hook or payoff."""
    total = sum(count_words(s.narration) for s in scenes)
    if total <= budget:
        return scenes
    protected = {"hook", "payoff"}
    # Drop whole low-value scenes from the middle outward.
    order = sorted(
        (i for i, s in enumerate(scenes)
         if s.role not in protected and i not in (0, len(scenes) - 1)),
        key=lambda i: -count_words(scenes[i].narration))
    keep = [True] * len(scenes)
    for i in order:
        if total <= budget or sum(keep) <= 3:
            break
        keep[i] = False
        total -= count_words(scenes[i].narration)
    trimmed = [s for s, k in zip(scenes, keep) if k]

    # Still over budget: shorten the longest remaining scene by sentence.
    while total > budget and len(trimmed) >= 3:
        idx = max(range(len(trimmed)), key=lambda i: count_words(trimmed[i].narration))
        parts = sentences(trimmed[idx].narration)
        if len(parts) <= 1:
            break
        removed = count_words(parts[-1])
        trimmed[idx].narration = " ".join(parts[:-1])
        total -= removed
    for i, s in enumerate(trimmed):
        s.index = i
    return trimmed


# --------------------------------------------------------------------------
# Deterministic fallback builder (clearly degraded, never silent)
# --------------------------------------------------------------------------
def build_template_script(idea: ContentIdea, profile: NicheProfile,
                          duration: int, language: str,
                          structure: list[tuple[str, str, float]],
                          target_words: int) -> Script:
    """Assemble a real script from the idea fields without an LLM.

    This is a genuine degraded path, not a mock: it produces coherent narration
    from validated research fields.  Provider is recorded as `template` so the
    quality gate can penalise it and the UI can warn the user.
    """
    topic = idea.topic or "this topic"
    hook = idea.hook_concept or f"There is something about {topic} that does not add up."

    beats: list[tuple[str, str]] = [
        ("hook", hook),
        ("context", f"{topic.capitalize()} {agree(topic, 'is', 'are')} usually "
                    f"explained the same way every time."),
    ]

    # `idea.angle` and `idea.why_now` are ANALYSIS FIELDS, not narration.
    # Speaking them verbatim leaked internal text into the audio - a real bug
    # caught in testing, where a video narrated "1 angles on 'space' are already
    # saturated; this one is not." They are only used here if they read like a
    # plain sentence, and always as a phrased clause rather than raw.
    angle_line = _narratable(idea.angle)
    if angle_line:
        beats.append(("value", f"The part worth looking at is {angle_line}."))
    why_line = _narratable(idea.why_now)
    if why_line:
        beats.append(("value", f"{why_line[0].upper()}{why_line[1:]}."))

    # Fill toward the word budget.
    #
    # Without this the builder produced ~47 words against a 108-word target,
    # and the TTS re-fit then had to slow delivery to -28% to reach the
    # requested duration, which sounds wrong. Expanding here keeps the speaking
    # rate natural.
    #
    # Every filler line is FRAMING, never a factual assertion: with no LLM and
    # no verified research text there is nothing to state truthfully about the
    # subject, and inventing specifics would be worse than being general.
    filler = [
        ("value", f"Most explanations of {topic} stop at the surface."),
        ("value", "The detail that actually matters is easy to miss."),
        ("value", f"Look closer at {topic} and the usual summary starts to break down."),
        ("value", "That gap is the interesting part."),
        ("value", "It changes which questions are worth asking."),
        ("value", f"It also explains why {topic} keeps coming up."),
        ("value", "The short version leaves out the part that matters most."),
        ("value", "Once you notice it, you cannot unsee it."),
        ("value", f"There is a reason {topic} {agree(topic, 'is', 'are')} "
                  f"described so loosely."),
        ("value", "The simple story is easier to tell than the accurate one."),
        ("value", "That is fine, right up until the detail matters."),
        ("value", f"And with {topic}, the detail matters."),
        ("value", "It reframes what the usual summary implies."),
        ("value", "Which is why the standard explanation feels incomplete."),
    ]
    budget = max(target_words, 24)
    used = sum(count_words(text) for _, text in beats)
    payoff = f"That is what makes {topic} worth a second look."
    cta_line = ("See you in the next story." if profile.made_for_kids
                else "Follow for more of these.")
    reserved = count_words(payoff) + count_words(cta_line)

    for role, text in filler:
        if used + reserved >= budget * 0.92:
            break
        beats.append((role, text))
        used += count_words(text)

    beats.append(("payoff", payoff))
    beats.append(("cta", cta_line))

    reached = used + reserved
    if reached < budget * 0.85:
        # Be explicit rather than silently shipping a short video: the pool of
        # honest, non-asserting framing lines is finite, and padding further
        # would just be repetition.
        log_event("SCRIPT", "template builder could not fill the word budget",
                  words=reached, target=budget,
                  hint="configure GROQ_API_KEY or GEMINI_API_KEY for full-length "
                       "scripts at this duration")

    scenes: list[dict[str, Any]] = []
    from ..core.util import keywords as kw_extract
    for role, text in beats:
        scenes.append(Scene(
            index=len(scenes), narration=text, role=role,
            visual_prompt=f"{topic}, {profile.visual_style}",
            visual_keywords=kw_extract(f"{topic} {text}", limit=3),
        ).to_dict())

    body = " ".join(s["narration"] for s in scenes)
    return Script(
        idea_id=idea.idea_id,
        title_ideas=[idea.working_title] if idea.working_title else [topic.title()],
        hook=hook,
        script=body,
        scenes=scenes,
        visual_plan=[s["visual_prompt"] for s in scenes],
        voice_style="calm" if profile.made_for_kids else "energetic",
        cta=scenes[-1]["narration"],
        language=language,
        provider="template",
        claims=[{"claim": body, "confidence": "low",
                 "basis": "assembled from research fields without an LLM"}],
    )


# Markers of internal analysis text that must never reach the narration.
_NOT_NARRATION = re.compile(
    r"(\bangles?\b.*\bsaturated\b|\bgap score\b|\bmomentum\b|\bcluster\b|"
    r"\bderived from\b|\bwithout an LLM\b|\bstructural\b|['\"]|"
    r"\b\d+\s+(angles?|videos?|samples?)\b|:|=|\bn=)", re.I)


def _narratable(text: str) -> str:
    """Return `text` only if it is safe to speak aloud, else "".

    Analysis fields describe the *decision*, not the *content*. Anything that
    reads like a metric, a quoted keyword, or an internal note is rejected
    rather than narrated.
    """
    cleaned = (text or "").strip().rstrip(".").strip()
    if not cleaned or len(cleaned.split()) < 3 or len(cleaned.split()) > 26:
        return ""
    if _NOT_NARRATION.search(cleaned):
        return ""
    return cleaned[0].lower() + cleaned[1:]
