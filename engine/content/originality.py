"""Originality protection + fact-check discipline (spec sections 7 & 11).

Two independent jobs:

  1. `OriginalityChecker` - similarity checks against the researched corpus and
     against our OWN previously published scripts (anti-spam, spec section 46),
     plus the ORIGINALITY REPORT that must exist before publishing.

  2. `FactChecker` - separates RESEARCH from SCRIPT GENERATION. It grades each
     claim the script makes, flags high-risk claims for human approval, and
     refuses to let unverifiable medical/financial claims through silently.

Neither pretends to be authoritative: both surface risk for the quality gate
and the approval flow rather than silently "passing" content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.config import Config
from ..core.db import Database
from ..core.logging import log_event
from ..core.models import Asset, ContentIdea, ResearchVideo, Script
from ..core.niche import NicheProfile
from ..core.util import STOPWORDS, jaccard, sha1, token_overlap, words


@dataclass
class OriginalityResult:
    passed: bool = True
    max_similarity: float = 0.0
    closest_source: str = ""
    self_similarity: float = 0.0
    closest_own_script: str = ""
    findings: list[str] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "max_similarity_to_research": round(self.max_similarity, 4),
            "closest_source": self.closest_source,
            "self_similarity": round(self.self_similarity, 4),
            "closest_own_script": self.closest_own_script,
            "findings": self.findings,
            "report": self.report,
        }


class OriginalityChecker:
    def __init__(self, cfg: Config, db: Database | None = None):
        self.cfg = cfg
        self.db = db
        self.threshold = float(cfg.get("originality.max_similarity", 0.30))
        self.self_threshold = float(
            cfg.get("automation.duplicate_similarity_threshold", 0.80))

    def check(self, script: Script, idea: ContentIdea,
              videos: list[ResearchVideo], assets: list[Asset] | None = None,
              *, voice_provider: str = "") -> OriginalityResult:
        result = OriginalityResult()

        # --- 1. Against the researched corpus --------------------------
        # We only ever have titles + descriptions (we never download anyone's
        # video or transcript), so the comparison is against that text.
        #
        # The blocking decision uses 4-gram Jaccard over the SCRIPT, because
        # reused wording is what plagiarism actually looks like. Hook-vs-title
        # overlap is reported separately and never blocks on its own: with the
        # min-length denominator, a hook sharing one topic word with a
        # two-word title ("space" vs "Why Space Is Dark") scored 0.5 and
        # blocked a completely original script during testing.
        worst_title_overlap = 0.0
        worst_title = ""
        for v in videos:
            corpus = f"{v.title} {v.description[:900]}"
            sim = jaccard(script.script, corpus, n=4)
            if sim > result.max_similarity:
                result.max_similarity = sim
                result.closest_source = f"{v.title[:80]} ({v.video_id})"

            # Compare titles on their distinctive words only, and only when the
            # existing title has enough content words to be meaningful.
            title_tokens = [w for w in words(v.title) if w not in STOPWORDS]
            if len(title_tokens) >= 3:
                overlap = token_overlap(script.hook, v.title)
                if overlap > worst_title_overlap:
                    worst_title_overlap, worst_title = overlap, v.title

        if result.max_similarity >= self.threshold:
            result.passed = False
            result.findings.append(
                f"Script reuses {result.max_similarity * 100:.0f}% of the wording "
                f"in '{result.closest_source}' - above the "
                f"{self.threshold * 100:.0f}% limit.")

        # A near-identical hook/title is worth a warning for the reviewer.
        if worst_title_overlap >= 0.85:
            result.findings.append(
                f"Hook closely echoes the title '{worst_title[:70]}' "
                f"({worst_title_overlap * 100:.0f}% word overlap) - consider "
                f"rephrasing, though the script itself is original.")

        # --- 2. Against our own history (anti-spam) --------------------
        if self.db is not None:
            for script_id, text in self.db.recent_script_texts(limit=60):
                if script_id == script.script_id or not text:
                    continue
                sim = jaccard(script.script, text, n=4)
                if sim > result.self_similarity:
                    result.self_similarity = sim
                    result.closest_own_script = script_id
            if result.self_similarity >= self.self_threshold:
                result.passed = False
                result.findings.append(
                    f"Script is {result.self_similarity * 100:.0f}% similar to our "
                    f"own earlier script {result.closest_own_script} - this would "
                    f"be near-duplicate publishing.")

        # --- 3. The ORIGINALITY REPORT (spec section 7) ---------------
        result.report = {
            "concept": {
                "topic": idea.topic,
                "angle": idea.angle,
                "hook_type": idea.hook_type,
                "originality_note": idea.originality_note,
                "generated_by": script.provider,
            },
            "research_sources": [
                {"video_id": v.video_id, "title": v.title,
                 "channel": v.channel_title, "views": v.views,
                 "used_as": "demand signal only - not source material"}
                for v in videos[:10]
            ],
            "inspiration_videos": idea.inspiration_video_ids,
            "script_originality": {
                "max_similarity_to_any_researched_text": round(result.max_similarity, 4),
                "similarity_threshold": self.threshold,
                "similarity_to_our_previous_scripts": round(result.self_similarity, 4),
                "script_sha1": sha1(script.script),
                "method": ("4-gram Jaccard over content words of the script; "
                           "hook/title overlap is reported but never blocks on "
                           "its own"),
                "verdict": "original" if result.passed else "needs review",
            },
            "visual_sources": [
                {"asset": a.asset, "source": a.source, "license": a.license,
                 "attribution": a.attribution}
                for a in (assets or [])
            ],
            "audio_sources": {
                "narration": (f"synthetic TTS ({voice_provider}) - provider's own "
                              f"synthetic voice, not a clone of any real person"),
                "music": "procedurally synthesised or user-supplied licensed track",
                "sfx": "procedurally synthesised from noise - no third-party samples",
            },
            "declared_not_used": [
                "no third-party video footage",
                "no copied or paraphrased scripts",
                "no downloaded copyrighted music",
                "no cloned voices",
            ],
        }

        log_event("ORIGINALITY", "check complete",
                  passed=result.passed,
                  vs_research=f"{result.max_similarity:.2f}",
                  vs_own=f"{result.self_similarity:.2f}")
        return result


# --------------------------------------------------------------------------
# Fact checking
# --------------------------------------------------------------------------
# Patterns that must not be asserted casually.
HIGH_RISK_PATTERNS: list[tuple[str, str]] = [
    (r"\b(cure|cures|treats|prevents)\b.*\b(cancer|covid|diabetes|autism|hiv)\b",
     "medical cure claim"),
    (r"\b(vaccines?|vaccination)\b.*\b(cause|caused|autism|danger)\b",
     "vaccine misinformation risk"),
    # Order-independent, and plural-tolerant: "guaranteed returns, risk-free"
    # must match as readily as "risk-free guaranteed return".
    (r"\b(guaranteed|risk[- ]free|can'?t lose|no risk)\b"
     r"(?:(?!\b(?:guaranteed|risk[- ]free)\b).){0,80}?"
     r"\b(returns?|profits?|gains?|investments?|money)\b",
     "financial guarantee"),
    (r"\b(returns?|profits?|gains?|investments?)\b.{0,60}?"
     r"\b(guaranteed|risk[- ]free|no risk)\b",
     "financial guarantee"),
    (r"\b(buy|sell|invest in)\b.{0,60}?\b(now|today|immediately)\b",
     "personalised financial advice"),
    (r"\b(\d{2,3}(\.\d+)?%)\b.*\b(of (all )?(people|deaths|cases))\b",
     "unsourced population statistic"),
    (r"\b(scientists? (have )?(proved|proven)|proof that)\b",
     "overstated scientific certainty"),
    (r"\b(government|they) (are )?(hiding|covering up)\b",
     "conspiracy framing"),
]

# Numbers and dates are the most common LLM hallucination surface.
NUMERIC_CLAIM = re.compile(
    r"\b\d[\d,\.]*\s*(%|percent|million|billion|trillion|years?|km|miles|"
    r"kg|tons?|degrees|light[- ]years?|bce?|ad|ce)\b", re.I)
NAMED_STUDY = re.compile(
    r"\b(study|studies|research|researchers?|scientists?|survey|report|paper|"
    r"trial|experiment)\b", re.I)


@dataclass
class FactCheckResult:
    risk: str = "low"                     # low | medium | high
    requires_approval: bool = False
    flagged: list[dict[str, Any]] = field(default_factory=list)
    unverified_claims: list[str] = field(default_factory=list)
    confidence_summary: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"risk": self.risk, "requires_approval": self.requires_approval,
                "flagged": self.flagged,
                "unverified_claims": self.unverified_claims,
                "confidence_summary": self.confidence_summary,
                "notes": self.notes}


class FactChecker:
    """Grades the claims the script makes. Does NOT invent verification.

    For factual niches the pipeline separates research from generation, so the
    script arrives with a `claims` array. This grades those claims and scans the
    narration independently, because a model that hallucinates a statistic will
    also happily omit it from its own claims list.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.factual_niches = {
            n.lower() for n in cfg.get("content.fact_check_required_niches", []) or []}

    def check(self, script: Script, profile: NicheProfile) -> FactCheckResult:
        result = FactCheckResult()
        text = script.script or ""
        needs_check = (profile.requires_fact_check
                       or profile.name.lower() in self.factual_niches
                       or profile.is_sensitive)

        # --- 1. Hard policy patterns (always scanned) -----------------
        for pattern, label in HIGH_RISK_PATTERNS:
            for match in re.finditer(pattern, text, re.I):
                result.flagged.append({
                    "type": label, "severity": "high",
                    "excerpt": text[max(match.start() - 40, 0):match.end() + 40].strip(),
                })

        # --- 2. Claim confidence from the generator -------------------
        counts = {"high": 0, "medium": 0, "low": 0, "unstated": 0}
        for claim in script.claims or []:
            conf = str(claim.get("confidence", "unstated")).lower()
            conf = conf if conf in counts else "unstated"
            counts[conf] += 1
            if conf in {"low", "unstated"}:
                result.unverified_claims.append(str(claim.get("claim", ""))[:200])
        result.confidence_summary = counts

        # --- 3. Independent scan for unsupported specifics ------------
        if needs_check:
            numbers = NUMERIC_CLAIM.findall(text)
            declared = " ".join(str(c.get("claim", "")) for c in script.claims or [])
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                if not NUMERIC_CLAIM.search(sentence):
                    continue
                # A number is "supported" if the claims array mentions it.
                digits = re.findall(r"\d[\d,\.]*", sentence)
                if digits and not any(d in declared for d in digits):
                    result.flagged.append({
                        "type": "numeric claim not declared in claims array",
                        "severity": "medium", "excerpt": sentence.strip()[:200]})
            if NAMED_STUDY.search(text) and not script.sources:
                result.flagged.append({
                    "type": "references research but lists no sources",
                    "severity": "medium",
                    "excerpt": "script mentions studies/researchers"})
            if numbers:
                result.notes.append(
                    f"{len(numbers)} numeric/dated specifics present - verify "
                    f"before publishing a factual video.")

        # --- 4. Provider quality -------------------------------------
        if script.provider == "template":
            result.flagged.append({
                "type": "script generated without an LLM (template fallback)",
                "severity": "medium",
                "excerpt": "prose quality and factual depth are degraded"})

        # --- 5. Verdict ----------------------------------------------
        high = sum(1 for f in result.flagged if f["severity"] == "high")
        medium = sum(1 for f in result.flagged if f["severity"] == "medium")
        if high:
            result.risk = "high"
        elif medium >= 2 or (medium and profile.is_sensitive):
            result.risk = "medium"
        elif medium:
            result.risk = "medium" if needs_check else "low"
        else:
            result.risk = "low"

        result.requires_approval = (
            result.risk == "high"
            or (result.risk == "medium" and profile.is_sensitive)
            or bool(profile.made_for_kids and result.flagged))

        log_event("FACTCHECK", f"risk {result.risk}",
                  flagged=len(result.flagged),
                  unverified=len(result.unverified_claims),
                  approval=result.requires_approval)
        return result
