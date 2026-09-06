package com.autotube.ai.data.prefs

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKeys

/**
 * In-memory SharedPreferences, used only when encrypted storage cannot be
 * opened at all. Nothing survives the process, which is the point: secrets
 * must never fall back to plaintext on disk.
 */
private class InMemoryPrefs : SharedPreferences {
    private val map = mutableMapOf<String, Any?>()

    override fun getAll(): MutableMap<String, *> = map.toMutableMap()
    override fun getString(key: String?, defValue: String?) = map[key] as? String ?: defValue
    override fun getStringSet(key: String?, defValues: MutableSet<String>?) =
        @Suppress("UNCHECKED_CAST") (map[key] as? MutableSet<String> ?: defValues)
    override fun getInt(key: String?, defValue: Int) = map[key] as? Int ?: defValue
    override fun getLong(key: String?, defValue: Long) = map[key] as? Long ?: defValue
    override fun getFloat(key: String?, defValue: Float) = map[key] as? Float ?: defValue
    override fun getBoolean(key: String?, defValue: Boolean) = map[key] as? Boolean ?: defValue
    override fun contains(key: String?) = map.containsKey(key)
    override fun registerOnSharedPreferenceChangeListener(
        listener: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit
    override fun unregisterOnSharedPreferenceChangeListener(
        listener: SharedPreferences.OnSharedPreferenceChangeListener?) = Unit

    override fun edit(): SharedPreferences.Editor = object : SharedPreferences.Editor {
        private val staged = mutableMapOf<String, Any?>()
        private val removed = mutableSetOf<String>()
        private var clearAll = false

        override fun putString(key: String, value: String?) = apply { staged[key] = value }
        override fun putStringSet(key: String, values: MutableSet<String>?) =
            apply { staged[key] = values }
        override fun putInt(key: String, value: Int) = apply { staged[key] = value }
        override fun putLong(key: String, value: Long) = apply { staged[key] = value }
        override fun putFloat(key: String, value: Float) = apply { staged[key] = value }
        override fun putBoolean(key: String, value: Boolean) = apply { staged[key] = value }
        override fun remove(key: String) = apply { removed += key }
        override fun clear() = apply { clearAll = true }

        override fun commit(): Boolean { write(); return true }
        override fun apply() = write()

        private fun write() {
            if (clearAll) map.clear()
            removed.forEach { map.remove(it) }
            map.putAll(staged)
        }
    }
}

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
        try {
            return build(context)
        } catch (first: Exception) {
            Log.w(TAG, "encrypted prefs unavailable: ${first.javaClass.simpleName}")
        }

        // Recovery attempt. The previous version deleted only the preferences
        // FILE, which fixes nothing when the fault is the key: rebuilding with
        // the same Keystore alias fails identically, so the app crashed on
        // every launch after the first and the only cure was clearing app
        // data. The Keystore entry has to go too.
        //
        // This is the documented failure mode of EncryptedSharedPreferences -
        // an AEADBadTagException or KeyStoreException after a restore, an OS
        // upgrade, or an interrupted write leaves file and key out of step.
        try {
            context.deleteSharedPreferences(FILE_NAME)
            deleteMasterKey()
            return build(context)
        } catch (second: Exception) {
            Log.e(TAG, "could not recreate encrypted prefs: ${second.javaClass.simpleName}")
        }

        // Last resort: keep running with secrets held in memory only.
        //
        // The alternative is throwing from an Application `by lazy`, which
        // kills the process on launch with nothing the user can do but clear
        // app data - exactly the bug being fixed. Secrets are NOT written to
        // plaintext storage: they live for this process only, so the user
        // re-enters the backend key and reconnects YouTube. Degraded and
        // visible beats dead.
        Log.e(TAG, "falling back to in-memory secrets for this session")
        inMemoryOnly = true
        return InMemoryPrefs()
    }

    /** True when encrypted storage could not be opened and nothing persists. */
    var inMemoryOnly: Boolean = false
        private set

    private fun deleteMasterKey() {
        try {
            val ks = java.security.KeyStore.getInstance("AndroidKeyStore")
            ks.load(null)
            if (ks.containsAlias(MASTER_KEY_ALIAS)) {
                ks.deleteEntry(MASTER_KEY_ALIAS)
                Log.w(TAG, "deleted the stale Keystore master key")
            }
        } catch (e: Exception) {
            Log.w(TAG, "could not delete master key: ${e.javaClass.simpleName}")
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

    /**
     * Which Google account to sign in as, e.g. the channel owner.
     *
     * Sent as `login_hint`. Without it, Chrome hands the authorization request
     * to whichever account is the browser's default, which on a phone with
     * several Google accounts is usually the wrong one - and the flow either
     * signs in the wrong channel or is refused.
     */
    var youtubeAccountEmail: String
        get() = prefs.getString(KEY_YT_ACCOUNT, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_YT_ACCOUNT, value.trim()).apply()

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

    /**
     * Approval count at the last sync. Used to notify only when it INCREASES.
     * Without it the 15-minute sync re-notified about the same pending job
     * forever, including jobs created before the user first opened the app.
     */
    var lastApprovalCount: Int
        get() = prefs.getInt(KEY_LAST_APPROVALS, 0)
        set(value) = prefs.edit().putInt(KEY_LAST_APPROVALS, value).apply()

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

        // MasterKeys.AES256_GCM_SPEC uses this fixed alias. Needed by name so
        // a stale entry can be deleted during recovery.
        private const val MASTER_KEY_ALIAS = "_androidx_security_master_key_"

        private const val KEY_API_KEY = "api_key"
        private const val KEY_REFRESH_TOKEN = "yt_refresh_token"
        private const val KEY_OAUTH_CLIENT_ID = "yt_oauth_client_id"
        private const val KEY_YT_ACCOUNT = "yt_account_email"
        private const val KEY_BACKEND_URL = "backend_url"
        private const val KEY_DEFAULT_NICHE = "default_niche"
        private const val KEY_DEFAULT_LANGUAGE = "default_language"
        private const val KEY_TIMEZONE = "timezone"
        private const val KEY_QUALITY_THRESHOLD = "quality_threshold"
        private const val KEY_AUTO_APPROVE = "auto_approve"
        private const val KEY_CHANNEL_ID = "channel_id"
        private const val KEY_ONBOARDED = "onboarded"
        private const val KEY_LAST_APPROVALS = "last_approval_count"
    }
}
