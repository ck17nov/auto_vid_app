package com.autotube.ai.data.repo

import com.autotube.ai.data.local.AnalyticsEntity
import com.autotube.ai.data.local.AppDatabase
import com.autotube.ai.data.local.AutomationEntity
import com.autotube.ai.data.local.EventEntity
import com.autotube.ai.data.local.JobEntity
import com.autotube.ai.data.local.ResearchEntity
import com.autotube.ai.data.prefs.SecureStore
import com.autotube.ai.data.remote.ApiClient
import com.autotube.ai.data.remote.AutomationRequestDto
import com.autotube.ai.data.remote.HealthDto
import com.autotube.ai.data.remote.JobDetailDto
import com.autotube.ai.data.remote.NichePreviewDto
import com.autotube.ai.data.remote.QuotaDto
import com.autotube.ai.data.remote.RejectBodyDto
import com.autotube.ai.data.remote.ResearchDto
import com.autotube.ai.data.remote.TokenBodyDto
import com.autotube.ai.data.remote.YouTubeStatusDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.withContext
import retrofit2.HttpException
import java.io.IOException
import java.util.UUID

/**
 * Single entry point for data. UI observes Room; network calls refresh Room.
 *
 * Errors are returned as [Result] rather than thrown, so every screen can show
 * a real message instead of crashing when the backend is unreachable - which is
 * the normal case on mobile data.
 */
