package com.autotube.ai.data.local

import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.RoomDatabase
import androidx.room.TypeConverter
import androidx.room.TypeConverters
import kotlinx.coroutines.flow.Flow

/**
 * On-device mirror of the backend state (spec section 27).
 *
 * Why mirror at all: the phone must show the dashboard, the queue and the
 * schedule while offline or while the backend is unreachable. Room is the
 * source of truth for the UI; the backend is the source of truth for jobs.
 *
 * Credentials are NOT here - they live in EncryptedSharedPreferences.
 */

// --------------------------------------------------------------------------
// Entities
// --------------------------------------------------------------------------
@Entity(tableName = "automations")
data class AutomationEntity(
    @PrimaryKey val id: String,
    val niche: String,
    val audience: String,
    val language: String,
    @ColumnInfo(name = "video_format") val videoFormat: String,
    @ColumnInfo(name = "duration_seconds") val durationSeconds: Int,
    val style: String,
    val mode: String,
    val frequency: String,
    val days: List<Int>,
    @ColumnInfo(name = "upload_time") val uploadTime: String,
    val timezone: String,
    @ColumnInfo(name = "made_for_kids") val madeForKids: Boolean,
    @ColumnInfo(name = "created_at") val createdAt: Long,
    val enabled: Boolean = true,
)

@Entity(tableName = "jobs")
data class JobEntity(
    @PrimaryKey @ColumnInfo(name = "job_id") val jobId: String,
    val status: String,
    val niche: String,
    val title: String,
    @ColumnInfo(name = "quality_score") val qualityScore: Double,
    @ColumnInfo(name = "quality_passed") val qualityPassed: Boolean,
    @ColumnInfo(name = "retention_score") val retentionScore: Double,
    val duration: Double,
    val blockers: List<String>,
    @ColumnInfo(name = "youtube_video_id") val youtubeVideoId: String,
    @ColumnInfo(name = "scheduled_for") val scheduledFor: String,
    val error: String,
    @ColumnInfo(name = "retry_count") val retryCount: Int,
    @ColumnInfo(name = "has_video") val hasVideo: Boolean,
    @ColumnInfo(name = "has_thumbnail") val hasThumbnail: Boolean,
    @ColumnInfo(name = "updated_at") val updatedAt: Long,
)

@Entity(tableName = "research_cache")
data class ResearchEntity(
    @PrimaryKey @ColumnInfo(name = "video_id") val videoId: String,
    val niche: String,
    val title: String,
    @ColumnInfo(name = "channel_title") val channelTitle: String,
    val views: Long,
    @ColumnInfo(name = "view_velocity") val viewVelocity: Double,
    @ColumnInfo(name = "engagement_rate") val engagementRate: Double,
    @ColumnInfo(name = "performance_ratio") val performanceRatio: Double,
    @ColumnInfo(name = "is_breakout") val isBreakout: Boolean,
    @ColumnInfo(name = "viral_score") val viralScore: Double,
    @ColumnInfo(name = "ctr_potential_score") val ctrPotentialScore: Double,
    @ColumnInfo(name = "age_days") val ageDays: Double,
    @ColumnInfo(name = "thumbnail_url") val thumbnailUrl: String,
    @ColumnInfo(name = "fetched_at") val fetchedAt: Long,
)

@Entity(tableName = "analytics")
data class AnalyticsEntity(
    @PrimaryKey @ColumnInfo(name = "youtube_video_id") val videoId: String,
    val views: Long,
    @ColumnInfo(name = "avg_view_percentage") val avgViewPercentage: Double,
    /** Own-channel CTR only. Never a competitor's. */
    val ctr: Double,
    @ColumnInfo(name = "subscribers_gained") val subscribersGained: Long,
    val likes: Long,
    val comments: Long,
    @ColumnInfo(name = "collected_at") val collectedAt: Long,
)

@Entity(tableName = "schedules")
data class ScheduleEntity(
    @PrimaryKey val id: String,
    @ColumnInfo(name = "automation_id") val automationId: String,
    @ColumnInfo(name = "job_id") val jobId: String,
    @ColumnInfo(name = "publish_at_utc") val publishAtUtc: String,
    @ColumnInfo(name = "local_time") val localTime: String,
    val timezone: String,
    val state: String,
)

@Entity(tableName = "event_log")
data class EventEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val tag: String,
    val message: String,
    @ColumnInfo(name = "job_id") val jobId: String = "",
    val at: Long,
)

// --------------------------------------------------------------------------
// Converters
// --------------------------------------------------------------------------
class Converters {
    // Delimiter is the ASCII Unit Separator, U+001F. Titles and quality-gate
    // blocker messages legitimately contain commas, pipes and newlines, so the
    // delimiter must be a character that cannot appear in the data.
    @TypeConverter
    fun stringListToString(value: List<String>): String = value.joinToString("\u001F")

    @TypeConverter
    fun stringToStringList(value: String): List<String> =
        if (value.isBlank()) emptyList() else value.split("\u001F")

