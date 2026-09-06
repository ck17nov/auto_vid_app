"""Character bible: the same people, drawn the same way, in every shot.

An illustrated story is only a story if the cast is recognisable. Generate each
scene from its narration alone and every shot invents new children - different
ages, different clothes, different number of them - and the result reads as a
mood board rather than a narrative. Fixing that needs one decision made once
and then repeated verbatim, which is what this module holds.

Two constraints shaped it:

  * BREVITY. A description has to be short enough that it does not bury the
    scene it is attached to. Long character blocks demonstrably cause the
    generator to ignore the scene entirely, so each character gets one clause,
    and only the characters a scene actually mentions are sent with it.

  * HONESTY ABOUT WHAT THIS ACHIEVES. Repeating a textual description gets
    "recognisably the same child", not "the same child". Real consistency needs
    reference-image conditioning, which the free generators do not offer. This
    closes most of the gap; it does not close all of it, and the fallback when
    no model is available is no characters at all rather than a guess that
    would make things worse.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.logging import log_event

# Deliberately tight. Anything longer competes with the scene description.
MAX_DESCRIPTION_CHARS = 140
MAX_CHARACTERS = 6

_SYSTEM = (
    "You design character sheets for an illustrated story. You reply with one "
    "JSON object and nothing else."
)

_PROMPT = """From the narration below, identify the recurring PEOPLE (or animals)
who appear in more than one scene and will need to be drawn consistently.

Rules:
- At most {max_characters} characters. Only recurring ones; ignore people who
  appear once.
- Each description is ONE short clause under {max_chars} characters, covering
  only what an illustrator must repeat: approximate age, hair, clothing,
  distinguishing feature. No personality, no backstory, no scene detail.
- Use the name the narration uses. If a character is unnamed but recurring,
  give a plain descriptive label such as "the grandmother".
- If the story has no recurring characters at all (an explainer, a list, a
  documentary), return an empty list. Do not invent people.

Return JSON exactly like:
{{"characters": [{{"name": "RAJU", "description": "a 10-year-old boy, short black hair, green shirt, barefoot"}}]}}

NARRATION:
{narration}
"""


@dataclass
class Character:
    name: str
    description: str

    def clause(self) -> str:
        return f"{self.name} is {self.description}"


@dataclass
class CharacterBible:
    characters: list[Character] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.characters)

    # ------------------------------------------------------------------
    def clause_for(self, *texts: str) -> str:
        """Descriptions of only the characters these texts mention.

        Falls back to the whole cast when nothing matches by name, because a
        scene often refers to "the children" or "they" while still needing them
        drawn correctly. Capped at three so the clause cannot grow long enough
        to swamp the scene.
        """
        if not self.characters:
            return ""
        haystack = " ".join(texts).lower()
        hits = [c for c in self.characters
                if _mentions(haystack, c.name)]
        chosen = hits or self.characters
        return ". ".join(c.clause() for c in chosen[:3])

    def to_dict(self) -> dict[str, Any]:
        return {"characters": [{"name": c.name, "description": c.description}
                               for c in self.characters]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CharacterBible:
        out: list[Character] = []
        for raw in (data or {}).get("characters", [])[:MAX_CHARACTERS]:
            name = str(raw.get("name", "")).strip()
            desc = str(raw.get("description", "")).strip()
            if not name or not desc:
                continue
            out.append(Character(name=name,
                                 description=desc[:MAX_DESCRIPTION_CHARS]))
        return cls(characters=out)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                        encoding="utf-8")


def _mentions(haystack: str, name: str) -> bool:
    token = re.escape(name.strip().lower())
    if not token:
        return False
    return re.search(rf"\b{token}\b", haystack) is not None


def build_bible(narration: str, router: Any) -> CharacterBible:
    """Ask the model who recurs. Returns an empty bible on any failure.

    Empty is a safe answer: the AI provider simply omits the character clause,
    which is exactly the previous behaviour. A wrong bible is worse than none,
    because it would be repeated into every single frame.
    """
    text = (narration or "").strip()
    if not text or router is None:
        return CharacterBible()
    try:
        data, provider = router.complete_json(
            _PROMPT.format(narration=text[:6000],
                           max_characters=MAX_CHARACTERS,
                           max_chars=MAX_DESCRIPTION_CHARS),
            system=_SYSTEM, temperature=0.4, max_tokens=900)
    except Exception as exc:
        log_event("CHARACTER", "bible unavailable, continuing without one",
                  error=type(exc).__name__)
        return CharacterBible()

    bible = CharacterBible.from_dict(data)
    log_event("CHARACTER", "cast fixed", count=len(bible.characters),
              names=", ".join(c.name for c in bible.characters) or "-",
              provider=provider)
    return bible