class AutoTubeRepository(
    private val db: AppDatabase,
    private val api: ApiClient,
    private val store: SecureStore,
) {

    // ---- observation (offline-first) -----------------------------------
    fun observeJobs(limit: Int = 50): Flow<List<JobEntity>> =
        db.jobs().observeRecent(limit)

    fun observeJobsByStatus(status: String): Flow<List<JobEntity>> =
        db.jobs().observeByStatus(status)

    fun observeAutomations(): Flow<List<AutomationEntity>> =
        db.automations().observeAll()

    fun observeResearch(niche: String): Flow<List<ResearchEntity>> =
        db.research().observeForNiche(niche)

    fun observeAnalytics(): Flow<List<AnalyticsEntity>> = db.analytics().observeAll()

    fun observeEvents(): Flow<List<EventEntity>> = db.events().observeRecent()

    fun countByStatus(status: String): Flow<Int> = db.jobs().countByStatus(status)

    fun countCompletedToday(): Flow<Int> =
        db.jobs().countCompletedSince(System.currentTimeMillis() - 86_400_000L)

    fun totalViews(): Flow<Long?> = db.analytics().totalViews()
    fun averageRetention(): Flow<Double?> = db.analytics().averageRetention()
    fun totalSubscribers(): Flow<Long?> = db.analytics().totalSubscribers()

    // ---- network -------------------------------------------------------
    suspend fun health(): Result<HealthDto> = call { api.service().health() }

    suspend fun nichePreview(
        niche: String,
        audience: String,
        style: String,
        duration: Int,
    ): Result<NichePreviewDto> = call {
        api.service().nichePreview(niche, audience, style, duration)
    }

    suspend fun startAutomation(request: AutomationRequestDto): Result<String> = call {
        val ack = api.service().createAutomation(request)
        // Persist locally so the Scheduler screen works offline.
        db.automations().upsert(
            AutomationEntity(
                id = ack.automationId.ifBlank { UUID.randomUUID().toString() },
                niche = request.niche,
                audience = request.audience,
                language = request.language,
                videoFormat = request.videoFormat,
                durationSeconds = request.durationSeconds,
                style = request.style,
                mode = request.mode,
                frequency = request.frequency,
                days = request.days,
                uploadTime = request.uploadTime,
                timezone = request.timezone,
                madeForKids = request.madeForKids,
                createdAt = System.currentTimeMillis(),
            )
        )
        logEvent("AUTOMATION", "queued ${request.niche} x${request.count}")
        ack.automationId
    }

    suspend fun refreshJobs(limit: Int = 50): Result<Int> = call {
        val response = api.service().jobs(limit = limit)
        val now = System.currentTimeMillis()
        db.jobs().upsertAll(
            response.jobs.map { j ->
                JobEntity(
                    jobId = j.jobId,
                    status = j.status,
                    niche = j.niche,
                    title = j.title,
                    qualityScore = j.qualityScore,
                    qualityPassed = j.qualityPassed,
                    retentionScore = j.retentionScore,
                    duration = j.duration,
                    blockers = j.blockers,
                    youtubeVideoId = j.youtubeVideoId,
                    scheduledFor = j.scheduledFor,
                    error = j.error,
                    retryCount = j.retryCount,
                    hasVideo = j.hasVideo,
                    hasThumbnail = j.hasThumbnail,
                    // Backend timestamps are epoch SECONDS (Python time.time()).
                    updatedAt = if (j.updatedAt > 0) (j.updatedAt * 1000).toLong() else now,
                )
            }
        )
        response.jobs.size
    }

    suspend fun jobDetail(jobId: String): Result<JobDetailDto> =
        call { api.service().job(jobId) }

    suspend fun approve(jobId: String): Result<Unit> = call {
        api.service().approve(jobId)
        logEvent("APPROVAL", "approved $jobId", jobId)
        Unit
    }

    suspend fun reject(jobId: String, reason: String): Result<Unit> = call {
        api.service().reject(jobId, RejectBodyDto(reason))
        logEvent("APPROVAL", "rejected $jobId", jobId)
        Unit
    }

    suspend fun research(niche: String, videoFormat: String): Result<ResearchDto> =
        call {
            val response = api.service().research(niche, videoFormat)
            val now = System.currentTimeMillis()
            db.research().upsertAll(
                response.videos.map { v ->
                    ResearchEntity(
                        videoId = v.videoId,
                        niche = niche,
                        title = v.title,
                        channelTitle = v.channelTitle,
                        views = v.views,
                        viewVelocity = v.viewVelocity,
                        engagementRate = v.engagementRate,
                        performanceRatio = v.performanceRatio,
                        isBreakout = v.isBreakout,
                        viralScore = v.viralScore,
                        ctrPotentialScore = v.ctrPotentialScore,
                        ageDays = v.ageDays,
                        thumbnailUrl = v.thumbnailUrl,
                        fetchedAt = now,
                    )
                }
            )
            response
        }

    suspend fun refreshAnalytics(collect: Boolean = false): Result<Int> = call {
        val response = api.service().analytics(collect = collect)
        val now = System.currentTimeMillis()
        db.analytics().upsertAll(
            response.videos.map { a ->
                AnalyticsEntity(
                    videoId = a.videoId,
                    views = a.views,
                    avgViewPercentage = a.avgViewPercentage,
                    ctr = a.ctr,
                    subscribersGained = a.subscribersGained,
                    likes = a.likes,
                    comments = a.comments,
                    collectedAt = now,
                )
            }
        )
        response.videos.size
    }

    suspend fun quota(): Result<QuotaDto> = call { api.service().quota() }

    suspend fun youtubeStatus(): Result<YouTubeStatusDto> =
        call { api.service().youtubeStatus() }

    /**
     * Hand the OAuth refresh token to the backend so it can upload.
     * The token is stored encrypted on device and never logged.
     *
     * The client id goes with it. A token minted by an Android OAuth client
     * can only be refreshed by that same client, with no secret, because an
     * Android client is a public PKCE client. Sending the token alone left the
     * backend refreshing it with the desktop credentials from .env, which
     * Google rejects - so connecting from the phone looked like it worked and
     * then never uploaded anything.
     */
    suspend fun sendRefreshToken(token: String): Result<Boolean> = call {
        store.refreshToken = token
        api.service().sendRefreshToken(
            TokenBodyDto(token, store.oauthClientId.trim())
        ).stored
    }

    fun mediaUrl(path: String): String = api.mediaUrl(path)
    fun apiKeyHeader(): Pair<String, String>? = api.apiKeyHeader

    // ---- housekeeping --------------------------------------------------
    suspend fun logEvent(tag: String, message: String, jobId: String = "") {
        withContext(Dispatchers.IO) {
            db.events().add(
                EventEntity(tag = tag, message = message, jobId = jobId,
                    at = System.currentTimeMillis())
            )
        }
    }

    suspend fun prune(retainDays: Int = 30) = withContext(Dispatchers.IO) {
        val cutoff = System.currentTimeMillis() - retainDays * 86_400_000L
        db.jobs().pruneOlderThan(cutoff)
        db.events().pruneOlderThan(cutoff)
    }

    /**
     * Wraps a network call so callers get a readable failure instead of an
     * exception type. Distinguishing these matters for the UI: an auth error
     * needs a Settings prompt, a connection error just needs a retry.
     */
    private suspend fun <T> call(block: suspend () -> T): Result<T> =
        withContext(Dispatchers.IO) {
            try {
                Result.success(block())
            } catch (e: HttpException) {
                val message = when (e.code()) {
                    401 -> "Backend rejected the API key. Check Settings."
                    409 -> "Confirmation required (see the message on screen)."
                    429 -> "Backend rate limit reached. Try again shortly."
                    503 -> "Backend is not ready (missing ffmpeg or API keys)."
                    else -> "Backend error ${e.code()}."
                }
                Result.failure(RepositoryException(message, e))
            } catch (e: IOException) {
                Result.failure(
                    RepositoryException(
                        // Include the cause. "Cannot reach the backend" alone
                        // is indistinguishable between no network, DNS
                        // failure, a rejected certificate and an empty URL -
                        // and those need completely different fixes.
                        "Cannot reach ${store.backendUrl.ifBlank { "(no URL set)" }} " +
                            "- ${e.javaClass.simpleName}" +
                            (e.message?.take(80)?.let { ": $it" } ?: ""),
                        e,
                    )
                )
            } catch (e: Exception) {
                Result.failure(RepositoryException(e.message ?: "Unexpected error", e))
            }
        }
}

class RepositoryException(message: String, cause: Throwable? = null) :
    Exception(message, cause)
