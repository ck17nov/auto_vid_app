"""Content idea generation (spec sections 5 & 6).

The gap engine says WHERE the opportunity is; this turns it into a concrete,
original concept and scores the opportunity.

Originality is enforced structurally, not just by prompt text: any generated
concept whose title overlaps too heavily with a researched title, or with a
topic this channel already published, is rejected before it can reach scripting.
"""
from __future__ import annotations

from ..core.config import Config
from ..core.db import Database
from ..core.logging import log_event
from ..core.models import ContentGap, ContentIdea, ResearchVideo, TopicCluster
from ..core.niche import NicheProfile
from ..core.util import agree, clamp, token_overlap, truncate, words
from .llm import LLMError, LLMRouter

SYSTEM_PROMPT = """You are a YouTube content strategist. You find the angle a \
topic is missing and turn it into a concept that is genuinely new.

Rules:
- Never propose a concept that is a reworded version of an existing title.
- The angle must be defensible: something true and interesting, not a stunt.
- No deceptive framing. If the payoff cannot deliver, do not promise it.
- Prefer specificity over scale: one surprising concrete thing beats "10 facts".
- Output valid JSON only."""

# How much title overlap with an existing video is too much.
MAX_TITLE_OVERLAP = 0.62


class IdeaGenerator:
    def __init__(self, cfg: Config, router: LLMRouter | None = None,
                 db: Database | None = None):
        self.cfg = cfg
        self.db = db
        self.router = router or LLMRouter(
            list(cfg.get("content.llm_provider_order",
                         ["groq", "gemini", "ollama", "template"])), cfg)

    # ------------------------------------------------------------------
    def generate(self, niche: str, profile: NicheProfile,
                 videos: list[ResearchVideo], clusters: list[TopicCluster],
                 gaps: list[ContentGap], *, count: int | None = None,
                 research_context: str = "",
                 strategy_hints: str = "") -> list[ContentIdea]:
        count = count or int(self.cfg.get("content.max_ideas", 10))
        try:
            ideas = self._generate_llm(niche, profile, gaps, clusters, count,
                                       research_context, strategy_hints)
        except LLMError as exc:
            log_event("IDEA", "LLM unavailable, using structural builder",
                      error=str(exc)[:180])
            ideas = build_structural_ideas(niche, profile, gaps, clusters, count)

        ideas = self._enforce_originality(ideas, videos, niche)
        ideas = self._score(ideas, gaps, clusters, profile)

        if self.db is not None:
            for idea in ideas:
                self.db.save_idea(niche, idea)
        log_event("IDEA", f"{len(ideas)} ideas generated", niche=niche,
                  best=f"{ideas[0].opportunity_score:.1f}" if ideas else "n/a")
        return ideas

    # ------------------------------------------------------------------
    def _generate_llm(self, niche: str, profile: NicheProfile,
                      gaps: list[ContentGap], clusters: list[TopicCluster],
                      count: int, research_context: str,
                      strategy_hints: str) -> list[ContentIdea]:
        gap_block = "\n".join(
            f"- topic '{g.topic}' (gap score {g.gap_score:.2f})\n"
            f"    already covered: {', '.join(g.common_angles[:6]) or 'nothing clear'}\n"
            f"    NOT covered: {'; '.join(g.missing_angles[:4])}\n"
            f"    open questions: {'; '.join(g.unanswered_questions[:3])}\n"
            f"    context: {g.audience_curiosity}"
            for g in gaps[:5]) or "No structured gaps available."

        kids_line = ("\nThis is CHILD-DIRECTED content: nothing scary, no danger, "
                     "no conflict, no romance. Wonder and warmth only.\n"
                     if profile.made_for_kids else "")

        prompt = f"""Propose {count} original video concepts for this channel.

{profile.prompt_block()}
{kids_line}
{research_context}

STRUCTURAL GAP ANALYSIS:
{gap_block}

{strategy_hints}

For each concept:
- Pick a gap and take an angle NONE of the researched videos take.
- The working_title must not reuse the wording of any researched title.
- hook_concept is the first spoken line: max 12 words, creates one unresolved
  question, contains no greeting.
- Flag any concept needing careful factual handling in risk_flags.

Return JSON:
{{"ideas": [
  {{"topic": "the subject",
    "angle": "the specific new angle, one sentence",
    "working_title": "8-12 words, no ALL CAPS, no false promise",
    "hook_concept": "the actual opening line",
    "hook_type": "question|shock|countdown|story|myth|reveal|mechanism",
    "why_now": "why this is timely, one sentence",
    "originality_note": "how this differs from what already exists",
    "risk_flags": ["factual_sensitive"]}}
]}}"""

        data, provider = self.router.complete_json(
            prompt, system=SYSTEM_PROMPT,
            temperature=float(self.cfg.get("content.temperature", 0.85)),
            max_tokens=4096)
        raw = data.get("ideas") or []
        if not isinstance(raw, list) or not raw:
            raise LLMError("LLM returned no ideas")

        ideas: list[ContentIdea] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            title = str(item.get("working_title") or "").strip()
            topic = str(item.get("topic") or "").strip()
            if not (title or topic):
                continue
            flags = item.get("risk_flags") or []
            if isinstance(flags, str):
                flags = [flags]
            ideas.append(ContentIdea(
                topic=topic or title,
                angle=str(item.get("angle") or "").strip(),
                working_title=truncate(title or topic, 100),
                hook_concept=str(item.get("hook_concept") or "").strip(),
                hook_type=str(item.get("hook_type") or "reveal").lower(),
                why_now=str(item.get("why_now") or "").strip(),
                originality_note=str(item.get("originality_note") or "").strip(),
                risk_flags=[str(f) for f in flags][:6],
                source_topic_cluster=_nearest_cluster(topic or title, clusters),
                inspiration_video_ids=_cluster_video_ids(
                    topic or title, clusters, gaps),
            ))
        if not ideas:
            raise LLMError("no usable ideas after parsing")
        log_event("IDEA", "ideas from LLM", provider=provider, count=len(ideas))
        return ideas

    # ------------------------------------------------------------------
    def _enforce_originality(self, ideas: list[ContentIdea],
                             videos: list[ResearchVideo],
                             niche: str) -> list[ContentIdea]:
        """Reject near-duplicates of researched titles or our own past topics."""
        existing_titles = [v.title for v in videos]
        past_topics = self.db.used_topics(niche) if self.db is not None else []

        kept: list[ContentIdea] = []
        for idea in ideas:
            # Compare the ANGLE, not the subject.
            #
            # Every video about black holes shares the words "black holes" with
            # every other one - that is the topic, not plagiarism. Including
            # subject nouns in the comparison rejected every legitimate idea
            # ("What Most People Get Wrong About Black Holes" scored 0.67
            # against "10 Facts About Black Holes"). Stripping the topic tokens
            # leaves only the distinctive framing, which is what originality
            # actually means here.
            topic_tokens = set(words(idea.topic))
            probe = _without(f"{idea.working_title} {idea.topic}", topic_tokens)
            worst = 0.0
            worst_title = ""
            for title in existing_titles:
                overlap = token_overlap(probe, _without(title, topic_tokens))
                if overlap > worst:
                    worst, worst_title = overlap, title
            if worst >= MAX_TITLE_OVERLAP:
                log_event("IDEA", "rejected - too close to an existing video",
                          title=idea.working_title[:60],
                          overlap=f"{worst:.2f}", against=worst_title[:60])
                continue

            repeat = max((token_overlap(idea.topic, t) for t in past_topics),
                         default=0.0)
            if repeat >= float(self.cfg.get(
                    "automation.duplicate_similarity_threshold", 0.80)):
                log_event("IDEA", "rejected - we already covered this",
                          topic=idea.topic[:60], overlap=f"{repeat:.2f}")
                continue

            # Also reject internal duplicates within this batch, again on the
            # angle rather than the shared subject.
            if any(token_overlap(
                    probe, _without(f"{k.working_title} {k.topic}", topic_tokens)
                   ) >= 0.75 for k in kept):
                continue
            kept.append(idea)
        if not kept and ideas:
            # Never hard-fail: keep the single most distinct idea and flag it.
            best = min(ideas, key=lambda i: max(
                (token_overlap(
                    _without(f"{i.working_title} {i.topic}", set(words(i.topic))),
                    _without(t, set(words(i.topic))))
                 for t in existing_titles), default=0.0))
            best.risk_flags = list({*best.risk_flags, "originality_review"})
            log_event("IDEA", "all ideas were near-duplicates; keeping the most "
                              "distinct one flagged for review")
            kept = [best]
        return kept

    # ------------------------------------------------------------------
    def _score(self, ideas: list[ContentIdea], gaps: list[ContentGap],
               clusters: list[TopicCluster],
               profile: NicheProfile) -> list[ContentIdea]:
        gap_by_topic = {g.topic: g for g in gaps}
        cluster_by_topic = {c.topic: c for c in clusters}
        strategy = (self.db.strategy("hook_type") if self.db is not None else {})

        for idea in ideas:
            gap = gap_by_topic.get(idea.source_topic_cluster)
            cluster = cluster_by_topic.get(idea.source_topic_cluster)

            gap_component = gap.gap_score if gap else 0.35
            momentum = cluster.momentum if cluster else 0.35
            # Hook strength: short, specific, question-or-reveal shaped.
            hook = idea.hook_concept or ""
            hook_words = len(hook.split())
            hook_component = clamp(
                (0.55 if 3 <= hook_words <= 12 else 0.25)
                + (0.20 if any(ch in hook for ch in "?") else 0.0)
                + (0.15 if any(c.isdigit() for c in hook) else 0.0)
                + (0.10 if idea.angle else 0.0))
            # Learned preference from published performance (spec section 25).
            learned = clamp(strategy.get(idea.hook_type, 1.0) / 2.0, 0.0, 1.0)
            specificity = clamp(len(idea.angle.split()) / 24.0)
            risk_penalty = 0.10 if "originality_review" in idea.risk_flags else 0.0

            parts = {
                "content_gap": gap_component,
                "topic_momentum": momentum,
                "hook_strength": hook_component,
                "learned_preference": learned,
                "specificity": specificity,
            }
            weights = {"content_gap": 0.30, "topic_momentum": 0.24,
                       "hook_strength": 0.24, "learned_preference": 0.12,
                       "specificity": 0.10}
            total = sum(parts[k] * weights[k] for k in parts) - risk_penalty
            idea.score_breakdown = {k: round(v, 4) for k, v in parts.items()}
            idea.opportunity_score = round(clamp(total) * 100, 2)

        ideas.sort(key=lambda i: i.opportunity_score, reverse=True)
        return ideas


