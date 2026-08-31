"""Reusable video style templates (spec section 45).

A NicheProfile says what the content *is*. A StyleTemplate says how the video
*looks and moves*. They are separate because the same niche can be shot several
ways: "science" can be a FAST_FACTS rapid-fire Short or a calm
SCIENCE_EXPLAINER.

Each template controls: scene duration, typography, transitions, caption style,
pacing, background treatment and visual frequency.

Templates are selected automatically from the niche + user style text, or forced
with `video.style_template` in config.yaml.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.niche import NicheProfile
from ..core.util import words


@dataclass
class StyleTemplate:
    name: str
    description: str

    # --- pacing / structure ---
    scene_seconds: float                  # target on-screen time per visual
    visual_frequency: float = 1.0          # multiplier on images per minute
    words_per_second: float = 2.6

    # --- typography ---
    font: str = "Anton"                    # file stem in assets/fonts/
    font_scale: float = 1.0                # multiplier on captions.font_size
    uppercase: bool = True
    letter_spacing: float = 1.2

    # --- captions ---
    caption_style: str = "karaoke"         # karaoke | block | none
    highlight_color: str = "&H0000E5FF"    # ASS BGR
    outline: int = 7
    safe_bottom: float = 0.22

    # --- motion / transitions ---
    transition: str = "fade"               # fade | smoothleft | slideup | auto
    transition_duration: float = 0.35
    motion_cycle: list[str] = field(default_factory=lambda: [
        "zoom_in", "pan_right", "zoom_out", "pan_left"])
    kenburns: bool = True

    # --- look ---
    contrast: float = 1.045
    saturation: float = 1.07
    visual_style_suffix: str = ""          # appended to every image prompt
    music_mood: str = "cinematic"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
TEMPLATES: dict[str, StyleTemplate] = {
    "FAST_FACTS": StyleTemplate(
        name="FAST_FACTS",
        description="Rapid-fire facts. Maximum cuts, big punchy captions.",
        scene_seconds=2.4, visual_frequency=1.35, words_per_second=2.9,
        font_scale=1.10, caption_style="karaoke",
        highlight_color="&H0000E5FF",           # amber
        transition="fade", transition_duration=0.22,
        motion_cycle=["zoom_in", "pan_left", "zoom_out", "pan_right",
                      "zoom_in", "pan_up"],
        contrast=1.07, saturation=1.14,
        visual_style_suffix="bold high-contrast composition, single clear subject",
        music_mood="tension",
    ),
    "TECH_NEWS": StyleTemplate(
        name="TECH_NEWS",
        description="Clean, current, slightly clinical. Product-shot feel.",
        scene_seconds=3.0, words_per_second=2.7,
        font_scale=0.95, highlight_color="&H00FFD34D",   # cyan-ish
        transition="smoothleft", transition_duration=0.30,
        motion_cycle=["pan_right", "zoom_in", "pan_left", "zoom_out"],
        contrast=1.05, saturation=1.05,
        visual_style_suffix=("modern product photography, cool blue and teal "
                             "light, shallow depth of field"),
        music_mood="tech",
    ),
    "SCIENCE_EXPLAINER": StyleTemplate(
        name="SCIENCE_EXPLAINER",
        description="Awe-driven, cinematic, room to breathe.",
        scene_seconds=3.6, words_per_second=2.5,
        font_scale=1.0, highlight_color="&H00FFC46A",
        transition="fade", transition_duration=0.45,
        motion_cycle=["zoom_in", "zoom_out", "pan_up", "pan_down"],
        contrast=1.06, saturation=1.10,
        visual_style_suffix=("cinematic astrophotography, deep blacks, one "
                             "luminous subject, no text"),
        music_mood="cinematic",
    ),
    "STORYTELLING": StyleTemplate(
        name="STORYTELLING",
        description="Intimate narrative. Slow pushes, moody grade.",
        scene_seconds=3.8, words_per_second=2.4,
        font_scale=0.92, caption_style="karaoke",
        highlight_color="&H00B0B0FF", outline=6,
        transition="fade", transition_duration=0.55,
        motion_cycle=["zoom_in", "pan_left", "zoom_in", "pan_right"],
        contrast=1.09, saturation=0.94,
        visual_style_suffix=("moody atmospheric photography, heavy shadow, "
                             "film grain, single light source"),
        music_mood="sombre",
    ),
    "TOP_5": StyleTemplate(
        name="TOP_5",
        description="Countdown list. Hard cuts, numbers on screen.",
        scene_seconds=2.8, visual_frequency=1.2, words_per_second=2.8,
        font_scale=1.08, highlight_color="&H004DFF4D",   # green
        transition="slideup", transition_duration=0.26,
        motion_cycle=["zoom_out", "pan_right", "zoom_in", "pan_left"],
        contrast=1.07, saturation=1.12,
        visual_style_suffix="bold graphic composition, strong subject separation",
        music_mood="tech",
    ),
    "MYSTERY": StyleTemplate(
        name="MYSTERY",
        description="Withholds. Long holds, cold grade, slow reveals.",
        scene_seconds=4.0, visual_frequency=0.85, words_per_second=2.3,
        font_scale=0.95, highlight_color="&H00FF6AD1",
        transition="fade", transition_duration=0.60,
        motion_cycle=["zoom_in", "zoom_in", "pan_up", "zoom_out"],
        contrast=1.12, saturation=0.88,
        visual_style_suffix=("dark cinematic photography, fog, negative space, "
                             "unresolved composition"),
        music_mood="sombre",
    ),
    "KIDS_STORY": StyleTemplate(
        name="KIDS_STORY",
        description="Gentle, bright, calm. Whole-phrase captions.",
        scene_seconds=4.2, visual_frequency=0.8, words_per_second=2.0,
        font_scale=0.90, uppercase=False,
        caption_style="block",                  # never flash single words at kids
        highlight_color="&H0080E0FF", outline=6, safe_bottom=0.26,
        transition="fade", transition_duration=0.65,
        motion_cycle=["zoom_in", "pan_right", "zoom_out", "pan_left"],
        contrast=1.02, saturation=1.06,
        visual_style_suffix=("bright friendly cartoon illustration, rounded "
                             "shapes, soft primary colours, nothing scary"),
        music_mood="playful",
    ),
    "EDUCATIONAL": StyleTemplate(
        name="EDUCATIONAL",
        description="Clear and unhurried. One idea per frame.",
        scene_seconds=3.4, words_per_second=2.5,
        font_scale=0.95, highlight_color="&H00FFD34D",
        transition="fade", transition_duration=0.38,
        motion_cycle=["pan_right", "zoom_in", "pan_left", "zoom_out"],
        contrast=1.04, saturation=1.03,
        visual_style_suffix="clean minimal composition, generous negative space",
        music_mood="warm",
    ),
    "MOTIVATIONAL": StyleTemplate(
        name="MOTIVATIONAL",
        description="Rising energy, warm grade, strong typography.",
        scene_seconds=3.0, words_per_second=2.7,
        font_scale=1.12, highlight_color="&H0040D0FF",
        transition="auto", transition_duration=0.34,
        motion_cycle=["zoom_in", "pan_up", "zoom_in", "pan_right"],
        contrast=1.08, saturation=1.16,
        visual_style_suffix=("golden hour light, human silhouette, wide open "
                             "landscape, aspirational"),
        music_mood="cinematic",
    ),
}

DEFAULT_TEMPLATE = "EDUCATIONAL"

# Words in the niche / user style that point at a template.
_HINTS: dict[str, tuple[str, ...]] = {
    "KIDS_STORY": ("kids", "children", "toddler", "nursery", "bedtime",
                   "preschool", "cartoon"),
    "FAST_FACTS": ("facts", "fast", "rapid", "punchy", "quick", "did you know",
                   "trivia"),
    "TOP_5": ("top", "countdown", "ranked", "ranking", "best", "list",
              "listicle"),
    "MYSTERY": ("mystery", "unsolved", "strange", "creepy", "unexplained",
                "disappeared", "conspiracy"),
    "STORYTELLING": ("story", "storytelling", "narrative", "tale", "reddit",
                     "confession", "horror", "scary", "creepypasta"),
    "TECH_NEWS": ("tech", "technology", "ai", "gadget", "software", "startup",
                  "programming", "coding", "news"),
    "SCIENCE_EXPLAINER": ("science", "space", "physics", "biology", "astronomy",
                          "cosmos", "quantum", "explainer", "explained"),
    "MOTIVATIONAL": ("motivation", "motivational", "discipline", "mindset",
                     "success", "inspire", "productivity"),
    "EDUCATIONAL": ("education", "learn", "learning", "tutorial", "how to",
                    "guide", "study", "history", "finance", "health"),
}

# Tie-break order, so selection never depends on dict iteration.
_PRIORITY = ("KIDS_STORY", "MYSTERY", "TOP_5", "FAST_FACTS", "STORYTELLING",
             "TECH_NEWS", "SCIENCE_EXPLAINER", "MOTIVATIONAL", "EDUCATIONAL")


def select_template(niche: str, style: str = "", *,
                    made_for_kids: bool = False,
                    forced: str = "") -> StyleTemplate:
    """Choose a template from the niche and the user's style text.

    Child-directed content always gets KIDS_STORY: its caption style and pacing
    are part of the safety profile, not a preference.
    """
    if forced:
        template = TEMPLATES.get(forced.strip().upper())
        if template:
            return template

    if made_for_kids:
        return TEMPLATES["KIDS_STORY"]

    haystack = f"{niche} {style}".lower()
    # Plural-tolerant token set: "true horror stories" must match the "story"
    # hint, otherwise it silently falls through to the default template.
    tokens: set[str] = set()
    for w in words(haystack):
        tokens.add(w)
        if w.endswith("ies") and len(w) > 4:
            tokens.add(w[:-3] + "y")
        elif w.endswith("es") and len(w) > 3:
            tokens.add(w[:-2])
        if w.endswith("s") and len(w) > 3:
            tokens.add(w[:-1])
        else:
            tokens.add(w + "s")

    scores: dict[str, float] = {}
    for name, hints in _HINTS.items():
        score = 0.0
        for hint in hints:
            if " " in hint:
                score += 1.0 if hint in haystack else 0.0
            else:
                score += 1.0 if hint in tokens else 0.0
        scores[name] = score

    best = max(scores.values(), default=0.0)
    if best <= 0:
        return TEMPLATES[DEFAULT_TEMPLATE]
    for name in _PRIORITY:
        if scores.get(name, 0.0) == best:
            return TEMPLATES[name]
    return TEMPLATES[DEFAULT_TEMPLATE]


def apply_to_profile(profile: NicheProfile,
                     template: StyleTemplate) -> NicheProfile:
    """Fold the template's look-and-feel into the niche profile.

    The niche keeps authority over CONTENT (restrictions, fact-check needs,
    disclaimers); the template governs PRESENTATION. Kids restrictions are never
    relaxed by a template.
    """
    profile.scene_seconds = template.scene_seconds
    profile.words_per_second = template.words_per_second
    profile.caption_style = template.caption_style
    profile.music_mood = template.music_mood
    if template.visual_style_suffix:
        profile.visual_style = template.visual_style_suffix
    return profile


def caption_overrides(template: StyleTemplate,
                      base_font_size: int) -> dict[str, Any]:
    """Caption engine settings for this template."""
    return {
        "captions.font_file": template.font,
        "captions.font_size": max(24, int(base_font_size * template.font_scale)),
        "captions.uppercase": template.uppercase,
        "captions.highlight_color": template.highlight_color,
        "captions.outline": template.outline,
        "captions.safe_bottom": template.safe_bottom,
        "captions.style": template.caption_style,
    }


def video_overrides(template: StyleTemplate) -> dict[str, Any]:
    """Composer settings for this template."""
    return {
        "video.transition": template.transition,
        "video.transition_duration": template.transition_duration,
        "video.kenburns": template.kenburns,
        "video.contrast": template.contrast,
        "video.saturation": template.saturation,
    }


def list_templates() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description,
         "scene_seconds": t.scene_seconds, "caption_style": t.caption_style,
         "music_mood": t.music_mood}
        for t in TEMPLATES.values()
    ]
