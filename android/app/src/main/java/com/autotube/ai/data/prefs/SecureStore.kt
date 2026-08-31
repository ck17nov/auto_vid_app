package com.autotube.ai.data.prefs

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKeys

/**
 * Credential and settings storage (spec sections 30 & 31).
 *
 * Secrets (backend API key, OAuth refresh token) live in
 * EncryptedSharedPreferences, whose master key is held in the Android Keystore -
 * so the values are not readable from a backup, or from an adb pull on a rooted
 * device, without the Keystore.
 *
 * A YouTube PASSWORD is never stored anywhere. Only OAuth tokens.
 *
 * API note: this uses `MasterKeys` rather than the newer `MasterKey` builder.
 * `MasterKey` only exists in security-crypto 1.1.0-*alpha*, and depending on a
 * years-old alpha for the component that guards the user's OAuth token is a
 * worse trade than using the deprecated-but-stable 1.0.0 API. Both are backed by
 * the same Keystore-held AES-256-GCM key.
 */
@Suppress("DEPRECATION")
class SecureStore(context: Context) {

    private val prefs: SharedPreferences = createPrefs(context)

    private fun createPrefs(context: Context): SharedPreferences {
        return try {
            build(context)
        } catch (e: Exception) {
            // A corrupted keystore entry (e.g. after a restore onto a new
            // device) makes the encrypted file undecryptable. Clearing it loses
            // the stored secrets, which is the correct trade: the user re-enters
            // them, and we never silently downgrade to plaintext storage.
            Log.w(TAG, "encrypted prefs unavailable, recreating: ${e.javaClass.simpleName}")
            context.deleteSharedPreferences(FILE_NAME)
            build(context)
        }
    }

    private fun build(context: Context): SharedPreferences {
        val masterKeyAlias = MasterKeys.getOrCreate(MasterKeys.AES256_GCM_SPEC)
        return EncryptedSharedPreferences.create(
            FILE_NAME,
            masterKeyAlias,
            context,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    // ---- secrets --------------------------------------------------------
    var apiKey: String
        get() = prefs.getString(KEY_API_KEY, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_API_KEY, value.trim()).apply()

    var refreshToken: String
        get() = prefs.getString(KEY_REFRESH_TOKEN, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_REFRESH_TOKEN, value.trim()).apply()

    var oauthClientId: String
        get() = prefs.getString(KEY_OAUTH_CLIENT_ID, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_OAUTH_CLIENT_ID, value.trim()).apply()

    // ---- configuration --------------------------------------------------
    var backendUrl: String
        get() = prefs.getString(KEY_BACKEND_URL, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_BACKEND_URL, value.trim()).apply()

    var defaultNiche: String
        get() = prefs.getString(KEY_DEFAULT_NICHE, "science").orEmpty()
        set(value) = prefs.edit().putString(KEY_DEFAULT_NICHE, value).apply()

    var defaultLanguage: String
        get() = prefs.getString(KEY_DEFAULT_LANGUAGE, "en").orEmpty()
        set(value) = prefs.edit().putString(KEY_DEFAULT_LANGUAGE, value).apply()

    var timezone: String
        get() = prefs.getString(KEY_TIMEZONE, "Asia/Kolkata").orEmpty()
        set(value) = prefs.edit().putString(KEY_TIMEZONE, value).apply()

    var qualityThreshold: Int
        get() = prefs.getInt(KEY_QUALITY_THRESHOLD, 80)
        set(value) = prefs.edit().putInt(KEY_QUALITY_THRESHOLD, value).apply()

    /** APPROVAL mode is the default for a new install (spec section 24). */
    var autoApprove: Boolean
        get() = prefs.getBoolean(KEY_AUTO_APPROVE, false)
        set(value) = prefs.edit().putBoolean(KEY_AUTO_APPROVE, value).apply()

    var selectedChannelId: String
        get() = prefs.getString(KEY_CHANNEL_ID, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_CHANNEL_ID, value).apply()

    var onboarded: Boolean
        get() = prefs.getBoolean(KEY_ONBOARDED, false)
        set(value) = prefs.edit().putBoolean(KEY_ONBOARDED, value).apply()

    val isConfigured: Boolean
        get() = backendUrl.isNotBlank() && apiKey.isNotBlank()

    fun clearSecrets() {
        prefs.edit()
            .remove(KEY_API_KEY)
            .remove(KEY_REFRESH_TOKEN)
            .remove(KEY_OAUTH_CLIENT_ID)
            .remove(KEY_CHANNEL_ID)
            .apply()
    }

    companion object {
        private const val TAG = "SecureStore"
        private const val FILE_NAME = "autotube_secure_prefs"

        private const val KEY_API_KEY = "api_key"
        private const val KEY_REFRESH_TOKEN = "yt_refresh_token"
        private const val KEY_OAUTH_CLIENT_ID = "yt_oauth_client_id"
        private const val KEY_BACKEND_URL = "backend_url"
        private const val KEY_DEFAULT_NICHE = "default_niche"
        private const val KEY_DEFAULT_LANGUAGE = "default_language"
        private const val KEY_TIMEZONE = "timezone"
        private const val KEY_QUALITY_THRESHOLD = "quality_threshold"
        private const val KEY_AUTO_APPROVE = "auto_approve"
        private const val KEY_CHANNEL_ID = "channel_id"
        private const val KEY_ONBOARDED = "onboarded"
    }
}
