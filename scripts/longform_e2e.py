#!/usr/bin/env python
"""End-to-end long-form check with a STUBBED LLM.

    python scripts/longform_e2e.py [seconds]

Why this exists: every stage of long-form except the LLM can be verified
locally. This replaces ONLY the LLM router with a deterministic writer, then
runs the genuine pipeline - sectioned script assembly, per-scene TTS with real
word timings, 100+ generated visuals, karaoke captions, the batched render, the
quality gate. That covers the parts that broke when the scene cap was lifted.

The stub is clearly labelled: the resulting job records
`provider: stub-llm`, so it can never be mistaken for real model output. It is
NOT a substitute for a run with a real GROQ_API_KEY, which is the only way to
judge script quality.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.core.config import load_config           # noqa: E402
from engine.core.models import AutomationRequest      # noqa: E402
from engine.pipeline import Pipeline                  # noqa: E402

# Framing sentences with real English structure, so TTS, word-boundary timing,
# caption grouping and the retention scorer all see plausible input.
BODY = [
    "The record everyone cites was compiled from three separate surveys.",
    "Each survey used a different definition of the same boundary.",
    "When the numbers were merged, that difference was quietly dropped.",
    "The result looked far more precise than the underlying data allowed.",
    "Later measurements narrowed the range but never removed the ambiguity.",
    "That is the part the summary version leaves out entirely.",
    "It matters because the figure is still quoted as a single value.",
    "A single value implies a confidence nobody actually has.",
    "The original authors were explicit about the uncertainty.",
    "Their caveat did not survive the second retelling.",
    "By the third, the range had become a fact.",
    "This is how a careful estimate turns into a round number.",
]


class StubRouter:
    """Deterministic stand-in for LLMRouter with the same call contract."""

    def __init__(self, sections: int = 12):
        self.sections = sections
        self.outline_calls = 0
        self.section_calls = 0

    def has_real_llm(self) -> bool:
        return True

    def hints(self) -> str:
        return ""

    def complete_json(self, prompt, *, system="", temperature=0.8,
                      max_tokens=4096, attempts=2):
        if "Plan an original" in prompt:
            self.outline_calls += 1
            return {
                "title_ideas": [
                    "The Measurement That Was Never As Precise As It Looks",
                    "How A Range Became A Round Number",
                ],
                "hook": "The number in every textbook was never a single value.",
                "voice_style": "serious",
                "cta": "Check the original paper before you quote the figure.",
                "sections": [
                    {"heading": h, "purpose": p, "key_points": k}
                    for h, p, k in [
                        ("Where The Number Came From", "establish the source",
                         ["three surveys, three definitions"]),
                        ("What Got Dropped", "show the loss of nuance",
                         ["the merge discarded the disagreement"]),
                        ("Why It Looked Precise", "explain the false confidence",
                         ["a single value implies certainty"]),
                        ("What The Authors Said", "restore the caveat",
                         ["the original text was explicit"]),
                        ("How It Spread", "trace the retelling",
                         ["each retelling shed a qualifier"]),
                        ("What Is Actually Known", "state the honest range",
                         ["the range never closed"]),
                    ]
                ][:self.sections] * max(1, self.sections // 6),
                "sources": [{"title": "The original survey report",
                             "note": "states the uncertainty explicitly"}],
            }, "stub-llm"

        if "Write ONE SECTION" in prompt:
            import re
            self.section_calls += 1
            m = re.search(r"exactly\s+(\d+)\s+scenes", prompt)
            scenes = int(m.group(1)) if m else 8
            m2 = re.search(r"is (\d+) words of narration", prompt)
            words = int(m2.group(1)) if m2 else scenes * 12
            per_scene = max(1, round(words / max(scenes, 1)))
            out = []
            for n in range(scenes):
                line, used = [], 0
                while used < per_scene:
                    s = BODY[(self.section_calls * 7 + n * 3 + len(line)) % len(BODY)]
                    line.append(s)
                    used += len(s.split())
                out.append({
                    "role": "value",
                    "narration": " ".join(line),
                    "visual_prompt": f"an archive reading room, shaft of window "
                                     f"light across a table, angle {n}",
                    "visual_keywords": ["archive", "documents", "light"],
                    "on_screen_text": "",
                })
            return {"scenes": out,
                    "claims": [{"claim": "The cited figure was a range.",
                                "confidence": "medium",
                                "basis": "stated in the section"}]}, "stub-llm"

        # Single-shot path (titles, or a Short) - not what this script tests.
        return {"titles": ["How A Range Became A Round Number"],
                "scenes": [], "title_ideas": []}, "stub-llm"


def main() -> int:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 240
    cfg = load_config()
    cfg.set("dry_run", True)
    # Force the batched render path even at a modest scene count, so this
    # actually exercises segmentation rather than the flat chain.
    cfg.set("video.render_segment_max", 15)

    pipe = Pipeline(cfg)
    stub = StubRouter()
    pipe.router = stub
    pipe.script_engine.router = stub
    pipe.idea_engine.router = stub
    pipe.metadata_engine.router = None      # use the structural title builder

    request = AutomationRequest(
        niche="history", duration_seconds=seconds, video_format="LONGFORM",
        style="measured, evidence-first", audience="25-45")

    print(f"Long-form end-to-end: {seconds}s, LLM stubbed, dry run\n")
    started = time.monotonic()
    result = pipe.run(request)
    elapsed = time.monotonic() - started

    job_dir = Path(result.job.dir)
    print("\n--- result ---------------------------------------------")
    print(f"job dir        {job_dir}")
    print(f"status         {result.job.status}")
    print(f"wall clock     {elapsed / 60:.1f} min")
    print(f"outline calls  {stub.outline_calls}")
    print(f"section calls  {stub.section_calls}")
    if result.quality:
        print(f"quality        {result.quality.score}/100  "
              f"passed={result.quality.passed}")
        for b in result.quality.blockers:
            print(f"  BLOCKER  {b}")
        for w in result.quality.warnings[:8]:
            print(f"  warn     {w}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
