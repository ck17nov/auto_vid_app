"""NicheProfile - keeps the content engine niche-agnostic (spec sections 8 & 9).

A profile is derived, never hard-coded per niche: known niches get a curated
profile, unknown niches fall back to a generated one built from the niche words
plus the closest family match.  This means the user can type ANY niche.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .util import words

# --------------------------------------------------------------------------
# Kids compliance profile (spec section 9) - applied on top of any niche.
# --------------------------------------------------------------------------
KIDS_RESTRICTIONS = [
    "no violence, injury, blood or weapons",
    "no scary imagery, jump scares or horror atmosphere",
    "no dangerous challenges, stunts or imitable risky behaviour",
    "no profanity, slang insults or crude humour",
    "no romance, dating or sexual references",
    "no alcohol, tobacco, drugs or gambling",
    "no manipulative urgency, fake giveaways or 'you must' pressure",
    "no misleading thumbnail or title claims",
    "no collection of personal information or 'tell us in comments where you live'",
    "no external links or purchase prompts aimed at children",
    "simple vocabulary, short sentences, warm and calm delivery",
]

FACTUAL_NICHES = {
    "science", "space", "astronomy", "physics", "biology", "chemistry",
    "technology", "tech", "ai", "programming", "coding", "software",
    "history", "geography", "finance", "money", "investing", "economics",
    "health", "fitness", "nutrition", "medicine", "psychology", "news",
    "facts", "education", "engineering", "cars", "automotive",
}

# Niches where YouTube / advertiser policy needs extra care.
SENSITIVE_NICHES = {
    "health", "medicine", "nutrition", "finance", "investing", "money",
    "crypto", "news", "politics", "law", "psychology", "religion",
}


@dataclass
class NicheProfile:
    name: str
    audience: str = "18-35"
    tone: str = "curious, confident, plain-spoken"
    vocabulary: str = "everyday words, no jargon unless explained in one clause"
    visual_style: str = "clean cinematic photography, high contrast, single clear subject"
    pacing: str = "fast"                     # fast | medium | calm
    hook_style: str = "curiosity gap in the first sentence"
    cta_style: str = "one short line, no begging"
    restrictions: list[str] = field(default_factory=list)
    research_requirements: list[str] = field(default_factory=list)
    scene_seconds: float = 3.2               # target on-screen time per visual
    words_per_second: float = 2.6            # narration density target
    requires_fact_check: bool = False
    is_sensitive: bool = False
    made_for_kids: bool = False
    search_modifiers: list[str] = field(default_factory=list)
    music_mood: str = "subtle tension, low-mid, non-distracting"
    caption_style: str = "karaoke"
    disclaimers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_block(self) -> str:
        """Compact profile description injected into LLM prompts."""
        lines = [
            f"NICHE: {self.name}",
            f"AUDIENCE: {self.audience}",
            f"TONE: {self.tone}",
            f"VOCABULARY: {self.vocabulary}",
            f"PACING: {self.pacing} (~{self.words_per_second:.1f} words/second narration)",
            f"HOOK STYLE: {self.hook_style}",
            f"CTA STYLE: {self.cta_style}",
            f"VISUAL STYLE: {self.visual_style}",
        ]
        if self.restrictions:
            lines.append("HARD RESTRICTIONS:\n- " + "\n- ".join(self.restrictions))
        if self.research_requirements:
            lines.append("RESEARCH REQUIREMENTS:\n- " +
                         "\n- ".join(self.research_requirements))
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Curated families.  A niche is matched to a family by keyword.
# --------------------------------------------------------------------------
_FAMILIES: dict[str, dict[str, Any]] = {
    "science": {
        "keys": {"science", "space", "astronomy", "physics", "biology", "chemistry",
                 "nature", "ocean", "geology", "quantum", "cosmos", "universe"},
        "tone": "awe-driven but precise; never sensational",
        "visual_style": ("cinematic astrophotography and macro science imagery, deep "
                         "blacks, single luminous subject, no text in image"),
        "hook_style": "a concrete surprising fact stated flatly in under 8 words",
        "research_requirements": [
            "every numeric claim must trace to a named institution, mission or journal",
            "distinguish confirmed findings from hypotheses",
            "prefer discoveries from the last 24 months where relevant",
        ],
        "music_mood": "slow cinematic pad with a rising pulse",
        "scene_seconds": 3.4,
    },
    "technology": {
        "keys": {"technology", "tech", "ai", "gadget", "programming", "coding",
                 "software", "computer", "startup", "robot", "engineering", "cyber"},
        "tone": "sharp, practical, slightly skeptical of hype",
        "visual_style": ("modern product photography and abstract circuitry, cool "
                         "blue/teal light, shallow depth of field"),
        "hook_style": "a capability or consequence the viewer did not think possible yet",
        "research_requirements": [
            "name the company/lab and the release or paper date",
            "separate demo claims from shipped capability",
            "avoid predicting stock or product prices",
        ],
        "music_mood": "clean electronic pulse, minimal percussion",
        "scene_seconds": 3.0,
    },
    "history": {
        "keys": {"history", "ancient", "war", "empire", "mystery", "archaeology",
                 "civilization", "medieval", "biography"},
        "tone": "story-first narration, vivid but sober",
        "visual_style": ("painterly historical scenes, aged texture, warm dusty "
                         "palette, dramatic single-source light"),
        "hook_style": "drop the viewer into the strangest moment of the story",
        "research_requirements": [
            "attribute events to a place and an approximate date",
            "flag contested or legendary accounts as contested",
        ],
        "music_mood": "low strings with a slow heartbeat drum",
        "pacing": "medium",
        "scene_seconds": 3.8,
    },
    "finance": {
        "keys": {"finance", "money", "investing", "stocks", "economics", "business",
                 "crypto", "saving", "budget", "entrepreneur"},
        "tone": "calm, concrete, anti-hype",
        "visual_style": ("clean editorial photography, muted greens and greys, charts "
                         "as abstract shapes only"),
        "hook_style": "a common belief that is measurably wrong",
        "research_requirements": [
            "cite the source and date of any figure or return number",
            "no forward-looking price predictions",
            "no personalised advice - general education only",
        ],
        "restrictions": [
            "never phrase anything as 'you should buy/sell'",
            "no guaranteed-return language",
            "no personalised financial advice",
        ],
        "disclaimers": ["Educational content only. Not financial advice."],
        "music_mood": "restrained minimal piano and soft pad",
        "pacing": "medium",
        "scene_seconds": 3.6,
    },
    "health": {
        "keys": {"health", "fitness", "nutrition", "medicine", "sleep", "diet",
                 "workout", "mental", "psychology", "wellness"},
        "tone": "supportive, evidence-led, never alarmist",
        "visual_style": "bright natural light, real-life human context, soft palette",
        "hook_style": "a mechanism the viewer feels but cannot explain",
        "research_requirements": [
            "prefer systematic reviews or major health bodies (WHO, NHS, NIH, ICMR)",
            "state population and effect size, not just direction",
            "never contradict mainstream medical consensus",
        ],
        "restrictions": [
            "no diagnosis, dosage or treatment instructions",
            "no cure claims",
            "no body-shaming framing",
        ],
        "disclaimers": ["General information only. Not medical advice."],
        "music_mood": "warm soft pad, no percussion",
        "pacing": "medium",
        "scene_seconds": 3.8,
    },
    "kids": {
        "keys": {"kids", "children", "toddler", "nursery", "cartoon", "bedtime",
                 "kindergarten", "preschool", "story for kids"},
        "audience": "under 13 (child-directed)",
        "tone": "warm, playful, gentle, encouraging",
        "vocabulary": "very simple words, one idea per sentence, lots of repetition",
        "visual_style": ("bright friendly cartoon illustration, rounded shapes, soft "
                         "primary colours, no realistic faces, no scary elements"),
        "hook_style": "a friendly question or a funny sound-word",
        "cta_style": "gentle invitation to watch the next story - never pressure",
        "research_requirements": [
            "keep every fact age-appropriate and verifiable",
            "no frightening or ambiguous outcomes",
        ],
        "music_mood": "playful ukulele/marimba, very light",
        "pacing": "calm",
        "scene_seconds": 4.2,
        "words_per_second": 2.0,
        "caption_style": "block",
    },
    "storytelling": {
        "keys": {"story", "storytelling", "horror", "reddit", "true", "creepy",
                 "drama", "narrative", "mystery story"},
        "tone": "intimate, escalating, cinematic",
        "visual_style": "moody atmospheric photography, heavy shadow, film grain",
        "hook_style": "the last line of the story told first, out of context",
        "music_mood": "low drone with sparse piano notes",
        "pacing": "medium",
        "scene_seconds": 3.6,
    },
    "education": {
        "keys": {"education", "learning", "study", "exam", "language", "math",
                 "school", "tutorial", "howto", "productivity", "skill"},
        "tone": "clear, encouraging, example-driven",
        "visual_style": "clean minimal graphics, generous whitespace, one concept per frame",
        "hook_style": "the exact mistake the viewer is probably making",
        "music_mood": "light neutral pulse",
        "scene_seconds": 3.4,
    },
    "entertainment": {
        "keys": {"entertainment", "gaming", "game", "travel", "cars", "food",
                 "sports", "movie", "music", "funny", "memes", "celebrity"},
        "tone": "high-energy, conversational, punchy",
        "visual_style": "saturated dynamic photography, bold contrast, motion feel",
        "hook_style": "the most extreme visual moment described in one line",
        "music_mood": "upbeat percussive loop",
        "scene_seconds": 2.8,
    },
}

_DEFAULT_FAMILY = "education"


def _variants(text: str) -> set[str]:
    """Token set plus crude singular/plural variants.

    Without this, "car reviews" misses the `cars` keyword and "history facts"
    misses `fact`. Full stemming is overkill for one-to-three-word niches.
    """
    out: set[str] = set()
    for w in words(text):
        out.add(w)
        if w.endswith("ies") and len(w) > 4:
            out.add(w[:-3] + "y")
        elif w.endswith("es") and len(w) > 3:
            out.add(w[:-2])
        if w.endswith("s") and len(w) > 3:
            out.add(w[:-1])
        else:
            out.add(w + "s")
    return out


def _match_family(niche: str) -> str:
    toks = _variants(niche)
    if not toks:
        return _DEFAULT_FAMILY
    best, best_hits = _DEFAULT_FAMILY, 0
    for fam, spec in _FAMILIES.items():
        keys: set[str] = spec["keys"]
        hits = len(toks & keys)
        # also match multi-word keys contained in the raw string
        raw = niche.lower()
        hits += sum(1 for k in keys if " " in k and k in raw)
        if hits > best_hits:
            best, best_hits = fam, hits
    return best


def build_profile(niche: str, *, audience: str = "18-35", style: str = "",
                  made_for_kids: bool = False, language: str = "en",
                  duration_seconds: int = 45) -> NicheProfile:
    """Create a NicheProfile for ANY niche string."""
    niche = (niche or "general").strip()
    fam = _match_family(niche)
    spec = _FAMILIES[fam]

    profile = NicheProfile(name=niche)
    for key in ("tone", "vocabulary", "visual_style", "pacing", "hook_style",
                "cta_style", "music_mood", "scene_seconds", "words_per_second",
                "caption_style", "audience"):
        if key in spec:
            setattr(profile, key, spec[key])
    profile.restrictions = list(spec.get("restrictions", []))
    profile.research_requirements = list(spec.get("research_requirements", []))
    profile.disclaimers = list(spec.get("disclaimers", []))

    if audience:
        profile.audience = audience
    niche_variants = _variants(niche)
    profile.requires_fact_check = bool(
        niche_variants & FACTUAL_NICHES) or fam in {
            "science", "technology", "history", "finance", "health"}
    profile.is_sensitive = bool(niche_variants & SENSITIVE_NICHES) or fam in {
        "finance", "health"}

    # Style hints from the user tweak pacing without breaking the profile.
    s = (style or "").lower()
    if any(w in s for w in ("fast", "punchy", "rapid", "energetic")):
        profile.pacing = "fast"
        profile.scene_seconds = min(profile.scene_seconds, 2.9)
        profile.words_per_second = max(profile.words_per_second, 2.7)
    elif any(w in s for w in ("calm", "slow", "relaxing", "asmr", "gentle", "sleep")):
        profile.pacing = "calm"
        profile.scene_seconds = max(profile.scene_seconds, 4.2)
        profile.words_per_second = min(profile.words_per_second, 2.2)
    if "cinematic" in s:
        profile.visual_style = "cinematic " + profile.visual_style

    # Long-form gets longer scenes; very short videos get tighter ones.
    if duration_seconds > 180:
        profile.scene_seconds = max(profile.scene_seconds, 4.5)
    elif duration_seconds <= 30:
        profile.scene_seconds = min(profile.scene_seconds, 2.8)

    # Kids overrides win over everything.
    kids_family = fam == "kids"
    if made_for_kids or kids_family:
        kid_spec = _FAMILIES["kids"]
        profile.made_for_kids = True
        profile.audience = kid_spec["audience"]
        profile.tone = kid_spec["tone"]
        profile.vocabulary = kid_spec["vocabulary"]
        profile.visual_style = kid_spec["visual_style"]
        profile.hook_style = kid_spec["hook_style"]
        profile.cta_style = kid_spec["cta_style"]
        profile.music_mood = kid_spec["music_mood"]
        profile.pacing = "calm"
        profile.words_per_second = 2.0
        profile.scene_seconds = max(profile.scene_seconds, 4.0)
        profile.caption_style = "block"
        profile.restrictions = KIDS_RESTRICTIONS + profile.restrictions

    profile.search_modifiers = _search_modifiers(fam, profile)
    if language and language != "en":
        profile.search_modifiers = profile.search_modifiers[:2]
    return profile


def _search_modifiers(family: str, profile: NicheProfile) -> list[str]:
    base = ["explained", "facts", "shorts"]
    per_family = {
        "science": ["discovery", "facts", "explained"],
        "technology": ["new", "explained", "review"],
        "history": ["story", "mystery", "explained"],
        "finance": ["explained", "mistakes", "basics"],
        "health": ["explained", "myths", "science of"],
        "kids": ["for kids", "story", "learning"],
        "storytelling": ["story", "true story", "scary story"],
        "education": ["explained", "tips", "how to"],
        "entertainment": ["moments", "best", "reaction"],
    }
    mods = per_family.get(family, base)
    return mods if not profile.made_for_kids else ["for kids", "learning", "story"]


def is_kids_niche(niche: str) -> bool:
    return _match_family(niche) == "kids"
