package com.autotube.ai.ui.screens

import android.app.Activity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.autotube.ai.auth.YouTubeAuthManager
import com.autotube.ai.ui.components.BannerTone
import com.autotube.ai.ui.components.InfoBanner
import com.autotube.ai.ui.components.LoadingRow
import com.autotube.ai.ui.components.SectionTitle
import com.autotube.ai.ui.vm.SettingsViewModel
import com.autotube.ai.ui.vm.appViewModel
import kotlin.math.roundToInt

private val TIMEZONES = listOf(
    "Asia/Kolkata", "UTC", "America/New_York", "America/Los_Angeles",
    "Europe/London", "Asia/Dubai", "Asia/Singapore", "Australia/Sydney",
)

@Composable
fun SettingsScreen() {
    val vm: SettingsViewModel = appViewModel()
    val store = vm.store
    val health by vm.health.collectAsStateWithLifecycle()
    val youtube by vm.youtube.collectAsStateWithLifecycle()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val message by vm.message.collectAsStateWithLifecycle()
    val context = LocalContext.current

    var backendUrl by remember { mutableStateOf(store.backendUrl) }
    var apiKey by remember { mutableStateOf(store.apiKey) }
    var oauthClientId by remember { mutableStateOf(store.oauthClientId) }
    var defaultNiche by remember { mutableStateOf(store.defaultNiche) }
    var timezone by remember { mutableStateOf(store.timezone) }
    var threshold by remember { mutableIntStateOf(store.qualityThreshold) }
    var autoApprove by remember { mutableStateOf(store.autoApprove) }

    val authManager = remember { YouTubeAuthManager(context, store) }
    DisposableEffect(Unit) { onDispose { authManager.dispose() } }

    val authLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK || result.data != null) {
            authManager.handleResult(result.data) { token, error ->
                when {
                    token != null -> vm.sendRefreshToken(token)
                    error != null -> vm.reportAuthError(error)
                }
            }
        } else {
            vm.reportAuthError("Sign-in was cancelled.")
        }
    }

    LaunchedEffect(Unit) {
        if (store.isConfigured) {
            vm.testConnection()
            vm.refreshYouTube()
        }
    }

    Column(
        Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Settings", style = MaterialTheme.typography.displaySmall)

        message?.let { msg ->
            InfoBanner(
                text = msg.text,
                tone = if (msg.isError) BannerTone.Error else BannerTone.Success,
                actionLabel = "Dismiss",
                onAction = { vm.clearMessage() },
            )
        }
        if (busy) LoadingRow()

        // ---- backend ----------------------------------------------------
        SectionTitle("Backend")
        OutlinedTextField(
            value = backendUrl,
            onValueChange = { backendUrl = it; store.backendUrl = it },
            label = { Text("Backend URL") },
            placeholder = { Text("http://192.168.1.20:8099/") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it; store.apiKey = it },
            label = { Text("Backend API key (AUTOTUBE_API_TOKEN)") },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            modifier = Modifier.fillMaxWidth(),
        )
        Text(
            "Stored encrypted on this device using the Android Keystore. " +
                "HTTPS is required except on a local network address.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Button(onClick = { vm.testConnection() }, enabled = !busy) {
            Text("Test connection")
        }

        health?.let { h ->
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant
                ),
                shape = RoundedCornerShape(12.dp),
            ) {
                Column(Modifier.padding(12.dp)) {
                    SectionTitle("Backend services")
                    ServiceLine("ffmpeg", h.ffmpeg, if (h.ffmpeg) "installed" else "missing")
                    ServiceLine(
                        "LLM",
                        h.llmProviders.isNotEmpty(),
                        h.llmProviders.joinToString().ifBlank { "none configured" },
                    )
                    ServiceLine(
                        "Voice (TTS)",
                        h.ttsProviders.isNotEmpty(),
                        h.ttsProviders.joinToString().ifBlank { "none" },
                    )
                    ServiceLine(
                        "YouTube research",
                        h.researchConfigured,
                        if (h.researchConfigured) "API key set" else "no API key",
                    )
                    ServiceLine(
                        "Uploads",
                        h.uploadEnabled && !h.dryRun,
                        when {
                            h.dryRun -> "DRY RUN - artifacts only, nothing uploaded"
                            !h.uploadEnabled -> "disabled in backend config"
                            else -> "enabled"
                        },
                    )
                }
            }
        }

        // ---- YouTube account --------------------------------------------
        SectionTitle("YouTube account")
        OutlinedTextField(
            value = oauthClientId,
            onValueChange = { oauthClientId = it; store.oauthClientId = it },
            label = { Text("Android OAuth client ID") },
            placeholder = { Text("xxxx.apps.googleusercontent.com") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Text(
            "Create an Android OAuth client in Google Cloud (no client secret " +
                "needed - the app uses PKCE). See docs/YOUTUBE_SETUP.md.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(
                onClick = {
                    runCatching { authManager.launch(authLauncher) }
                        .onFailure { vm.reportAuthError(it.message ?: "Cannot start sign-in") }
                },
                enabled = oauthClientId.isNotBlank() && !busy,
            ) { Text("Connect YouTube") }
            OutlinedButton(onClick = { vm.refreshYouTube() }) { Text("Refresh") }
        }

        youtube?.let { yt ->
            Card(shape = RoundedCornerShape(12.dp)) {
                Column(Modifier.padding(12.dp)) {
                    ServiceLine(
                        "Backend OAuth client",
                        yt.configured,
                        if (yt.configured) "configured" else "missing on backend",
                    )
                    ServiceLine(
                        "Authorised",
                        yt.authorized,
                        if (yt.authorized) "yes" else "not connected",
                    )
                    yt.channels.forEach { channel ->
                        Spacer(Modifier.height(6.dp))
                        Text(channel.title, style = MaterialTheme.typography.bodyMedium)
                        Text(
                            "${channel.subscribers} subscribers - " +
                                "${channel.videos} videos",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    if (yt.error.isNotBlank()) {
                        Text(
                            yt.error,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                }
            }
        }
        Text(
            "Your Google password is never seen or stored by this app. Only an " +
                "OAuth token is used, and it can be revoked from your Google " +
                "account at any time.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        // ---- defaults ----------------------------------------------------
        SectionTitle("Defaults")
        OutlinedTextField(
            value = defaultNiche,
            onValueChange = { defaultNiche = it; store.defaultNiche = it },
            label = { Text("Default niche") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        SectionTitle("Timezone: $timezone")
        Column {
            TIMEZONES.forEach { option ->
                Row(
                    Modifier
                        .fillMaxWidth()
                        .padding(vertical = 2.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Switch(
                        checked = timezone == option,
                        onCheckedChange = {
                            if (it) { timezone = option; store.timezone = option }
                        },
                    )
                    Text(
                        option,
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(start = 10.dp),
                    )
                }
            }
        }

        SectionTitle("Minimum quality score to publish: $threshold/100")
        Slider(
            value = threshold.toFloat(),
            onValueChange = {
                threshold = it.roundToInt()
                store.qualityThreshold = threshold
            },
            valueRange = 50f..95f,
            steps = 8,
        )
        Text(
            "The backend refuses to upload anything below this score. Lowering " +
                "it does not disable the hard blockers (silent audio, wrong " +
                "resolution, failed originality check).",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(
                checked = autoApprove,
                onCheckedChange = { autoApprove = it; store.autoApprove = it },
            )
            Column(Modifier.padding(start = 12.dp)) {
                Text("Default to AUTO mode", style = MaterialTheme.typography.bodyMedium)
                Text(
                    "New automations skip manual approval. Off by default.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        // ---- storage / danger zone --------------------------------------
        SectionTitle("Storage")
        Text(
            "Rendered video stays on the backend. This app caches only job " +
                "metadata and streams previews on demand, to keep phone storage " +
                "use minimal.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedButton(onClick = { vm.clearSecrets() }) {
            Text("Clear stored credentials")
        }

        Spacer(Modifier.height(32.dp))
    }
}

@Composable
private fun ServiceLine(label: String, ok: Boolean, detail: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
        Text(
            if (ok) "OK" else "--",
            style = MaterialTheme.typography.labelSmall,
            color = if (ok) MaterialTheme.colorScheme.tertiary
            else MaterialTheme.colorScheme.error,
            modifier = Modifier.padding(end = 8.dp),
        )
        Column {
            Text(label, style = MaterialTheme.typography.bodySmall)
            Text(
                detail,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
