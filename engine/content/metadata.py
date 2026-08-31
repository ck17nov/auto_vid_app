"""Title, description, tags and chapters (spec sections 17 & 18).

Titles are generated then SCORED, and the misleading-risk term is subtractive:
a title that promises something the script does not contain loses points no
matter how clickable it is.  `TitleScore` is reported out of 100.
"""
from __future__ import annotations

import re
from typing import Any

from ..core.config import Config
from ..core.logging import log_event
from ..core.models import ContentIdea, Script, VideoMetadata
from ..core.niche import NicheProfile
from ..core.util import clamp, keywords, sentences, token_overlap, truncate, words

YOUTUBE_TITLE_LIMIT = 100
YOUTUBE_DESC_LIMIT = 5000
YOUTUBE_TAGS_TOTAL_CHARS = 460          # 500 with separators; stay safely under
# A 40-minute video legitimately has more than a dozen sections. YouTube has
# no documented chapter ceiling; 30 keeps the description readable.
MAX_CHAPTERS = 30

CURIOSITY_WORDS = {
    "why", "how", "what", "who", "actually", "really", "hidden", "secret",
    "nobody", "strange", "stranger", "unexpected", "wrong", "mistake", "myth",
    "found", "discovered", "revealed", "until", "before", "almost", "never",
}
EMOTION_WORDS = {
    "shocking", "incredible", "terrifying", "beautiful", "brutal", "insane",
    "amazing", "unbelievable", "wild", "crazy", "stunning", "haunting",
    "dangerous", "impossible", "extraordinary",
}
# Overpromises we refuse to ship (spec section 17).
MISLEADING_PATTERNS = [
    (r"\b(aliens?|ufo)\b", 0.35, "unverifiable claim"),
    (r"\b(proof|proves|confirmed)\b", 0.18, "overstated certainty"),
    (r"\b(cure|cures|miracle)\b", 0.35, "health overpromise"),
    (r"\b(guaranteed|100%|risk[- ]free)\b", 0.30, "guarantee language"),
    (r"\b(you won'?t believe|will shock you|gone wrong)\b", 0.28, "empty clickbait"),
    (r"[!]{2,}", 0.20, "punctuation shouting"),
    (r"\b[A-Z]{6,}\b", 0.14, "all-caps shouting"),
    (r"\b(get rich|make \$?\d+|double your money)\b", 0.35, "financial overpromise"),
]

SYSTEM_PROMPT = """You write YouTube titles that are clickable AND accurate.

Rules:
- The title must be delivered by the script. No promise the video cannot keep.
- No ALL CAPS words, no multiple exclamation marks, no "you won't believe".
- 40-70 characters is the sweet spot; hard limit 100.
- Specific beats vague: a number, a name or a concrete noun.
- Output valid JSON only."""


# Hook-type / analysis prefixes the idea engine attaches internally. They are
# useful for scoring and must never reach a viewer.
_ANALYSIS_PREFIX = re.compile(
    r"^\s*(consequence|correction|hidden|reveal|contradiction|mechanism|"
    r"question|story|number|comparison|myth|gap|angle)\s*[:\-]\s*", re.I)

_NOT_PUBLISHABLE = re.compile(
    r"(gap score|momentum|cluster|saturated|\bn=|derived from|"
    r"without an LLM|structural|\b\d+\s+(angles?|videos?|samples?)\b)", re.I)


def _publishable_angle(text: str) -> str:
    """Return `text` fit for a public description, or "".

    Strips the internal hook-type prefix and rejects anything that reads like a
    metric or an internal note rather than a sentence.
    """
    cleaned = _ANALYSIS_PREFIX.sub("", (text or "").strip()).strip()
    if not cleaned or len(cleaned.split()) < 4:
        return ""
    if _NOT_PUBLISHABLE.search(cleaned):
        return ""
    return cleaned[0].upper() + cleaned[1:]


