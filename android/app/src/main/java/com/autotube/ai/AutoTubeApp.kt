package com.autotube.ai

import android.app.Application
import androidx.room.Room
import com.autotube.ai.data.local.AppDatabase
import com.autotube.ai.data.prefs.SecureStore
import com.autotube.ai.data.remote.ApiClient
import com.autotube.ai.data.repo.AutoTubeRepository
import com.autotube.ai.workers.WorkScheduler
import com.autotube.ai.workers.ensureNotificationChannel

/**
 * Application-scoped container.
 *
 * Manual dependency wiring rather than Hilt: the graph is six objects, and
 * avoiding an annotation processor keeps the build simple and fast for a
 * single-module app.
 */
class AutoTubeApp : Application() {

    val secureStore: SecureStore by lazy { SecureStore(this) }

    val database: AppDatabase by lazy {
        Room.databaseBuilder(this, AppDatabase::class.java, AppDatabase.NAME)
            // The local DB is a cache of backend state; on a schema change the
            // fastest correct behaviour is to rebuild it from the backend.
            .fallbackToDestructiveMigration()
            .build()
    }

    val apiClient: ApiClient by lazy { ApiClient(secureStore) }

    val repository: AutoTubeRepository by lazy {
        AutoTubeRepository(database, apiClient, secureStore)
    }

    override fun onCreate() {
        super.onCreate()
        ensureNotificationChannel(this)
        // Safe to call every launch: both use unique-work policies.
        WorkScheduler.scheduleSync(this)
        WorkScheduler.scheduleAnalytics(this)
    }
}
