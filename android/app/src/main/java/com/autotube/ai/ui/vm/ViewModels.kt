package com.autotube.ai.ui.vm

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.CreationExtras
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext
import com.autotube.ai.AutoTubeApp
import com.autotube.ai.data.local.AnalyticsEntity
import com.autotube.ai.data.local.JobEntity
import com.autotube.ai.data.prefs.SecureStore
import com.autotube.ai.data.remote.AutomationRequestDto
import com.autotube.ai.data.remote.HealthDto
import com.autotube.ai.data.remote.JobDetailDto
import com.autotube.ai.data.remote.NichePreviewDto
import com.autotube.ai.data.remote.QuotaDto
import com.autotube.ai.data.remote.YouTubeStatusDto
import com.autotube.ai.data.repo.AutoTubeRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * ViewModels.
 *
 * Room Flows drive the UI so every screen renders from cache instantly and
 * offline; network refreshes are explicit and always report their error text
 * rather than failing silently.
 */

// --------------------------------------------------------------------------
@Composable
inline fun <reified T : ViewModel> appViewModel(): T {
    val app = LocalContext.current.applicationContext as AutoTubeApp
    return viewModel(factory = AppViewModelFactory(app))
}

class AppViewModelFactory(private val app: AutoTubeApp) : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>, extras: CreationExtras): T {
        val repo = app.repository
        val store = app.secureStore
        return when {
            modelClass.isAssignableFrom(DashboardViewModel::class.java) ->
                DashboardViewModel(repo, store, app) as T
            modelClass.isAssignableFrom(CreateViewModel::class.java) ->
                CreateViewModel(repo, store, app) as T
            modelClass.isAssignableFrom(JobViewModel::class.java) ->
                JobViewModel(repo, store) as T
            modelClass.isAssignableFrom(AnalyticsViewModel::class.java) ->
                AnalyticsViewModel(repo) as T
            modelClass.isAssignableFrom(SettingsViewModel::class.java) ->
                SettingsViewModel(repo, store, app) as T
            else -> throw IllegalArgumentException("Unknown ViewModel $modelClass")
        }
    }
}

// --------------------------------------------------------------------------
data class UiMessage(val text: String, val isError: Boolean = false)

open class BaseViewModel : ViewModel() {
    private val _message = MutableStateFlow<UiMessage?>(null)
    val message: StateFlow<UiMessage?> = _message.asStateFlow()

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy.asStateFlow()

    protected fun info(text: String) { _message.value = UiMessage(text, false) }
    protected fun error(text: String) { _message.value = UiMessage(text, true) }
    fun clearMessage() { _message.value = null }

    protected fun <T> runTask(
        onSuccess: (T) -> Unit = {},
        block: suspend () -> Result<T>,
    ) {
        viewModelScope.launch {
            _busy.value = true
            val result = block()
            _busy.value = false
            result.fold(
                onSuccess = {
                    // Clear a stale error before running the success handler.
                    //
                    // Without this, the first failure stuck to the screen for
                    // the rest of the session. A fresh install times out
                    // against the default backend address before the user has
                    // set a URL, so the Create tab kept showing "cannot reach
                    // the backend" over a niche profile it had just fetched
                    // successfully - while the Dashboard, whose own call had
                    // succeeded, showed connected. The banner was the bug; the
                    // backend was fine.
                    if (_message.value?.isError == true) _message.value = null
                    onSuccess(it)
                },
                onFailure = { error(it.message ?: "Something went wrong") },
            )
        }
    }
}

// --------------------------------------------------------------------------
class DashboardViewModel(
    private val repo: AutoTubeRepository,
    val store: SecureStore,
    private val app: AutoTubeApp,
) : BaseViewModel() {

    val jobs: StateFlow<List<JobEntity>> = repo.observeJobs()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    val awaitingApproval: StateFlow<List<JobEntity>> =
        repo.observeJobsByStatus("AWAITING_APPROVAL")
            .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    val publishedCount: StateFlow<Int> = repo.countByStatus("PUBLISHED")
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    val scheduledCount: StateFlow<Int> = repo.countByStatus("SCHEDULED")
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    val failedCount: StateFlow<Int> = repo.countByStatus("FAILED")
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    val todayCount: StateFlow<Int> = repo.countCompletedToday()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    val totalViews: StateFlow<Long?> = repo.totalViews()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    private val _health = MutableStateFlow<HealthDto?>(null)
    val health: StateFlow<HealthDto?> = _health.asStateFlow()

    private val _quota = MutableStateFlow<QuotaDto?>(null)
    val quota: StateFlow<QuotaDto?> = _quota.asStateFlow()

    val isConfigured: Boolean get() = store.isConfigured

    fun refresh() {
        if (!store.isConfigured) {
            error("Set the backend URL and API key in Settings first.")
            return
        }
        runTask<Int>({ }) { repo.refreshJobs() }
        viewModelScope.launch {
            repo.health().onSuccess { _health.value = it }
            repo.quota().onSuccess { _quota.value = it }
        }
    }

    fun approve(jobId: String) = runTask<Unit>({
        // Do not promise an upload the backend cannot perform.
        //
        // In dry-run mode the backend renders everything and uploads nothing,
        // so "Uploading or scheduling now" was simply false - the video never
        // appeared in Schedule because no publishAt was ever set, and there
        // was nothing on screen to explain why.
        if (_health.value?.dryRun != false) {
            info(
                "Approved. The backend is in DRY RUN, so the video is rendered " +
                    "but not uploaded and will not appear in Schedule. Set " +
                    "dry_run: false on the backend to publish."
            )
        } else {
            info("Approved. Uploading or scheduling now.")
        }
        refresh()
    }) { repo.approve(jobId) }

    fun reject(jobId: String, reason: String = "rejected from dashboard") =
        runTask<Unit>({ info("Rejected."); refresh() }) { repo.reject(jobId, reason) }
}

