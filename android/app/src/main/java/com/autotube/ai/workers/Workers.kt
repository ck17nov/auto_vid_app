package com.autotube.ai.workers

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.autotube.ai.AutoTubeApp
import com.autotube.ai.R
import com.autotube.ai.data.remote.AutomationRequestDto
import java.util.concurrent.TimeUnit

/**
 * Background work (spec section 2).
 *
 * The phone is an orchestrator, not a renderer: these workers only talk HTTP.
 * That is what makes them survivable under Doze and Android's background
 * restrictions - each run is short, network-bound and idempotent.
 *
 * We deliberately do NOT use a long-running foreground service: the spec
 * requires not assuming one can run indefinitely, and WorkManager already
 * guarantees execution across process death and reboot.
 */

/** Polls job status so the dashboard is current when the user opens the app. */
class SyncWorker(appContext: Context, params: WorkerParameters) :
    CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext as AutoTubeApp
        val repo = app.repository
        if (!app.secureStore.isConfigured) {
            // Nothing to sync until the backend is set up; do not burn retries.
            return Result.success()
        }

        val refreshed = repo.refreshJobs()
        if (refreshed.isFailure) {
            // Transient network problems are the common case -> retry with the
            // backoff configured on the request.
            return if (runAttemptCount < MAX_ATTEMPTS) Result.retry() else Result.success()
        }

        notifyIfNeeded(app)
        repo.prune()
        return Result.success()
    }

    /** Surface anything waiting on the user (spec section 24). */
    private suspend fun notifyIfNeeded(app: AutoTubeApp) {
        val jobs = app.database.jobs()
        val approvals = runCatching {
            jobs.countByStatusOnce(STATUS_AWAITING_APPROVAL)
        }.getOrDefault(0)
        val failures = runCatching {
            jobs.countByStatusOnce(STATUS_FAILED)
        }.getOrDefault(0)

        if (approvals > 0) {
            postNotification(
                app,
                title = "Video ready for approval",
                body = if (approvals == 1) "1 video is waiting for your review."
                else "$approvals videos are waiting for your review.",
                id = NOTIF_APPROVAL,
            )
        }
        if (failures > 0) {
            postNotification(
                app,
                title = "Automation problem",
                body = "$failures job(s) failed. Open AutoTube AI for details.",
                id = NOTIF_FAILURE,
            )
        }
    }

    companion object {
        const val NAME = "autotube-sync"
        const val MAX_ATTEMPTS = 4
        const val STATUS_AWAITING_APPROVAL = "AWAITING_APPROVAL"
        const val STATUS_FAILED = "FAILED"
        const val NOTIF_APPROVAL = 4101
        const val NOTIF_FAILURE = 4102
    }
}

/** Fires a configured automation on its schedule. */
class AutomationWorker(appContext: Context, params: WorkerParameters) :
    CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext as AutoTubeApp
        if (!app.secureStore.isConfigured) return Result.success()

        val automationId = inputData.getString(KEY_AUTOMATION_ID)
        val automation = automationId?.let { app.database.automations().byId(it) }
            ?: return Result.success()
        if (!automation.enabled) return Result.success()

        val request = AutomationRequestDto(
            niche = automation.niche,
            audience = automation.audience,
            language = automation.language,
            videoFormat = automation.videoFormat,
            durationSeconds = automation.durationSeconds,
            style = automation.style,
            count = 1,
            mode = automation.mode,
            frequency = automation.frequency,
            days = automation.days,
            uploadTime = automation.uploadTime,
            timezone = automation.timezone,
            madeForKids = automation.madeForKids,
        )
        val result = app.repository.startAutomation(request)
        return if (result.isSuccess) {
            Result.success()
        } else if (runAttemptCount < 3) {
            Result.retry()
        } else {
            app.repository.logEvent("AUTOMATION", "failed to queue after retries")
            Result.success()
        }
    }

    companion object {
        const val KEY_AUTOMATION_ID = "automation_id"
        fun nameFor(automationId: String) = "autotube-automation-$automationId"
    }
}

