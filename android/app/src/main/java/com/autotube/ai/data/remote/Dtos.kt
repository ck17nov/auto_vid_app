package com.autotube.ai.data.remote

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

/**
 * Wire models for the AutoTube backend.
 *
 * Every field has a default so a backend that grows a new field, or omits an
 * optional one, never crashes the app. `ignoreUnknownKeys` is also enabled on
 * the Json instance in [ApiClient].
 */

@Serializable
data class HealthDto(
    val ok: Boolean = false,
    val version: String = "",
    val ffmpeg: Boolean = false,
    @SerialName("dry_run") val dryRun: Boolean = true,
    @SerialName("upload_enabled") val uploadEnabled: Boolean = false,
    @SerialName("approval_required") val approvalRequired: Boolean = true,
    @SerialName("llm_providers") val llmProviders: List<String> = emptyList(),
    @SerialName("tts_providers") val ttsProviders: List<String> = emptyList(),
    @SerialName("research_configured") val researchConfigured: Boolean = false,
    @SerialName("auth_required") val authRequired: Boolean = false,
    @SerialName("queue_depth") val queueDepth: Int = 0,
)

@Serializable
data class AutomationRequestDto(
    val niche: String,
    val audience: String = "18-35",
    val language: String = "en",
    @SerialName("video_format") val videoFormat: String = "SHORT",
    @SerialName("duration_seconds") val durationSeconds: Int = 45,
    val style: String = "fast-paced, curiosity-driven",
    val count: Int = 1,
    val mode: String = "APPROVAL",
    val frequency: String = "once",
    val days: List<Int> = emptyList(),
    @SerialName("upload_time") val uploadTime: String = "",
    val timezone: String = "Asia/Kolkata",
    @SerialName("made_for_kids") val madeForKids: Boolean = false,
    val keywords: List<String> = emptyList(),
)

@Serializable
data class AutomationAcceptedDto(
    val accepted: Boolean = false,
    @SerialName("automation_id") val automationId: String = "",
    val queued: Int = 0,
    val note: String = "",
)

@Serializable
data class JobSummaryDto(
    @SerialName("job_id") val jobId: String = "",
    val status: String = "",
    @SerialName("created_at") val createdAt: Double = 0.0,
    @SerialName("updated_at") val updatedAt: Double = 0.0,
    val niche: String = "",
    val title: String = "",
    @SerialName("quality_score") val qualityScore: Double = 0.0,
    @SerialName("quality_passed") val qualityPassed: Boolean = false,
    val blockers: List<String> = emptyList(),
    @SerialName("retention_score") val retentionScore: Double = 0.0,
    val duration: Double = 0.0,
    @SerialName("youtube_video_id") val youtubeVideoId: String = "",
    @SerialName("scheduled_for") val scheduledFor: String = "",
    val error: String = "",
    @SerialName("retry_count") val retryCount: Int = 0,
    @SerialName("has_video") val hasVideo: Boolean = false,
    @SerialName("has_thumbnail") val hasThumbnail: Boolean = false,
)

@Serializable
data class JobListDto(
    @SerialName("queue_depth") val queueDepth: Int = 0,
    val jobs: List<JobSummaryDto> = emptyList(),
)

@Serializable
data class MediaLinksDto(
    val video: String? = null,
    val thumbnail: String? = null,
    val subtitle: String? = null,
    val voice: String? = null,
)

@Serializable
data class JobDetailDto(
    @SerialName("job_id") val jobId: String = "",
    val status: String = "",
    val error: String = "",
    @SerialName("retry_count") val retryCount: Int = 0,
    val request: JsonElement? = null,
    val idea: JsonElement? = null,
    val script: JsonElement? = null,
    val metadata: JsonElement? = null,
    val quality: JsonElement? = null,
    val assets: JsonElement? = null,
    val logs: List<String> = emptyList(),
    val media: MediaLinksDto = MediaLinksDto(),
    @SerialName("youtube_video_id") val youtubeVideoId: String = "",
    @SerialName("scheduled_for") val scheduledFor: String = "",
)

// ---- Research -----------------------------------------------------------
@Serializable
data class ResearchVideoDto(
    @SerialName("video_id") val videoId: String = "",
    val title: String = "",
    @SerialName("channel_title") val channelTitle: String = "",
    val views: Long = 0,
    val likes: Long = 0,
    val comments: Long = 0,
    @SerialName("age_days") val ageDays: Double = 0.0,
    @SerialName("view_velocity") val viewVelocity: Double = 0.0,
    @SerialName("engagement_rate") val engagementRate: Double = 0.0,
    @SerialName("performance_ratio") val performanceRatio: Double = 0.0,
    @SerialName("is_breakout") val isBreakout: Boolean = false,
    @SerialName("viral_score") val viralScore: Double = 0.0,
    /** Heuristic over public signals. NOT another channel's real CTR. */
    @SerialName("ctr_potential_score") val ctrPotentialScore: Double = 0.0,
    @SerialName("thumbnail_url") val thumbnailUrl: String = "",
    @SerialName("is_short") val isShort: Boolean = false,
)