    @TypeConverter
    fun intListToString(value: List<Int>): String = value.joinToString(",")

    @TypeConverter
    fun stringToIntList(value: String): List<Int> =
        if (value.isBlank()) emptyList()
        else value.split(",").mapNotNull { it.trim().toIntOrNull() }
}

// --------------------------------------------------------------------------
// DAOs
// --------------------------------------------------------------------------
@Dao
interface AutomationDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(item: AutomationEntity)

    @Query("SELECT * FROM automations ORDER BY created_at DESC")
    fun observeAll(): Flow<List<AutomationEntity>>

    @Query("SELECT * FROM automations WHERE enabled = 1 ORDER BY created_at DESC")
    suspend fun enabled(): List<AutomationEntity>

    @Query("SELECT * FROM automations WHERE id = :id")
    suspend fun byId(id: String): AutomationEntity?

    @Query("UPDATE automations SET enabled = :enabled WHERE id = :id")
    suspend fun setEnabled(id: String, enabled: Boolean)

    @Query("DELETE FROM automations WHERE id = :id")
    suspend fun delete(id: String)
}

@Dao
interface JobDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertAll(items: List<JobEntity>)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(item: JobEntity)

    @Query("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT :limit")
    fun observeRecent(limit: Int = 50): Flow<List<JobEntity>>

    @Query("SELECT * FROM jobs WHERE status = :status ORDER BY updated_at DESC")
    fun observeByStatus(status: String): Flow<List<JobEntity>>

    @Query("SELECT * FROM jobs WHERE job_id = :jobId")
    suspend fun byId(jobId: String): JobEntity?

    @Query("SELECT COUNT(*) FROM jobs WHERE status = :status")
    fun countByStatus(status: String): Flow<Int>

    /**
     * One-shot variant. Workers cannot collect a Flow without blocking, so
     * background code uses this instead.
     */
    @Query("SELECT COUNT(*) FROM jobs WHERE status = :status")
    suspend fun countByStatusOnce(status: String): Int

    @Query(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('PUBLISHED','SCHEDULED','READY') " +
            "AND updated_at >= :since"
    )
    fun countCompletedSince(since: Long): Flow<Int>

    @Query("DELETE FROM jobs WHERE updated_at < :before")
    suspend fun pruneOlderThan(before: Long)
}

@Dao
interface ResearchDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertAll(items: List<ResearchEntity>)

    @Query(
        "SELECT * FROM research_cache WHERE niche = :niche " +
            "ORDER BY viral_score DESC LIMIT :limit"
    )
    fun observeForNiche(niche: String, limit: Int = 40): Flow<List<ResearchEntity>>

    @Query("DELETE FROM research_cache WHERE niche = :niche")
    suspend fun clearNiche(niche: String)
}

@Dao
interface AnalyticsDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertAll(items: List<AnalyticsEntity>)

    @Query("SELECT * FROM analytics ORDER BY views DESC LIMIT :limit")
    fun observeAll(limit: Int = 100): Flow<List<AnalyticsEntity>>

    @Query("SELECT SUM(views) FROM analytics")
    fun totalViews(): Flow<Long?>

    @Query("SELECT AVG(avg_view_percentage) FROM analytics WHERE views > 0")
    fun averageRetention(): Flow<Double?>

    @Query("SELECT SUM(subscribers_gained) FROM analytics")
    fun totalSubscribers(): Flow<Long?>
}

@Dao
interface ScheduleDao {
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertAll(items: List<ScheduleEntity>)

    @Query("SELECT * FROM schedules ORDER BY publish_at_utc ASC")
    fun observeAll(): Flow<List<ScheduleEntity>>

    @Query("DELETE FROM schedules WHERE automation_id = :automationId")
    suspend fun clearForAutomation(automationId: String)
}

@Dao
interface EventDao {
    @Insert
    suspend fun add(event: EventEntity)

    @Query("SELECT * FROM event_log ORDER BY at DESC LIMIT :limit")
    fun observeRecent(limit: Int = 200): Flow<List<EventEntity>>

    @Query("DELETE FROM event_log WHERE at < :before")
    suspend fun pruneOlderThan(before: Long)
}

// --------------------------------------------------------------------------
@Database(
    entities = [
        AutomationEntity::class,
        JobEntity::class,
        ResearchEntity::class,
        AnalyticsEntity::class,
        ScheduleEntity::class,
        EventEntity::class,
    ],
    version = 1,
    exportSchema = false,
)
@TypeConverters(Converters::class)
abstract class AppDatabase : RoomDatabase() {
    abstract fun automations(): AutomationDao
    abstract fun jobs(): JobDao
    abstract fun research(): ResearchDao
    abstract fun analytics(): AnalyticsDao
    abstract fun schedules(): ScheduleDao
    abstract fun events(): EventDao

    companion object {
        const val NAME = "autotube.db"
    }
}