# --------------------------------------------------------------------------
def _nearest_cluster(text: str, clusters: list[TopicCluster]) -> str:
    best, score = "", 0.0
    for c in clusters:
        s = token_overlap(text, f"{c.topic} {' '.join(c.keywords)}")
        if s > score:
            best, score = c.topic, s
    return best or (clusters[0].topic if clusters else "")


def _cluster_video_ids(text: str, clusters: list[TopicCluster],
                       gaps: list[ContentGap]) -> list[str]:
    topic = _nearest_cluster(text, clusters)
    for c in clusters:
        if c.topic == topic:
            return list(c.video_ids[:6])
    return []


def build_structural_ideas(niche: str, profile: NicheProfile,
                           gaps: list[ContentGap], clusters: list[TopicCluster],
                           count: int) -> list[ContentIdea]:
    """LLM-free idea builder from the structural gap analysis.

    Degraded but real: it names the topic, states which angle is missing, and
    forms a hook from the unanswered question.  Marked `template_idea` so the
    quality gate and the UI both know an LLM was not involved.
    """
    ideas: list[ContentIdea] = []
    # (hook_type, hook_template, title_template).
    # Templates are deliberately number-agnostic: the topic label can be
    # singular ("neutron star") or plural ("black holes"), so nothing here may
    # depend on subject-verb agreement.
    angle_hooks: dict[str, tuple[str, str, str]] = {
        "correction": ("myth",
                       "Most explanations of {t} get one part wrong.",
                       "What Most People Get Wrong About {T}"),
        "mechanism": ("mechanism",
                      "Nobody explains what actually makes {t} work.",
                      "The Mechanism Behind {T}"),
        "consequence": ("reveal",
                        "The consequence of {t} is stranger than {t}.",
                        "The Consequence Of {T}"),
        "hidden": ("reveal",
                   "There is a part of {t} almost nobody mentions.",
                   "The Part Of {T} Nobody Explains"),
        "origin": ("story",
                   "The first person to notice {t} was not looking for it.",
                   "Where {T} Came From"),
        "human_story": ("story",
                        "One team changed how we understand {t}.",
                        "The Team That Changed {T}"),
        "extreme": ("shock",
                    "The most extreme case of {t} should not be possible.",
                    "The Most Extreme {T} Ever Found"),
        "future": ("question",
                   "What happens to {t} next is already being tested.",
                   "What Comes Next For {T}"),
        "comparison": ("question",
                       "Two things called {t} behave nothing alike.",
                       "Not All {T} Are The Same"),
        "explainer": ("question",
                      "What {v} {t}, in plain terms?",
                      "{T}, Explained Plainly"),
        "practical": ("question",
                      "How do you actually apply {t}?",
                      "How To Actually Use {T}"),
        "listicle": ("countdown",
                     "Three things about {t} that do not fit.",
                     "Three Things About {T} That Do Not Fit"),
    }

    for gap in gaps:
        for missing in gap.missing_angles:
            if len(ideas) >= count:
                break
            key = missing.split(":")[0]
            hook_type, hook_template, title_template = angle_hooks.get(
                key, ("reveal", "There is more to {t} than the usual story.",
                      "There Is More To {T}"))
            topic = gap.topic
            ideas.append(ContentIdea(
                topic=topic,
                angle=missing.split(":", 1)[-1].strip(),
                working_title=title_template.format(T=_title_case(topic)),
                # `v` supplies the agreeing verb for hooks that need one, so a
                # plural topic never produces "What is animals".
                hook_concept=hook_template.format(
                    t=topic, v=agree(topic, "is", "are")),
                hook_type=hook_type,
                # Human-readable: this field can end up in prompts and, in the
                # LLM-free path, near the narration. Never put counts, quoted
                # keywords or metric names here.
                why_now=(f"The usual framing of {topic} is well covered, "
                         f"so this angle is still open."),
                originality_note=("derived from structural gap analysis without "
                                  "an LLM"),
                risk_flags=["template_idea"],
                source_topic_cluster=gap.topic,
                inspiration_video_ids=_cluster_video_ids(topic, clusters, gaps),
            ))
        if len(ideas) >= count:
            break

    if not ideas:
        ideas.append(ContentIdea(
            topic=niche, angle=f"an overlooked detail in {niche}",
            working_title=f"The Part Of {niche.title()} Nobody Explains",
            hook_concept=f"There is one thing about {niche} that does not add up.",
            hook_type="reveal", why_now="no research signal was available",
            risk_flags=["template_idea", "no_research"],
            originality_note="fallback concept - no research data available"))
    return ideas[:count]


def _title_case(text: str) -> str:
    """Title-case a topic label without mangling acronyms."""
    out = []
    for word in (text or "").split():
        out.append(word if word.isupper() and len(word) <= 5 else word.capitalize())
    return " ".join(out)


def _without(text: str, drop: set[str]) -> str:
    """Text with the given tokens removed - used to compare angles, not topics."""
    return " ".join(w for w in words(text) if w not in drop)
