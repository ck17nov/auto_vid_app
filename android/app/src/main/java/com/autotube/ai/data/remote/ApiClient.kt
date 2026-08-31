package com.autotube.ai.data.remote

import com.autotube.ai.BuildConfig
import com.autotube.ai.data.prefs.SecureStore
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.TimeUnit

/**
 * Builds the Retrofit client.
 *
 * The API key is injected per request from [SecureStore] rather than captured
 * at construction, so changing it in Settings takes effect immediately and the
 * key is never held in a long-lived field.
 *
 * The client is rebuilt only when the base URL changes; that is checked on
 * every access because the user can repoint the backend at runtime.
 */
class ApiClient(private val store: SecureStore) {

    private val json = Json {
        ignoreUnknownKeys = true
        coerceInputValues = true
        explicitNulls = false
        isLenient = true
    }

    @Volatile private var cachedBaseUrl: String? = null
    @Volatile private var cachedService: ApiService? = null

    private fun normalizeBaseUrl(raw: String): String {
        val trimmed = raw.trim().ifEmpty { BuildConfig.DEFAULT_BACKEND_URL }
        // Retrofit requires a trailing slash on the base URL.
        return if (trimmed.endsWith("/")) trimmed else "$trimmed/"
    }

    /** Current service, rebuilt if the configured backend URL changed. */
    fun service(): ApiService {
        val baseUrl = normalizeBaseUrl(store.backendUrl)
        val existing = cachedService
        if (existing != null && cachedBaseUrl == baseUrl) return existing
        synchronized(this) {
            val again = cachedService
            if (again != null && cachedBaseUrl == baseUrl) return again
            val built = build(baseUrl)
            cachedBaseUrl = baseUrl
            cachedService = built
            return built
        }
    }

    private fun build(baseUrl: String): ApiService {
        val logging = HttpLoggingInterceptor().apply {
            // Never log bodies or headers in release: they would contain the
            // API key and job metadata (spec section 33).
            level = if (BuildConfig.DEBUG) HttpLoggingInterceptor.Level.BASIC
            else HttpLoggingInterceptor.Level.NONE
        }

        val client = OkHttpClient.Builder()
            // Rendering can take minutes, but every endpoint returns fast
            // because work is queued server-side; these are generous but finite.
            .connectTimeout(20, TimeUnit.SECONDS)
            .readTimeout(120, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .addInterceptor { chain ->
                val builder = chain.request().newBuilder()
                    .header("Accept", "application/json")
                store.apiKey.takeIf { it.isNotBlank() }?.let {
                    builder.header("X-API-Key", it)
                }
                chain.proceed(builder.build())
            }
            .addInterceptor(logging)
            .build()

        return Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(ApiService::class.java)
    }

    /** Absolute URL for a media path returned by the backend (e.g. previews). */
    fun mediaUrl(path: String): String {
        val base = normalizeBaseUrl(store.backendUrl)
        return base + path.removePrefix("/")
    }

    val apiKeyHeader: Pair<String, String>?
        get() = store.apiKey.takeIf { it.isNotBlank() }?.let { "X-API-Key" to it }
}
