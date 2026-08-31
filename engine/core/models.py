"""Domain models for the whole pipeline (plain dataclasses -> easy JSON round-trip)."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------
# Job lifecycle (spec section 21)
# --------------------------------------------------------------------------
class JobStatus(str, Enum):
    IDEA = "IDEA"
    RESEARCH = "RESEARCH"
    SCRIPT = "SCRIPT"
    VOICE = "VOICE"
    VISUALS = "VISUALS"
    RENDERING = "RENDERING"
    QUALITY_CHECK = "QUALITY_CHECK"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    ANALYZING = "ANALYZING"
    FAILED = "FAILED"
    REJECTED = "REJECTED"

    @property
    def terminal(self) -> bool:
        return self in {JobStatus.PUBLISHED, JobStatus.FAILED, JobStatus.REJECTED}


ORDERED_STATUSES = [
    JobStatus.IDEA, JobStatus.RESEARCH, JobStatus.SCRIPT, JobStatus.VOICE,
    JobStatus.VISUALS, JobStatus.RENDERING, JobStatus.QUALITY_CHECK,
    JobStatus.AWAITING_APPROVAL, JobStatus.READY, JobStatus.SCHEDULED,
    JobStatus.PUBLISHED, JobStatus.ANALYZING,
]


class VideoFormat(str, Enum):
    SHORT = "SHORT"        # 1080x1920, up to 180s
    LONGFORM = "LONGFORM"  # 1920x1080


class Mode(str, Enum):
    AUTO = "AUTO"
    APPROVAL = "APPROVAL"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


class JsonMixin:
    def to_dict(self) -> dict[str, Any]:
        def conv(o: Any) -> Any:
            if isinstance(o, Enum):
                return o.value
            if isinstance(o, dict):
                return {k: conv(v) for k, v in o.items()}
            if isinstance(o, (list, tuple)):
                return [conv(v) for v in o]
            return o
        return conv(asdict(self))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]):
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})


# --------------------------------------------------------------------------
# Automation request (what the user enters in the app)
# --------------------------------------------------------------------------
@dataclass
class AutomationRequest(JsonMixin):
    niche: str = "science"
    audience: str = "18-35"
    language: str = "en"
    video_format: str = VideoFormat.SHORT.value
    duration_seconds: int = 45
    style: str = "fast-paced, curiosity-driven"
    count: int = 1
    mode: str = Mode.APPROVAL.value
    # scheduling
    frequency: str = "daily"                         # daily | weekly | days | once
    days: list[int] = field(default_factory=list)    # 0=Mon .. 6=Sun
    upload_time: str = "20:00"
    timezone: str = "Asia/Kolkata"
    made_for_kids: bool = False
    keywords: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: new_id("auto"))


# --------------------------------------------------------------------------
# Research
# --------------------------------------------------------------------------
@dataclass
class ResearchVideo(JsonMixin):
    video_id: str
    title: str
    channel_id: str
    channel_title: str
    published_at: str
    duration_seconds: int
    views: int = 0
    likes: int = 0
    comments: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: str = ""
    channel_subscribers: int = 0
    channel_video_count: int = 0
    channel_total_views: int = 0
    thumbnail_url: str = ""
    is_short: bool = False
    age_days: float = 0.0
    # derived signals
    view_velocity: float = 0.0        # views per day
    engagement_rate: float = 0.0      # (likes + comments) / views
    performance_ratio: float = 0.0    # views / expected-views-for-channel
    is_breakout: bool = False
    viral_score: float = 0.0
    ctr_potential_score: float = 0.0  # NOT real CTR - observable signals only
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class TopicCluster(JsonMixin):
    topic: str
    keywords: list[str] = field(default_factory=list)
    video_ids: list[str] = field(default_factory=list)
    total_views: int = 0
    avg_velocity: float = 0.0
    momentum: float = 0.0
    breakout_count: int = 0
    title_patterns: list[str] = field(default_factory=list)
    example_titles: list[str] = field(default_factory=list)


@dataclass
class ContentGap(JsonMixin):
    topic: str
    common_angles: list[str] = field(default_factory=list)
    missing_angles: list[str] = field(default_factory=list)
    unanswered_questions: list[str] = field(default_factory=list)
    audience_curiosity: str = ""
    gap_score: float = 0.0


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------
@dataclass
class ContentIdea(JsonMixin):
    idea_id: str = field(default_factory=lambda: new_id("idea"))
    topic: str = ""
    angle: str = ""
    working_title: str = ""
    hook_concept: str = ""
    hook_type: str = ""               # question | shock | countdown | story | myth
    why_now: str = ""
    opportunity_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    inspiration_video_ids: list[str] = field(default_factory=list)
    source_topic_cluster: str = ""
    originality_note: str = ""
    risk_flags: list[str] = field(default_factory=list)


@dataclass
class Scene(JsonMixin):
    index: int
    narration: str
    visual_prompt: str = ""
    visual_keywords: list[str] = field(default_factory=list)
    on_screen_text: str = ""
    role: str = "value"               # hook | context | value | payoff | cta
    # filled in after TTS / render planning
    start: float = 0.0
    duration: float = 0.0
    asset_path: str = ""
    motion: str = "zoom_in"


@dataclass
class Script(JsonMixin):
    script_id: str = field(default_factory=lambda: new_id("scr"))
    idea_id: str = ""
    title_ideas: list[str] = field(default_factory=list)
    hook: str = ""
    script: str = ""
    scenes: list[dict[str, Any]] = field(default_factory=list)
    visual_plan: list[str] = field(default_factory=list)
    voice_style: str = "energetic"
    cta: str = ""
    estimated_duration: float = 0.0
    language: str = "en"
    sources: list[dict[str, str]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    retention_score: float = 0.0
    retention_notes: list[str] = field(default_factory=list)
    # Long-form only: section boundaries from the outline, as
    # {"heading": str, "scene_index": int}. Used for real YouTube chapters
    # instead of guessing labels from narration.
    chapters: list[dict[str, Any]] = field(default_factory=list)

    def scene_objects(self) -> list[Scene]:
        out = []
        for s in self.scenes:
            if isinstance(s, Scene):
                out.append(s)
            else:
                names = set(Scene.__dataclass_fields__)
                out.append(Scene(**{k: v for k, v in s.items() if k in names}))
        return out


@dataclass
class Asset(JsonMixin):
    asset: str                        # filename
    source: str                       # generated | provider:<name> | procedural
    license: str
    prompt: str = ""
    attribution: str = ""
    url: str = ""
    scene_index: int = -1


@dataclass
class VideoMetadata(JsonMixin):
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: str = "27"
    privacy: str = "private"
    publish_at: str | None = None     # RFC3339 UTC
    made_for_kids: bool = False
    synthetic_disclosure: bool = True
    title_score: float = 0.0
    title_candidates: list[dict[str, Any]] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    playlist_id: str = ""
    language: str = "en"


@dataclass
class QualityReport(JsonMixin):
    score: float = 0.0
    passed: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    policy_risk: str = "low"
    originality_ok: bool = True


@dataclass
class VideoJob(JsonMixin):
    job_id: str = field(default_factory=lambda: new_id("job"))
    automation_id: str = ""
    status: str = JobStatus.IDEA.value
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)
    error: str = ""
    retry_count: int = 0
    request: dict[str, Any] = field(default_factory=dict)
    idea: dict[str, Any] = field(default_factory=dict)
    script: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    assets: list[dict[str, Any]] = field(default_factory=list)
    dir: str = ""
    video_path: str = ""
    thumbnail_path: str = ""
    voice_path: str = ""
    subtitle_path: str = ""
    youtube_video_id: str = ""
    scheduled_for: str = ""
    published_at: str = ""
    logs: list[str] = field(default_factory=list)
