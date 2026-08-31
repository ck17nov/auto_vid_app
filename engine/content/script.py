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
import time
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


# One scene == one visual, so this is really a cap on how many images and TTS
# calls a single video may cost. It used to be a flat 24, which silently turned
# any long-form request into a slideshow: a 10-minute video got 24 scenes, i.e.
# one static image held for 25 seconds. The cap now only exists to bound cost,
# and it is high enough that pacing is decided by the niche profile instead.
DEFAULT_MAX_SCENES = 400


def _budget(profile: NicheProfile, duration: int, *,
            max_scenes: int = DEFAULT_MAX_SCENES) -> tuple[int, int, int]:
    """Return (target_words, min_words, scene_count)."""
    wps = max(profile.words_per_second, 1.2)
    target = int(duration * wps)
    ideal = int(round(duration / max(profile.scene_seconds, 1.5)))
    ceiling = max_scenes if max_scenes > 0 else DEFAULT_MAX_SCENES
    scenes = max(3, min(ideal, ceiling))
    if ideal > ceiling:
        log_event("SCRIPT", "scene count capped; visuals will hold longer "
                            "than the niche pacing target",
                  wanted=ideal, cap=ceiling,
                  seconds_per_scene=f"{duration / scenes:.1f}")
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
        target_words, min_words, scene_count = _budget(
            profile, duration,
            max_scenes=int(self.cfg.get("content.max_scenes", DEFAULT_MAX_SCENES)))
        structure = _structure(duration, is_short)

        # A single JSON response cannot carry a 3,000-word script: free-tier
        # per-minute token limits (6k-12k TPM) cut it off mid-array, and even
        # when it fits the model thins out the middle. Past this size the script
        # is built section by section instead - see _generate_sectioned.
        section_threshold = int(self.cfg.get("content.section_threshold", 20))
        script: Script | None = None
        if scene_count > section_threshold:
            try:
                script = self._generate_sectioned(
                    idea, profile, duration, language, target_words,
                    scene_count, structure, research_context, strategy_hints)
            except LLMError as exc:
                log_event("SCRIPT", "sectioned generation failed",
                          error=str(exc)[:180])

        if script is None:
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
    def _generate_sectioned(self, idea: ContentIdea, profile: NicheProfile,
                            duration: int, language: str, target_words: int,
                            scene_count: int,
                            structure: list[tuple[str, str, float]],
                            research_context: str,
                            strategy_hints: str) -> Script:
        """Build a long script as an outline plus one LLM call per section.

        This is what makes long-form possible on a free tier at all. Asking for
        a 3,000-word script in one response fails two ways: the reply exceeds
        the free per-minute token limit and gets truncated mid-JSON, and models
        that do fit it pad the middle with restatement.

        Each section call is small (roughly 250 words of narration), so it fits
        comfortably inside a 6k-12k tokens/minute budget, and each one is given
        the headings already covered so it does not repeat them.

        A section that fails is filled from the deterministic builder rather
        than aborting the whole script - one weak stretch in a 20-minute video
        is recoverable, losing the video is not.
        """
        per_section = max(6, min(12, int(self.cfg.get("content.scenes_per_section", 10))))
        section_count = max(2, min(60, int(round(scene_count / per_section))))

        # Sectioned generation multiplies whatever one call costs by the number
        # of sections. That is fine for a hosted model at 2-5s per call and
        # ruinous for CPU-only ollama, which was measured at over 10 minutes
        # per call on this project's dev machine - 14 sections would be hours.
        # The outline call is timed and used as the estimate.
        budget = float(self.cfg.get("content.section_time_budget_seconds", 600))
        started = time.monotonic()
        outline, provider = self._outline(
            idea, profile, duration, language, target_words, section_count,
            structure, research_context, strategy_hints)
        outline_seconds = time.monotonic() - started
        projected = outline_seconds * section_count
        if projected > budget:
            raise LLMError(
                f"{provider} takes {outline_seconds:.0f}s per call; "
                f"{section_count} sections would need ~{projected / 60:.0f} "
                f"minutes (budget {budget / 60:.0f}). Set GROQ_API_KEY or "
                f"GEMINI_API_KEY for long-form, or raise "
                f"content.section_time_budget_seconds.")

        sections = [s for s in (outline.get("sections") or []) if isinstance(s, dict)]
        if not sections:
            raise LLMError("outline contained no sections")

        # Distribute the scene budget across the sections the model actually
        # returned, not the number we asked for.
        scenes_each = _spread(scene_count, len(sections))
        words_each = _spread(target_words, len(sections))

        all_scenes: list[Scene] = []
        all_claims: list[dict[str, Any]] = []
        chapters: list[dict[str, Any]] = []
        covered: list[str] = []
        degraded = 0

        for i, section in enumerate(sections):
            heading = str(section.get("heading") or f"Part {i + 1}").strip()
            role_hint = ("hook" if i == 0 else
                         "payoff" if i == len(sections) - 1 else "value")
            chapters.append({"heading": truncate(heading, 42),
                             "scene_index": len(all_scenes)})
            if time.monotonic() - started > budget:
                # The per-call estimate was optimistic. Fill the rest rather
                # than run for hours; the sections written so far are real.
                degraded += 1
                log_event("SCRIPT", "time budget spent, filling remaining "
                                    "sections deterministically",
                          section=i, of=len(sections),
                          elapsed=f"{time.monotonic() - started:.0f}s")
                all_scenes += _template_section(
                    idea, profile, heading, role_hint, scenes_each[i],
                    words_each[i], len(all_scenes))
                covered.append(heading)
                continue
            try:
                data, _ = self.router.complete_json(
                    self._section_prompt(
                        idea, profile, language, outline, section, heading,
                        role_hint, scenes_each[i], words_each[i], covered,
                        research_context, i == 0, i == len(sections) - 1),
                    system=SYSTEM_PROMPT, temperature=self.temperature,
                    max_tokens=2048)
                got = self._parse_scenes(data.get("scenes") or [], len(all_scenes))
                if not got:
                    raise LLMError(f"section {i} produced no scenes")
                all_claims += [c for c in (data.get("claims") or [])
                               if isinstance(c, dict)]
            except LLMError as exc:
                degraded += 1
                log_event("SCRIPT", "section fell back to template",
                          section=i, heading=heading, error=str(exc)[:140])
                got = _template_section(
                    idea, profile, heading, role_hint, scenes_each[i],
                    words_each[i], len(all_scenes))
            all_scenes += got
            covered.append(heading)

        if not all_scenes:
            raise LLMError("no scenes across any section")

        titles = [str(t).strip() for t in (outline.get("title_ideas") or [])
                  if str(t).strip()]
        label = provider if not degraded else f"{provider}+template"
        log_event("SCRIPT", "sectioned script assembled",
                  sections=len(sections), degraded=degraded,
                  scenes=len(all_scenes),
                  words=sum(count_words(s.narration) for s in all_scenes),
                  target_words=target_words, provider=label)

        return Script(
            idea_id=idea.idea_id,
            title_ideas=titles[:10],
            hook=str(outline.get("hook") or all_scenes[0].narration).strip(),
            script=" ".join(s.narration for s in all_scenes),
            scenes=[s.to_dict() for s in all_scenes],
            visual_plan=[s.visual_prompt for s in all_scenes],
            voice_style=str(outline.get("voice_style") or "energetic").lower(),
            cta=str(outline.get("cta") or "").strip(),
            language=language,
            chapters=chapters,
            claims=all_claims[:40],
            sources=[{"title": str(s.get("title", "")), "note": str(s.get("note", ""))}
                     for s in (outline.get("sources") or [])
                     if isinstance(s, dict)][:12],
            provider=label,
        )

    # ------------------------------------------------------------------
    def _outline(self, idea: ContentIdea, profile: NicheProfile, duration: int,
                 language: str, target_words: int, section_count: int,
                 structure: list[tuple[str, str, float]],
                 research_context: str,
                 strategy_hints: str) -> tuple[dict[str, Any], str]:
        beats = "\n".join(f"- {role.upper()} (~{int(frac * duration)}s): {purpose}"
                          for role, purpose, frac in structure)
        minutes = duration / 60.0
        prompt = f"""Plan an original {minutes:.0f}-minute YouTube video.

{profile.prompt_block()}

CONCEPT (already validated - do not change the topic):
  Topic: {idea.topic}
  Angle: {idea.angle}
  Hook concept: {idea.hook_concept}

Total narration will be about {target_words} words. Break the body into exactly
{section_count} sections. Each section must advance a DIFFERENT idea - no
section may restate another. Give each 2-4 concrete key points that the writer
can turn into narration; a key point must be specific, not a topic label.

OVERALL SHAPE:
{beats}

{research_context or 'No external research was available; stay with widely established facts only.'}

{strategy_hints}

Return this exact JSON shape and nothing else:
{{
  "title_ideas": ["8-12 words each, 5 options, no ALL CAPS, no false claims"],
  "hook": "the first sentence of the video, max 14 words",
  "voice_style": "energetic|calm|serious|storytelling|excited|gentle",
  "cta": "one specific next action",
  "sections": [
    {{"heading": "3-6 words",
      "purpose": "what this section does for the viewer",
      "key_points": ["specific point", "specific point"]}}
  ],
  "sources": [{{"title": "", "note": "what it supports"}}]
}}"""
        return self.router.complete_json(
            prompt, system=SYSTEM_PROMPT, temperature=self.temperature,
            max_tokens=3072)

    # ------------------------------------------------------------------
    def _section_prompt(self, idea: ContentIdea, profile: NicheProfile,
                        language: str, outline: dict[str, Any],
                        section: dict[str, Any], heading: str, role_hint: str,
                        scenes: int, words: int, covered: list[str],
                        research_context: str, is_first: bool,
                        is_last: bool) -> str:
        points = section.get("key_points") or []
        if isinstance(points, str):
            points = [points]
        point_lines = "\n".join(f"  - {str(p).strip()}" for p in points) or "  - (none given)"
        already = ("\nSECTIONS ALREADY WRITTEN (do not repeat these):\n"
                   + "\n".join(f"  - {c}" for c in covered)) if covered else ""
        lang_line = ("Write the narration in English."
                     if language.startswith("en") else
                     f"Write the narration in this language code: {language}. "
                     f"Natural native phrasing, not translated English.")
        kids_line = ""
        if profile.made_for_kids:
            kids_line = ("\nTHIS IS CHILD-DIRECTED CONTENT. Simple words, one "
                         "idea per sentence, warm and calm. Nothing scary.\n")
        edge = ""
        if is_first:
            edge = (f"\nThis is the OPENING. Scene 1 must be the hook, stated "
                    f"cold: \"{outline.get('hook', '')}\". No greeting, no "
                    f"channel intro, no \"in this video\".\n")
        elif is_last:
            edge = (f"\nThis is the CLOSING. Resolve the opening hook, then end "
                    f"with this call to action: \"{outline.get('cta', '')}\".\n")

        return f"""Write ONE SECTION of an existing video script.

VIDEO TOPIC: {idea.topic}
VIDEO ANGLE: {idea.angle}
SECTION: {heading}
SECTION PURPOSE: {section.get('purpose', '')}
KEY POINTS TO COVER:
{point_lines}
{already}{edge}{kids_line}
{lang_line}

LENGTH IS A HARD CONSTRAINT:
  This section is {words} words of narration (+/- 10%), split into exactly
  {scenes} scenes of roughly equal spoken length - about {max(1, round(words / max(scenes, 1)))}
  words per scene. Count the words. Both numbers matter: a section that hits
  the scene count with twice the words makes the video overrun and forces
  whole scenes to be cut back out.

Write only this section. Do not summarise the video, do not introduce yourself,
do not preview what comes later. Continue as if mid-sentence in a longer piece.
Predominant scene role: {role_hint}.

VISUALS: for each scene give a `visual_prompt` describing ONE image - a literal,
photographable subject, camera framing and lighting. No text, no words, no logos
in the image. Also give 2-4 `visual_keywords` (plain nouns).

Return this exact JSON shape and nothing else:
{{
  "scenes": [
    {{"role": "hook|context|promise|value|payoff|cta",
      "narration": "spoken words only",
      "visual_prompt": "one image description",
      "visual_keywords": ["noun", "noun"],
      "on_screen_text": ""}}
  ],
  "claims": [{{"claim": "", "confidence": "high|medium|low", "basis": ""}}]
}}"""

    # ------------------------------------------------------------------
    def _parse_scenes(self, raw: Any, start_index: int) -> list[Scene]:
        """Turn a raw `scenes` array into Scene objects, skipping junk."""
        out: list[Scene] = []
        if not isinstance(raw, list):
            return out
        for s in raw:
            if not isinstance(s, dict):
                continue
            narration = str(s.get("narration") or "").strip()
            if not narration:
                continue
            kws = s.get("visual_keywords") or []
            if isinstance(kws, str):
                kws = [k.strip() for k in kws.split(",") if k.strip()]
            out.append(Scene(
                index=start_index + len(out),
                narration=narration,
                visual_prompt=str(s.get("visual_prompt") or narration).strip(),
                visual_keywords=[str(k) for k in kws][:5],
                on_screen_text=truncate(str(s.get("on_screen_text") or ""), 28),
                role=str(s.get("role") or "value").lower(),
            ))
        return out

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
        # Chapters reference scenes by index, and the steps below renumber and
        # can delete scenes. Track object identity so the headings still point
        # at the right scene afterwards instead of drifting.
        orig_index_by_id = {id(s): s.index for s in scenes}

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

        # 3. Trim to the word budget if the model overran.
        #
        # The threshold differs by format, and deliberately so. On a 45s Short
        # a scene is a large fraction of the video, so it is better to absorb a
        # moderate overrun by speaking slightly faster than to delete content.
        # On a 4-minute video a scene is ~3% of the whole and there are dozens,
        # so trimming is cheap - and leaving it to the speaking rate is not:
        # a 17% word overrun measured here forced a +31% delivery rate, which
        # sounds rushed. Trim first, let the rate make the fine correction.
        trigger, ceiling = (1.28, 1.12) if is_short else (1.08, 1.02)
        words_total = sum(count_words(s.narration) for s in scenes)
        if words_total > target_words * trigger and len(scenes) > 3:
            scenes = _trim_to_budget(scenes, int(target_words * ceiling))
            log_event("SCRIPT", "trimmed overlong script",
                      from_words=words_total,
                      to_words=sum(count_words(s.narration) for s in scenes),
                      target=target_words)

        if script.chapters:
            script.chapters = _remap_chapters(
                script.chapters, scenes, orig_index_by_id)

        script.scenes = [s.to_dict() for s in scenes]
        script.script = " ".join(s.narration for s in scenes)
        script.visual_plan = [s.visual_prompt for s in scenes]
        script.hook = strip_banned_opener(script.hook) or (
            scenes[0].narration if scenes else "")
        script.estimated_duration = round(
            count_words(script.script) / max(profile.words_per_second, 1.2), 2)
        return script


