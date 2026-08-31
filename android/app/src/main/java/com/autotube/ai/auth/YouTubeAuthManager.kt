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
        val clientId = store.oauthClientId
        require(clientId.isNotBlank()) {
            "Set the Android OAuth client ID in Settings first."
        }
        val request = AuthorizationRequest.Builder(
            serviceConfig,
            clientId,
            ResponseTypeValues.CODE,
            Uri.parse("$REDIRECT_SCHEME:/oauth2redirect"),
        )
            .setScopes(SCOPES)
            .setPrompt(AuthorizationRequest.Prompt.CONSENT)
            .setAdditionalParameters(mapOf("access_type" to "offline"))
            .build()
        return authService.getAuthorizationRequestIntent(request)
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

        val SCOPES = listOf(
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube",
            "https://www.googleapis.com/auth/yt-analytics.readonly",
        )
    }
}
