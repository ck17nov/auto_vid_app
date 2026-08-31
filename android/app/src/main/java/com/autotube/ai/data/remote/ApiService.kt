package com.autotube.ai.data.remote

import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

/** Backend contract. Mirrors backend/api/main.py exactly. */
interface ApiService {

    @GET("health")
    suspend fun health(): HealthDto

    @GET("niche/preview")
    suspend fun nichePreview(
        @Query("niche") niche: String,
        @Query("audience") audience: String = "18-35",
        @Query("style") style: String = "",
        @Query("duration") duration: Int = 45,
    ): NichePreviewDto

    @POST("automations")
    suspend fun createAutomation(@Body body: AutomationRequestDto): AutomationAcceptedDto

    @GET("jobs")
    suspend fun jobs(
        @Query("status") status: String = "",
        @Query("limit") limit: Int = 30,
    ): JobListDto

    @GET("jobs/{jobId}")
    suspend fun job(@Path("jobId") jobId: String): JobDetailDto

    @POST("jobs/{jobId}/approve")
    suspend fun approve(@Path("jobId") jobId: String): SimpleAckDto

    @POST("jobs/{jobId}/reject")
    suspend fun reject(
        @Path("jobId") jobId: String,
        @Body body: RejectBodyDto,
    ): SimpleAckDto

    @GET("research")
    suspend fun research(
        @Query("niche") niche: String,
        @Query("video_format") videoFormat: String = "SHORT",
        @Query("limit") limit: Int = 20,
    ): ResearchDto

    @GET("analytics")
    suspend fun analytics(
        @Query("days") days: Int = 28,
        @Query("collect") collect: Boolean = false,
    ): AnalyticsDto

    @GET("quota")
    suspend fun quota(): QuotaDto

    @GET("youtube/status")
    suspend fun youtubeStatus(): YouTubeStatusDto

    @POST("youtube/token")
    suspend fun sendRefreshToken(@Body body: TokenBodyDto): SimpleAckDto
}
