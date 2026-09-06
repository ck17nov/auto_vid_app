package com.autotube.ai.auth

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.activity.result.ActivityResultLauncher
import com.autotube.ai.BuildConfig
import com.autotube.ai.data.prefs.SecureStore
import net.openid.appauth.AuthorizationException
import net.openid.appauth.AuthorizationRequest
import net.openid.appauth.AuthorizationResponse
import net.openid.appauth.AuthorizationService
import net.openid.appauth.AuthorizationServiceConfiguration
import net.openid.appauth.ResponseTypeValues
import net.openid.appauth.TokenResponse

/**
 * YouTube OAuth 2.0 on device, via AppAuth (spec sections 19 & 31).
 *
 * Design notes:
 *  - Uses the Authorization Code flow with PKCE. AppAuth adds PKCE
 *    automatically, which is what allows a public client (an app with no
 *    secret) to authenticate safely. There is NO client secret in the APK.
 *  - The app only needs the refresh token: it forwards it to the backend,
 *    which performs uploads. The phone never uploads video bytes itself.
 *  - The user's password is never seen by the app - Google's own page handles it.
 */
class YouTubeAuthManager(context: Context, private val store: SecureStore) {

    private val authService = AuthorizationService(context.applicationContext)

    private val serviceConfig = AuthorizationServiceConfiguration(
        Uri.parse("https://accounts.google.com/o/oauth2/v2/auth"),
        Uri.parse("https://oauth2.googleapis.com/token"),
    )

    val isConfigured: Boolean get() = store.oauthClientId.isNotBlank()

    /**
     * Build the authorization intent.
     *
     * `access_type=offline` plus `prompt=consent` is required to receive a
     * refresh token; without both, Google returns only a short-lived access
     * token and unattended scheduled uploads would stop working within an hour.
     */
    fun authorizationIntent(): Intent {
        val clientId = store.oauthClientId.trim()
        require(clientId.isNotBlank()) {
            "Set the Android OAuth client ID in Settings first."
        }
        // Catch the wrong string here rather than in the browser. Google's
        // authorization endpoint answers a malformed or wrong-type client id
        // with a bare "invalid request" page that names nothing, so a client
        // secret pasted into this field, or a truncated id, sends the user
        // hunting through Google Cloud for a fault that is in the app.
        require(clientId.endsWith(GOOGLE_CLIENT_SUFFIX)) {
            "That does not look like an OAuth client ID. It must end in " +
                "\"$GOOGLE_CLIENT_SUFFIX\". Do not paste the client secret " +
                "- an Android client does not have one."
        }
        val request = AuthorizationRequest.Builder(
            serviceConfig,
            clientId,
            ResponseTypeValues.CODE,
            Uri.parse(redirectUri),
        )
            .setScopes(SCOPES)
            // select_account as well as consent.
            //
            // With consent alone, Chrome silently uses whichever Google
            // account is its default. On a phone signed into a personal
            // account and the channel's account, that is a coin flip, and the
            // user has no way to correct it from inside the flow. Asking for
            // select_account always shows the chooser.
            .setPromptValues(
                AuthorizationRequest.Prompt.SELECT_ACCOUNT,
                AuthorizationRequest.Prompt.CONSENT,
            )
            .setAdditionalParameters(extraParams())
            .build()
        return authService.getAuthorizationRequestIntent(request)
    }

    /**
     * `access_type=offline` is what makes Google return a refresh token;
     * without it the app would get a one-hour access token and scheduled
     * uploads would stop working the same afternoon.
     *
     * `login_hint` pre-selects the account, so the chooser opens on the right
     * one instead of the browser's default.
     */
    private fun extraParams(): Map<String, String> {
        val params = mutableMapOf("access_type" to "offline")
        store.youtubeAccountEmail.trim().takeIf { it.isNotBlank() }?.let {
            params["login_hint"] = it
        }
        return params
    }

    fun launch(launcher: ActivityResultLauncher<Intent>) {
        launcher.launch(authorizationIntent())
    }

    /**
     * Exchange the authorization code for tokens.
     * Calls back with the refresh token, or an error message.
     */
    fun handleResult(
        data: Intent?,
        onResult: (refreshToken: String?, error: String?) -> Unit,
    ) {
        if (data == null) {
            onResult(null, "Sign-in was cancelled.")
            return
        }
        val response = AuthorizationResponse.fromIntent(data)
        val exception = AuthorizationException.fromIntent(data)
        if (response == null) {
            onResult(null, exception?.errorDescription ?: "Authorization failed.")
            return
        }
        authService.performTokenRequest(response.createTokenExchangeRequest()) {
                tokenResponse: TokenResponse?, tokenException: AuthorizationException? ->
            val refresh = tokenResponse?.refreshToken
            when {
                refresh != null -> {
                    store.refreshToken = refresh
                    onResult(refresh, null)
                }
                tokenException != null ->
                    onResult(null, tokenException.errorDescription
                        ?: "Token exchange failed.")
                else -> onResult(
                    null,
                    "Google did not return a refresh token. Remove AutoTube AI " +
                        "from your Google account permissions and sign in again."
                )
            }
        }
    }

    fun signOut() {
        store.refreshToken = ""
    }

    fun dispose() {
        authService.dispose()
    }

    companion object {
        /**
         * The redirect scheme is the application ID of THIS build, not a
         * literal.
         *
         * A Google Android OAuth client is validated by package name plus
         * signing certificate, and AppAuth's redirect URI is
         * "<package>:/oauth2redirect". The debug build carries an
         * applicationIdSuffix, so a hard-coded "com.autotube.ai" made the
         * debug APK ask for a redirect that did not match its own package -
         * which Google rejects, with an error that reads like a bad client ID.
         *
         * Deriving it from BuildConfig means the scheme, the manifest
         * placeholder and the package can never drift apart again.
         */
        val REDIRECT_SCHEME: String = BuildConfig.APPLICATION_ID

        /** Exactly what has to be registered in Google Cloud. */
        val redirectUri: String = "$REDIRECT_SCHEME:/oauth2redirect"

        const val GOOGLE_CLIENT_SUFFIX = ".apps.googleusercontent.com"

        /**
         * SHA-1 of the certificate this APK is actually signed with.
         *
         * The second half of an Android OAuth client registration, and the
         * half that is easy to get wrong: a debug APK is signed with the
         * debug keystore, so the fingerprint from a release build - or from
         * another machine - does not match and Google refuses the request.
         * Reading it off the running app removes the guesswork.
         */
        fun signingSha1(context: Context): String = try {
            val pm = context.packageManager
            val bytes = if (android.os.Build.VERSION.SDK_INT >= 28) {
                val info = pm.getPackageInfo(
                    context.packageName,
                    android.content.pm.PackageManager.GET_SIGNING_CERTIFICATES,
                )
                info.signingInfo?.apkContentsSigners?.firstOrNull()?.toByteArray()
            } else {
                @Suppress("DEPRECATION")
                val info = pm.getPackageInfo(
                    context.packageName,
                    android.content.pm.PackageManager.GET_SIGNATURES,
                )
                @Suppress("DEPRECATION")
                info.signatures?.firstOrNull()?.toByteArray()
            } ?: return "unavailable"
            val cert = java.security.cert.CertificateFactory.getInstance("X.509")
                .generateCertificate(bytes.inputStream())
            java.security.MessageDigest.getInstance("SHA-1")
                .digest(cert.encoded)
                .joinToString(":") { "%02X".format(it) }
        } catch (e: Exception) {
            "unavailable"
        }

        val SCOPES = listOf(
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube",
            "https://www.googleapis.com/auth/yt-analytics.readonly",
        )
    }
}