@Serializable
data class TopicClusterDto(
    val topic: String = "",
    val keywords: List<String> = emptyList(),
    @SerialName("video_ids") val videoIds: List<String> = emptyList(),
    val momentum: Double = 0.0,
    @SerialName("breakout_count") val breakoutCount: Int = 0,
    @SerialName("title_patterns") val titlePatterns: List<String> = emptyList(),
    @SerialName("example_titles") val exampleTitles: List<String> = emptyList(),
)

@Serializable
data class ContentGapDto(
    val topic: String = "",
    @SerialName("common_angles") val commonAngles: List<String> = emptyList(),
    @SerialName("missing_angles") val missingAngles: List<String> = emptyList(),
    @SerialName("unanswered_questions") val unansweredQuestions: List<String> = emptyList(),
    @SerialName("audience_curiosity") val audienceCuriosity: String = "",
    @SerialName("gap_score") val gapScore: Double = 0.0,
)

@Serializable
data class ResearchDto(
    val niche: String = "",
    @SerialName("quota_used_today") val quotaUsedToday: Int = 0,
    @SerialName("quota_limit") val quotaLimit: Int = 10000,
    val videos: List<ResearchVideoDto> = emptyList(),
    val breakouts: List<String> = emptyList(),
    val clusters: List<TopicClusterDto> = emptyList(),
    val gaps: List<ContentGapDto> = emptyList(),
    val disclaimer: String = "",
)

// ---- Analytics ----------------------------------------------------------
@Serializable
data class AnalyticsRowDto(
    @SerialName("youtube_video_id") val videoId: String = "",
    val views: Long = 0,
    @SerialName("avg_view_percentage") val avgViewPercentage: Double = 0.0,
    val ctr: Double = 0.0,
    @SerialName("subscribers_gained") val subscribersGained: Long = 0,
    val likes: Long = 0,
    val comments: Long = 0,
)

@Serializable
data class StrategyInsightDto(
    val dimension: String = "",
    val value: String = "",
    val samples: Int = 0,
    val weight: Double = 1.0,
    @SerialName("avg_views") val avgViews: Double = 0.0,
    @SerialName("avg_retention") val avgRetention: Double = 0.0,
)

@Serializable
data class StrategyReportDto(
    val method: String = "",
    @SerialName("min_samples_to_apply") val minSamples: Int = 3,
    val insights: List<StrategyInsightDto> = emptyList(),
    val hints: String = "",
)

@Serializable
data class AnalyticsDto(
    val videos: List<AnalyticsRowDto> = emptyList(),
    val strategy: StrategyReportDto = StrategyReportDto(),
    val note: String = "",
)

// ---- Misc ---------------------------------------------------------------
@Serializable
data class QuotaDto(
    @SerialName("used_today") val usedToday: Int = 0,
    val limit: Int = 10000,
    @SerialName("reserved_for_uploads") val reservedForUploads: Int = 0,
    @SerialName("available_for_research") val availableForResearch: Int = 0,
    @SerialName("max_uploads_per_day") val maxUploadsPerDay: Int = 6,
    val resets: String = "",
)

@Serializable
data class NicheProfileDto(
    val name: String = "",
    val audience: String = "",
    val tone: String = "",
    @SerialName("visual_style") val visualStyle: String = "",
    val pacing: String = "",
    @SerialName("hook_style") val hookStyle: String = "",
    @SerialName("scene_seconds") val sceneSeconds: Double = 0.0,
    @SerialName("words_per_second") val wordsPerSecond: Double = 0.0,
    @SerialName("requires_fact_check") val requiresFactCheck: Boolean = false,
    @SerialName("is_sensitive") val isSensitive: Boolean = false,
    @SerialName("made_for_kids") val madeForKids: Boolean = false,
    val restrictions: List<String> = emptyList(),
    val disclaimers: List<String> = emptyList(),
)

@Serializable
data class NichePreviewDto(
    val profile: NicheProfileDto = NicheProfileDto(),
    @SerialName("kids_niche_detected") val kidsNicheDetected: Boolean = false,
    @SerialName("requires_kids_confirmation") val requiresKidsConfirmation: Boolean = false,
)

@Serializable
data class YouTubeChannelDto(
    @SerialName("channel_id") val channelId: String = "",
    val title: String = "",
    @SerialName("custom_url") val customUrl: String = "",
    val thumbnail: String = "",
    val subscribers: Long = 0,
    val videos: Long = 0,
    val views: Long = 0,
)

@Serializable
data class YouTubeStatusDto(
    val configured: Boolean = false,
    val authorized: Boolean = false,
    val channels: List<YouTubeChannelDto> = emptyList(),
    val error: String = "",
)

@Serializable
data class TokenBodyDto(@SerialName("refresh_token") val refreshToken: String)

@Serializable
data class RejectBodyDto(val reason: String = "")

@Serializable
data class SimpleAckDto(
    val accepted: Boolean = false,
    val stored: Boolean = false,
    val authorized: Boolean = false,
    @SerialName("job_id") val jobId: String = "",
    val status: String = "",
)