def _remap_chapters(chapters: list[dict[str, Any]], scenes: list[Scene],
                    orig_index_by_id: dict[int, int]) -> list[dict[str, Any]]:
    """Re-point chapter headings at scenes after renumbering and trimming.

    A chapter must NOT be dropped just because the one scene it happened to
    start on was trimmed: the section's other scenes are still in the video, so
    the heading is still true - it just starts slightly later. Dropping it
    instead lost 3 of 8 chapters on a 20-minute test script.

    Chapters are therefore re-anchored to the first surviving scene at or after
    their original position, and collapsed if two land on the same scene.
    """
    surviving = sorted(
        (orig_index_by_id[id(s)], i) for i, s in enumerate(scenes)
        if id(s) in orig_index_by_id)
    if not surviving:
        return []

    out: list[dict[str, Any]] = []
    used: set[int] = set()
    for chapter in chapters:
        want = int(chapter.get("scene_index", -1))
        anchor = next((new for orig, new in surviving if orig >= want), None)
        if anchor is None or anchor in used:
            continue
        used.add(anchor)
        out.append({**chapter, "scene_index": anchor})

    if len(out) != len(chapters):
        log_event("SCRIPT", "chapters merged after trimming",
                  before=len(chapters), after=len(out))
    return out


def _spread(total: int, parts: int) -> list[int]:
    """Split `total` into `parts` whole numbers that sum to exactly `total`.

    Naive `total // parts` loses the remainder, which for a 3,000-word script
    across 13 sections silently drops up to 12 words per section - about 8% of
    the video. The remainder is distributed one unit at a time instead.
    """
    parts = max(1, parts)
    base, extra = divmod(max(total, parts), parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def _template_section(idea: ContentIdea, profile: NicheProfile, heading: str,
                      role_hint: str, scenes: int, words: int,
                      start_index: int) -> list[Scene]:
    """Deterministic filler for ONE failed section of a long script.

    Clearly degraded, never a mock: the lines are framing only and assert
    nothing factual, because with no LLM response for this section there is
    nothing verified to say. The section heading carries the actual meaning.
    """
    topic = idea.topic or "this topic"
    pool = [
        f"{heading} is where this gets specific.",
        f"The usual account of {topic} skips over it.",
        "The detail that matters is easy to walk past.",
        f"Look at {topic} from here and the shape changes.",
        "That gap is the part worth holding on to.",
        "It changes which question is the right one to ask.",
        f"It is also why {topic} keeps coming back around.",
        "The short version leaves this out entirely.",
    ]
    out: list[Scene] = []
    used = 0
    i = 0
    while len(out) < max(1, scenes):
        text = pool[i % len(pool)]
        i += 1
        out.append(Scene(
            index=start_index + len(out),
            narration=text,
            role=role_hint if len(out) == 0 else "value",
            visual_prompt=f"{topic}, {heading}, {profile.visual_style}",
            visual_keywords=_kw(f"{topic} {heading}"),
        ))
        used += count_words(text)
        if used >= words and len(out) >= 1:
            break
    return out


def _kw(text: str, limit: int = 3) -> list[str]:
    from ..core.util import keywords as kw_extract
    return kw_extract(text, limit=limit)


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
    """Shorten from the middle 'value' scenes first; never cut hook or payoff.

    Deletions are spread across the timeline rather than taken strictly
    longest-first. On a short script those are the same thing, but on a
    sectioned long-form script the scenes are near-uniform in length, so
    "longest first" degenerates into deleting one contiguous block - in testing
    that removed the entire back third of a 20-minute video, taking three
    chapters with it. Within each region the flabbiest scene still goes first.
    """
    total = sum(count_words(s.narration) for s in scenes)
    if total <= budget:
        return scenes
    protected = {"hook", "payoff"}
    candidates = [i for i, s in enumerate(scenes)
                  if s.role not in protected and i not in (0, len(scenes) - 1)]
    keep = [True] * len(scenes)

    # One region for a Short (preserving the original longest-first behaviour),
    # up to 12 for a long script.
    regions = max(1, min(12, len(scenes) // 8))
    buckets: list[list[int]] = [[] for _ in range(regions)]
    for i in candidates:
        buckets[min(regions - 1, i * regions // max(len(scenes), 1))].append(i)
    for bucket in buckets:
        bucket.sort(key=lambda i: -count_words(scenes[i].narration))

    # Round-robin: at most one deletion per region per lap.
    while total > budget and sum(keep) > 3:
        progressed = False
        for bucket in buckets:
            if total <= budget or sum(keep) <= 3:
                break
            while bucket:
                i = bucket.pop(0)
                if keep[i]:
                    keep[i] = False
                    total -= count_words(scenes[i].narration)
                    progressed = True
                    break
        if not progressed:
            break
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