// --------------------------------------------------------------------------
class CreateViewModel(
    private val repo: AutoTubeRepository,
    val store: SecureStore,
    private val app: AutoTubeApp,
) : BaseViewModel() {

    private val _preview = MutableStateFlow<NichePreviewDto?>(null)
    val preview: StateFlow<NichePreviewDto?> = _preview.asStateFlow()

    private val _started = MutableStateFlow(false)
    val started: StateFlow<Boolean> = _started.asStateFlow()

    /** Shown when the backend says this niche looks child-directed. */
    private val _kidsPrompt = MutableStateFlow(false)
    val kidsPrompt: StateFlow<Boolean> = _kidsPrompt.asStateFlow()

    /**
     * Whether the backend pins every upload to private.
     *
     * Read here so the publish selector can say that scheduling will not take
     * effect, rather than offering a choice the backend quietly ignores.
     */
    private val _forcePrivate = MutableStateFlow(false)
    val forcePrivate: StateFlow<Boolean> = _forcePrivate.asStateFlow()

    init {
        if (store.isConfigured) {
            viewModelScope.launch {
                repo.health().onSuccess { _forcePrivate.value = it.forcePrivate }
            }
        }
    }

    fun previewNiche(niche: String, audience: String, style: String, duration: Int) {
        if (niche.length < 2) return
        runTask<NichePreviewDto>({
            _preview.value = it
            _kidsPrompt.value = it.requiresKidsConfirmation
        }) { repo.nichePreview(niche, audience, style, duration) }
    }

    fun dismissKidsPrompt() { _kidsPrompt.value = false }

    fun start(request: AutomationRequestDto) {
        if (!store.isConfigured) {
            error("Set the backend URL and API key in Settings first.")
            return
        }
        runTask<String>({
            _started.value = true
            info("Automation queued. Watch the Dashboard for progress.")
            com.autotube.ai.workers.WorkScheduler.syncNow(app)
            if (request.frequency != "once") {
                val intervalHours = when (request.frequency) {
                    "daily" -> 24L
                    "weekly" -> 168L
                    else -> 24L
                }
                com.autotube.ai.workers.WorkScheduler.scheduleAutomation(
                    app, it, intervalHours, initialDelayMinutes = 0,
                )
            }
        }) { repo.startAutomation(request) }
    }

    fun resetStarted() { _started.value = false }
}

// --------------------------------------------------------------------------
class JobViewModel(
    private val repo: AutoTubeRepository,
    val store: SecureStore,
) : BaseViewModel() {

    private val _detail = MutableStateFlow<JobDetailDto?>(null)
    val detail: StateFlow<JobDetailDto?> = _detail.asStateFlow()

    val jobs: StateFlow<List<JobEntity>> = repo.observeJobs(80)
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    fun load(jobId: String) {
        if (jobId.isBlank()) return
        runTask<JobDetailDto>({ _detail.value = it }) { repo.jobDetail(jobId) }
    }

    fun approve(jobId: String) = runTask<Unit>({
        info("Approved.")
        load(jobId)
    }) { repo.approve(jobId) }

    fun reject(jobId: String, reason: String) = runTask<Unit>({
        info("Rejected.")
        load(jobId)
    }) { repo.reject(jobId, reason) }

    fun mediaUrl(path: String) = repo.mediaUrl(path)
    fun apiKeyHeader() = repo.apiKeyHeader()
}

// --------------------------------------------------------------------------
class AnalyticsViewModel(
    private val repo: AutoTubeRepository,
) : BaseViewModel() {

    val rows: StateFlow<List<AnalyticsEntity>> = repo.observeAnalytics()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    val totalViews: StateFlow<Long?> = repo.totalViews()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    val avgRetention: StateFlow<Double?> = repo.averageRetention()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    val totalSubs: StateFlow<Long?> = repo.totalSubscribers()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)

    private val _hints = MutableStateFlow("")
    val hints: StateFlow<String> = _hints.asStateFlow()

    fun refresh(collect: Boolean = false) {
        runTask<Int>({ info(if (collect) "Collected from YouTube." else "Refreshed.") }) {
            repo.refreshAnalytics(collect)
        }
    }
}

// --------------------------------------------------------------------------
class SettingsViewModel(
    private val repo: AutoTubeRepository,
    val store: SecureStore,
    private val app: AutoTubeApp,
) : BaseViewModel() {

    private val _health = MutableStateFlow<HealthDto?>(null)
    val health: StateFlow<HealthDto?> = _health.asStateFlow()

    private val _youtube = MutableStateFlow<YouTubeStatusDto?>(null)
    val youtube: StateFlow<YouTubeStatusDto?> = _youtube.asStateFlow()

    fun testConnection() {
        runTask<HealthDto>({
            _health.value = it
            info("Connected to backend ${it.version}.")
        }) { repo.health() }
    }

    fun refreshYouTube() {
        runTask<YouTubeStatusDto>({ _youtube.value = it }) { repo.youtubeStatus() }
    }

    fun sendRefreshToken(token: String) {
        runTask<Boolean>({
            if (it) {
                info("YouTube connected. The backend can now upload.")
                refreshYouTube()
            } else {
                error("Backend did not store the token.")
            }
        }) { repo.sendRefreshToken(token) }
    }

    fun reportAuthError(text: String) = error(text)

    fun clearSecrets() {
        store.clearSecrets()
        info("Stored credentials cleared from this device.")
    }
}