class MetadataGenerator:
    def __init__(self, cfg: Config, router=None):
        self.cfg = cfg
        self.router = router

    # ------------------------------------------------------------------
    def build(self, script: Script, idea: ContentIdea, profile: NicheProfile,
              *, video_format: str = "SHORT", language: str = "en",
              made_for_kids: bool = False,
              synthetic_disclosure: bool = True,
              hashtags: bool = True) -> VideoMetadata:
        candidates = self._title_candidates(script, idea, profile)
        scored = [self.score_title(t, script, idea) for t in candidates]
        scored.sort(key=lambda c: c["score"], reverse=True)
        best = scored[0] if scored else {"title": idea.working_title, "score": 50.0}

        meta = VideoMetadata(
            title=truncate(best["title"], YOUTUBE_TITLE_LIMIT),
            title_score=round(best["score"], 1),
            title_candidates=scored[:10],
            category_id=str(self.cfg.get("youtube.default_category_id", "27")),
            privacy=str(self.cfg.get("youtube.default_privacy", "private")),
            made_for_kids=made_for_kids,
            synthetic_disclosure=synthetic_disclosure,
            language=language,
        )
        meta.tags = self.build_tags(script, idea, profile)
        if video_format == "LONGFORM":
            meta.chapters = self.build_chapters(script)
        meta.description = self.build_description(
            script, idea, profile, meta, video_format=video_format,
            hashtags=hashtags)
        log_event("METADATA", "metadata built",
                  title_score=f"{meta.title_score:.0f}/100",
                  tags=len(meta.tags), desc_chars=len(meta.description))
        return meta

    # ------------------------------------------------------------------
    def _title_candidates(self, script: Script, idea: ContentIdea,
                          profile: NicheProfile) -> list[str]:
        """10 candidates: LLM-generated where available, plus structural ones."""
        out: list[str] = []
        out += [t for t in (script.title_ideas or []) if t.strip()]
        if idea.working_title:
            out.append(idea.working_title)

        if self.router is not None and len(out) < 10:
            try:
                data, _ = self.router.complete_json(
                    self._title_prompt(script, idea, profile),
                    system=SYSTEM_PROMPT, temperature=0.9, max_tokens=1024)
                out += [str(t).strip() for t in (data.get("titles") or [])
                        if str(t).strip()]
            except Exception as exc:
                log_event("METADATA", "LLM titles unavailable, using structural",
                          error=str(exc)[:140])

        out += self._structural_titles(script, idea)

        # De-duplicate case-insensitively, keep order, cap at 10.
        seen: set[str] = set()
        unique: list[str] = []
        for t in out:
            t = re.sub(r"\s+", " ", t).strip(" .")
            key = t.lower()
            if t and key not in seen and len(t) <= YOUTUBE_TITLE_LIMIT:
                seen.add(key)
                unique.append(t)
        return unique[:10]

    def _title_prompt(self, script: Script, idea: ContentIdea,
                      profile: NicheProfile) -> str:
        return f"""Write 10 title options for this video.

NICHE: {profile.name}   AUDIENCE: {profile.audience}
TOPIC: {idea.topic}
ANGLE: {idea.angle}
HOOK (first spoken line): {script.hook}

FULL NARRATION:
{truncate(script.script, 1400)}

Every title must be supported by the narration above. Vary the structure across
the ten: question, reveal, mechanism, number, consequence.

Return JSON: {{"titles": ["...", "..."]}}"""

    def _structural_titles(self, script: Script, idea: ContentIdea) -> list[str]:
        """Deterministic fallbacks derived from the actual script content."""
        topic = (idea.topic or "").strip().rstrip(".")
        if not topic:
            return []
        # Title-case each word ("black holes" -> "Black Holes") but leave short
        # acronyms alone ("AI" must not become "Ai").
        subject = " ".join(
            w if w.isupper() and len(w) <= 4 else w.capitalize()
            for w in topic.split())
        angle_words = keywords(idea.angle or script.script, limit=3)
        detail = angle_words[0].title() if angle_words else subject
        # Every pattern here is deliberately NUMBER-AGNOSTIC. The topic label
        # can be singular ("neutron star") or plural ("animals"), so nothing may
        # depend on subject-verb agreement - "What Animals Actually Does" was a
        # real output before this was fixed.
        return [
            f"The Part Of {subject} Nobody Explains",
            f"Inside {subject}: What Most People Miss",
            f"The Truth About {subject}",
            f"{subject}: The Detail Everyone Skips",
            f"How {detail} Changes {subject}",
            f"{subject}, Explained Properly",
        ]

    # ------------------------------------------------------------------
    def score_title(self, title: str, script: Script,
                    idea: ContentIdea) -> dict[str, Any]:
        """Score 0-100 across the spec's eight dimensions."""
        text = title.strip()
        lowered = text.lower()
        toks = set(words(lowered))
        length = len(text)

        curiosity = clamp(len(toks & CURIOSITY_WORDS) / 2.0)
        if text.endswith("?"):
            curiosity = clamp(curiosity + 0.25)

        # Clarity: readable length, not too many words, no jargon pileup.
        word_count = len(text.split())
        clarity = clamp(1.0 - abs(word_count - 9) / 11.0)
        if length > 80:
            clarity *= 0.75

        emotional = clamp(len(toks & EMOTION_WORDS) / 2.0 + 0.25)

        # Specificity: numbers, proper nouns, concrete terms.
        has_number = any(c.isdigit() for c in text)
        proper_nouns = sum(1 for w in text.split()[1:] if w[:1].isupper())
        specificity = clamp((0.4 if has_number else 0.0)
                            + min(proper_nouns, 3) * 0.2 + 0.2)

        # Novelty: does it avoid the tired stock phrasings?
        tired = ("top 10", "you need to know", "in 60 seconds", "explained simply",
                 "mind blowing", "must know")
        novelty = clamp(1.0 - sum(1 for t in tired if t in lowered) * 0.4)

        # Search relevance: shares vocabulary with the actual content.
        search = clamp(token_overlap(text, f"{idea.topic} {script.script[:600]}") * 1.5)

        # Click potential: front-loaded interest.
        first_three = " ".join(text.split()[:3]).lower()
        click = clamp(0.35
                      + (0.3 if set(words(first_three)) & CURIOSITY_WORDS else 0.0)
                      + (0.2 if has_number else 0.0)
                      + (0.15 if 40 <= length <= 70 else 0.0))

        # Misleading risk: subtractive, and also checks the script backs it up.
        risk = 0.0
        reasons: list[str] = []
        for pattern, weight, why in MISLEADING_PATTERNS:
            if re.search(pattern, text, 0 if pattern.startswith(r"\b[A-Z]") else re.I):
                risk += weight
                reasons.append(why)
        # Claim support: every content word should appear in, or relate to, the script.
        support = token_overlap(text, script.script)
        if support < 0.20:
            risk += 0.22
            reasons.append("title vocabulary barely appears in the script")

        parts = {
            "curiosity": curiosity, "clarity": clarity, "emotional_pull": emotional,
            "specificity": specificity, "novelty": novelty,
            "search_relevance": search, "click_potential": click,
        }
        weights = {"curiosity": 0.20, "clarity": 0.16, "emotional_pull": 0.10,
                   "specificity": 0.14, "novelty": 0.10,
                   "search_relevance": 0.14, "click_potential": 0.16}
        base = sum(parts[k] * weights[k] for k in parts)
        score = clamp(base - risk) * 100
        return {"title": text, "score": round(score, 1),
                "breakdown": {k: round(v, 3) for k, v in parts.items()},
                "misleading_risk": round(risk, 3), "risk_reasons": reasons}

    # ------------------------------------------------------------------
    def build_tags(self, script: Script, idea: ContentIdea,
                   profile: NicheProfile) -> list[str]:
        """Relevant tags, no stuffing: derived from real script vocabulary."""
        pool: list[str] = []
        for source in (idea.topic, profile.name, idea.angle):
            pool += [k for k in keywords(source or "", limit=4)]
        pool += keywords(script.script, limit=10)
        # Two-word phrases read as real search terms.
        topic_kws = keywords(f"{idea.topic} {profile.name}", limit=3)
        if len(topic_kws) >= 2:
            pool.append(f"{topic_kws[0]} {topic_kws[1]}")
        if profile.name:
            pool.append(profile.name.lower())

        tags: list[str] = []
        used: set[str] = set()
        total = 0
        for tag in pool:
            t = re.sub(r"\s+", " ", tag).strip().lower()
            if not t or t in used or len(t) < 3:
                continue
            if total + len(t) + 1 > YOUTUBE_TAGS_TOTAL_CHARS or len(tags) >= 15:
                break
            used.add(t)
            tags.append(t)
            total += len(t) + 1
        return tags

    def build_chapters(self, script: Script) -> list[dict[str, Any]]:
        """Chapters for long-form. YouTube needs the first one at 00:00."""
        scenes = script.scene_objects()

        # A sectioned long-form script already carries real section headings
        # from its outline. Those are far better chapter labels than the first
        # sentence of some scene's narration, so prefer them when present.
        if script.chapters:
            starts: list[float] = []
            cursor = 0.0
            for scene in scenes:
                starts.append(cursor)
                cursor += scene.duration or 0.0
            out: list[dict[str, Any]] = []
            for chapter in script.chapters:
                idx = int(chapter.get("scene_index", 0))
                if not 0 <= idx < len(starts):
                    continue
                seconds = round(starts[idx], 1)
                # YouTube requires chapters to be at least 10s apart and the
                # first at 00:00, or it silently shows none at all.
                if out and seconds - out[-1]["seconds"] < 10.0:
                    continue
                out.append({"seconds": seconds,
                            "label": str(chapter.get("heading") or "").strip()
                                     or f"Part {len(out) + 1}"})
            if out:
                out[0]["seconds"] = 0.0
                return out[:MAX_CHAPTERS]

        chapters: list[dict[str, Any]] = []
        cursor = 0.0
        for scene in scenes:
            span = scene.duration or 0.0
            label_source = scene.on_screen_text or scene.narration
            if scene.role in {"hook", "context"} and cursor == 0.0:
                label = "Intro"
            else:
                label = truncate(sentences(label_source)[0]
                                 if sentences(label_source) else label_source, 42)
            if not chapters or (cursor - chapters[-1]["seconds"]) >= 10.0:
                chapters.append({"seconds": round(cursor, 1), "label": label})
            cursor += span
        if chapters:
            chapters[0]["seconds"] = 0.0
        return chapters[:MAX_CHAPTERS]

    # ------------------------------------------------------------------
    def build_description(self, script: Script, idea: ContentIdea,
                          profile: NicheProfile, meta: VideoMetadata, *,
                          video_format: str = "SHORT",
                          hashtags: bool = True) -> str:
        parts: list[str] = []

        # Lead: what the viewer gets, in the video's own words.
        lead = sentences(script.script)
        summary = " ".join(lead[:2]) if lead else idea.angle
        parts.append(truncate(summary, 260))
        # `idea.angle` is an ANALYSIS field, not copy. A real description shipped
        # with the line "consequence: the future tidal silence as the Moon
        # drifts away" - the hook-type label and all. Same class of leak as the
        # one that reached the narration, so it gets the same treatment.
        angle = _publishable_angle(idea.angle)
        if angle:
            parts.append(truncate(angle, 200))

        if meta.chapters and video_format == "LONGFORM":
            parts.append("Chapters:\n" + "\n".join(
                f"{int(c['seconds']) // 60:02d}:{int(c['seconds']) % 60:02d} {c['label']}"
                for c in meta.chapters))

        # Model-generated citations are NOT published.
        #
        # A real run produced these under "Sources and further reading":
        #   - NASA JPL Lunar Recession Update, 2024
        #   - Nature Astronomy, "Moon-Earth Distance Evolution" 2024
        # Plausible-looking, correctly formatted, and unverifiable - the system
        # prompt forbids inventing studies and the model did it anyway. Printing
        # them in a public description presents fabrications as evidence, which
        # is worse than citing nothing. They stay in script.json for review, and
        # the fact-check report already grades the claims that rest on them.
        if script.sources:
            log_event("METADATA", "model-supplied sources withheld from the "
                                  "description (unverifiable)",
                      count=len(script.sources))

        # Required / recommended disclosures (spec sections 11 & 48).
        if meta.synthetic_disclosure:
            parts.append("This video was produced with AI assistance "
                         "(script, synthetic narration and generated visuals).")
        for disclaimer in profile.disclaimers:
            parts.append(disclaimer)
        if profile.made_for_kids:
            parts.append("Made for children. No external links or purchase prompts.")

        if hashtags:
            tag_pool = [t for t in meta.tags if " " not in t][:3]
            if video_format != "LONGFORM":
                tag_pool = (tag_pool + ["shorts"])[:3]
            if tag_pool:
                parts.append(" ".join(f"#{t.replace('-', '')}" for t in tag_pool))

        text = "\n\n".join(p for p in parts if p and p.strip())
        return truncate(text, YOUTUBE_DESC_LIMIT)