/** Collects own-channel analytics daily so the learning loop has data. */
class AnalyticsWorker(appContext: Context, params: WorkerParameters) :
    CoroutineWorker(appContext, params) {

    override suspend fun doWork(): Result {
        val app = applicationContext as AutoTubeApp
        if (!app.secureStore.isConfigured) return Result.success()
        val result = app.repository.refreshAnalytics(collect = true)
        return if (result.isSuccess || runAttemptCount >= 3) Result.success()
        else Result.retry()
    }

    companion object {
        const val NAME = "autotube-analytics"
    }
}

// --------------------------------------------------------------------------
object WorkScheduler {

    private val networkConstraints = Constraints.Builder()
        .setRequiredNetworkType(NetworkType.CONNECTED)
        .build()

    /** Periodic status sync. 15 minutes is WorkManager's minimum interval. */
    fun scheduleSync(context: Context) {
        val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(networkConstraints)
            .setBackoffCriteria(
                androidx.work.BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            SyncWorker.NAME, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    fun syncNow(context: Context) {
        val request = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(networkConstraints)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            "${SyncWorker.NAME}-now", ExistingWorkPolicy.REPLACE, request)
    }

    fun scheduleAnalytics(context: Context) {
        val request = PeriodicWorkRequestBuilder<AnalyticsWorker>(12, TimeUnit.HOURS)
            .setConstraints(networkConstraints)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            AnalyticsWorker.NAME, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    /**
     * Schedule a recurring automation.
     *
     * Content is generated AHEAD of the publish time (spec section 20): the
     * worker runs `leadHours` before the slot, and the backend sets YouTube's
     * own `publishAt`, so the phone does not need to be online at 8 PM.
     */
    fun scheduleAutomation(
        context: Context,
        automationId: String,
        intervalHours: Long,
        initialDelayMinutes: Long,
    ) {
        val request = PeriodicWorkRequestBuilder<AutomationWorker>(
            intervalHours.coerceAtLeast(1), TimeUnit.HOURS
        )
            .setConstraints(networkConstraints)
            .setInitialDelay(initialDelayMinutes.coerceAtLeast(0), TimeUnit.MINUTES)
            .setInputData(workDataOf(AutomationWorker.KEY_AUTOMATION_ID to automationId))
            .setBackoffCriteria(
                androidx.work.BackoffPolicy.EXPONENTIAL, 5, TimeUnit.MINUTES)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            AutomationWorker.nameFor(automationId),
            ExistingPeriodicWorkPolicy.UPDATE,
            request,
        )
    }

    fun cancelAutomation(context: Context, automationId: String) {
        WorkManager.getInstance(context)
            .cancelUniqueWork(AutomationWorker.nameFor(automationId))
    }
}

// --------------------------------------------------------------------------
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED ||
            intent.action == Intent.ACTION_MY_PACKAGE_REPLACED
        ) {
            // Periodic work survives reboot on its own, but re-arming is cheap
            // and covers the case where the app was updated (spec section 22).
            WorkScheduler.scheduleSync(context)
            WorkScheduler.scheduleAnalytics(context)
        }
    }
}

// --------------------------------------------------------------------------
internal const val CHANNEL_ID = "autotube_status"

fun ensureNotificationChannel(context: Context) {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Automation status",
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = "Approvals, failures and publishing updates."
        }
        context.getSystemService(NotificationManager::class.java)
            ?.createNotificationChannel(channel)
    }
}

fun postNotification(context: Context, title: String, body: String, id: Int) {
    // POST_NOTIFICATIONS is a runtime permission from Android 13; posting
    // without it is a silent no-op, so check rather than assume.
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        val granted = context.checkSelfPermission(
            android.Manifest.permission.POST_NOTIFICATIONS
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) return
    }
    ensureNotificationChannel(context)
    val notification = NotificationCompat.Builder(context, CHANNEL_ID)
        .setSmallIcon(R.drawable.ic_launcher_foreground)
        .setContentTitle(title)
        .setContentText(body)
        .setAutoCancel(true)
        .setPriority(NotificationCompat.PRIORITY_DEFAULT)
        .build()
    runCatching {
        NotificationManagerCompat.from(context).notify(id, notification)
    }
}
