package com.autotube.ai.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
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
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import com.autotube.ai.ui.components.BannerTone
import com.autotube.ai.ui.components.InfoBanner
import com.autotube.ai.ui.components.LoadingRow
import com.autotube.ai.ui.components.ScoreBar
import com.autotube.ai.ui.components.SectionTitle
import com.autotube.ai.ui.components.StatusChip
import com.autotube.ai.ui.vm.JobViewModel
import com.autotube.ai.ui.vm.appViewModel
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * Screen 5: video preview with title, description, thumbnail and scores,
 * plus the approve/reject decision (spec sections 24 & 32).
 */
// Media3 marks these APIs with @UnstableApi, which is a JAVA annotation
// using androidx.annotation.RequiresOptIn - not Kotlin's @RequiresOptIn.
// kotlin.OptIn therefore compiles but does NOT satisfy it, and lint fails
// with UnsafeOptInUsageError. The androidx form is the one that counts.
// markerClass is a vararg (Class<?>[]), so Kotlin named form needs an array.
@androidx.annotation.OptIn(markerClass = [UnstableApi::class])
@Composable
fun PreviewScreen(jobId: String, onBack: () -> Unit) {
    val vm: JobViewModel = appViewModel()
    val detail by vm.detail.collectAsStateWithLifecycle()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val message by vm.message.collectAsStateWithLifecycle()
    val context = LocalContext.current

    LaunchedEffect(jobId) { vm.load(jobId) }

    val videoPath = detail?.media?.video
    val player = remember(videoPath) {
        if (videoPath == null) {
            null
        } else {
            // The preview endpoint is authenticated, so the player needs the
            // same X-API-Key header the Retrofit client sends.
            val headers = buildMap {
                vm.apiKeyHeader()?.let { put(it.first, it.second) }
            }
            val dataSourceFactory = DefaultHttpDataSource.Factory()
                .setDefaultRequestProperties(headers)
                .setConnectTimeoutMs(20_000)
                .setReadTimeoutMs(30_000)
            ExoPlayer.Builder(context)
                .setMediaSourceFactory(DefaultMediaSourceFactory(dataSourceFactory))
                .build()
                .apply {
                    setMediaItem(MediaItem.fromUri(vm.mediaUrl(videoPath)))
                    prepare()
                    playWhenReady = false
                    repeatMode = Player.REPEAT_MODE_ONE
                }
        }
    }

    DisposableEffect(player) {
        onDispose { player?.release() }
    }

    Column(
        Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row {
            TextButton(onClick = onBack) { Text("< Back") }
            Spacer(Modifier.weight(1f))
            detail?.status?.let { StatusChip(it) }
        }

        message?.let { msg ->
            InfoBanner(
                text = msg.text,
                tone = if (msg.isError) BannerTone.Error else BannerTone.Success,
                actionLabel = "Dismiss",
                onAction = { vm.clearMessage() },
            )
        }

        if (busy && detail == null) LoadingRow("Loading job...")

        val meta = detail?.metadata?.jsonObject
        val quality = detail?.quality?.jsonObject
        val script = detail?.script?.jsonObject

        // ---- player ----------------------------------------------------
        if (player != null) {
            Box(
                Modifier
                    .fillMaxWidth()
                    .aspectRatio(9f / 16f)
                    .clip(RoundedCornerShape(14.dp)),
            ) {
                AndroidView(
                    factory = { ctx ->
                        PlayerView(ctx).apply {
                            this.player = player
                            useController = true
                        }
                    },
                    modifier = Modifier.fillMaxWidth(),
                )
            }
        } else if (detail != null) {
            InfoBanner(
                text = "No video file for this job yet.",
                tone = BannerTone.Warning,
            )
        }

        // ---- metadata --------------------------------------------------
        meta?.let { m ->
            Text(
                m.str("title").ifBlank { "(no title)" },
                style = MaterialTheme.typography.headlineSmall,
            )
            val titleScore = m.num("title_score")
            if (titleScore > 0) ScoreBar("Title score", titleScore, threshold = 70)

            val desc = m.str("description")
            if (desc.isNotBlank()) {
                SectionTitle("Description")
                Text(desc, style = MaterialTheme.typography.bodySmall)
            }

            val tags = m.strList("tags")
            if (tags.isNotEmpty()) {
                SectionTitle("Tags")
                Text(
                    tags.joinToString(", "),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            Card(
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant
                ),
                shape = RoundedCornerShape(12.dp),
            ) {
                Column(Modifier.padding(12.dp)) {
                    KeyValue("Privacy", m.str("privacy"))
                    m.str("publish_at").takeIf { it.isNotBlank() }?.let {
                        KeyValue("Scheduled (UTC)", it)
                    }
                    KeyValue("Made for kids", m.bool("made_for_kids").toString())
                    KeyValue(
                        "AI disclosure",
                        if (m.bool("synthetic_disclosure")) "declared" else "not declared",
                    )
                }
            }
        }

        // ---- scores ----------------------------------------------------
        quality?.let { q ->
            SectionTitle("Quality gate")
            ScoreBar("Quality", q.num("score"))
            script?.let {
                Spacer(Modifier.height(6.dp))
                ScoreBar("Retention", it.num("retention_score"), threshold = 70)
            }
            val blockers = q.strList("blockers")
            val warnings = q.strList("warnings")
            if (blockers.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Blockers - upload is refused:",
                    style = MaterialTheme.typography.bodySmall,
                    fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.error,
                )
                blockers.forEach {
                    Text(
                        "- $it",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
            if (warnings.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                Text("Warnings:", style = MaterialTheme.typography.bodySmall)
                warnings.take(6).forEach {
                    Text(
                        "- $it",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        // ---- script ----------------------------------------------------
        script?.let { s ->
            val hook = s.str("hook")
            if (hook.isNotBlank()) {
                SectionTitle("Hook")
                Text(hook, style = MaterialTheme.typography.bodyMedium)
            }
            val notes = s.strList("retention_notes")
            if (notes.isNotEmpty()) {
                SectionTitle("Retention notes")
                notes.take(6).forEach {
                    Text("- $it", style = MaterialTheme.typography.bodySmall)
                }
            }
            val body = s.str("script")
            if (body.isNotBlank()) {
                SectionTitle("Narration")
                Text(body, style = MaterialTheme.typography.bodySmall)
            }
        }

        // ---- decision --------------------------------------------------
        if (detail?.status == "AWAITING_APPROVAL") {
            Spacer(Modifier.height(4.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(
                    onClick = { vm.approve(jobId) },
                    enabled = !busy,
                    modifier = Modifier.weight(1f),
                ) { Text("Approve & publish") }
                OutlinedButton(
                    onClick = { vm.reject(jobId, "rejected from preview") },
                    enabled = !busy,
                ) { Text("Reject") }
            }
        }

        detail?.logs?.takeIf { it.isNotEmpty() }?.let { logs ->
            SectionTitle("Log")
            logs.takeLast(14).forEach {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        Spacer(Modifier.height(32.dp))
    }
}

@Composable
private fun KeyValue(label: String, value: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(
            label,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(0.45f),
        )
        Text(
            value,
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.weight(0.55f),
        )
    }
}

// --------------------------------------------------------------------------
// Small JSON helpers. The backend returns nested job payloads whose shape
// evolves, so the UI reads fields defensively instead of failing to parse.
private fun kotlinx.serialization.json.JsonObject.str(key: String): String =
    runCatching { this[key]?.jsonPrimitive?.content.orEmpty() }.getOrDefault("")

private fun kotlinx.serialization.json.JsonObject.num(key: String): Double =
    runCatching { this[key]?.jsonPrimitive?.content?.toDouble() ?: 0.0 }
        .getOrDefault(0.0)

private fun kotlinx.serialization.json.JsonObject.bool(key: String): Boolean =
    runCatching { this[key]?.jsonPrimitive?.content?.toBooleanStrict() ?: false }
        .getOrDefault(false)

private fun kotlinx.serialization.json.JsonObject.strList(key: String): List<String> =
    runCatching {
        (this[key] as? JsonElement)?.jsonArray?.map { it.jsonPrimitive.content }
            ?: emptyList()
    }.getOrDefault(emptyList())
