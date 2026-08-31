package com.autotube.ai.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.autotube.ai.data.remote.AutomationRequestDto
import com.autotube.ai.ui.components.BannerTone
import com.autotube.ai.ui.components.InfoBanner
import com.autotube.ai.ui.components.LoadingRow
import com.autotube.ai.ui.components.SectionTitle
import com.autotube.ai.ui.vm.CreateViewModel
import com.autotube.ai.ui.vm.appViewModel
import kotlin.math.roundToInt

private val NICHE_SUGGESTIONS = listOf(
    "science", "space", "technology", "AI", "history", "interesting facts",
    "psychology", "finance basics", "productivity", "programming", "cars",
    "travel", "gaming", "kids bedtime stories", "health myths",
)

private val LANGUAGES = listOf(
    "en" to "English",
    "en-IN" to "Indian English",
    "hi" to "Hindi",
    "ta" to "Tamil",
    "te" to "Telugu",
    "bn" to "Bengali",
    "mr" to "Marathi",
    "es" to "Spanish",
)

private val STYLES = listOf(
    "fast-paced, curiosity-driven",
    "calm and cinematic",
    "storytelling",
    "educational and clear",
    "high-energy entertainment",
)

private val AUDIENCES = listOf("13-17", "18-24", "18-35", "25-44", "35+", "all ages")
private val WEEKDAYS = listOf("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CreateAutomationScreen(onStarted: () -> Unit) {
    val vm: CreateViewModel = appViewModel()
    val preview by vm.preview.collectAsStateWithLifecycle()
    val kidsPrompt by vm.kidsPrompt.collectAsStateWithLifecycle()
    val started by vm.started.collectAsStateWithLifecycle()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val message by vm.message.collectAsStateWithLifecycle()

    var niche by remember { mutableStateOf(vm.store.defaultNiche) }
    var audience by remember { mutableStateOf("18-35") }
    var language by remember { mutableStateOf(vm.store.defaultLanguage) }
    var isShort by remember { mutableStateOf(true) }
    var lengthSeconds by remember { mutableIntStateOf(45) }
    var style by remember { mutableStateOf(STYLES.first()) }
    var frequency by remember { mutableStateOf("daily") }
    var selectedDays by remember { mutableStateOf(setOf(0, 1, 2, 3, 4)) }
    var uploadTime by remember { mutableStateOf("20:00") }
    var count by remember { mutableIntStateOf(1) }
    var autoMode by remember { mutableStateOf(vm.store.autoApprove) }
    var madeForKids by remember { mutableStateOf(false) }

    // Ask the backend how it will interpret the niche (and whether it looks
    // child-directed) as the user types.
    LaunchedEffect(niche, audience, style, lengthSeconds) {
        if (niche.length >= 3) {
            vm.previewNiche(niche, audience, style, lengthSeconds)
        }
    }

    LaunchedEffect(started) {
        if (started) {
            vm.resetStarted()
            onStarted()
        }
    }

    Column(
        Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Create automation", style = MaterialTheme.typography.displaySmall)

        message?.let { msg ->
            InfoBanner(
                text = msg.text,
                tone = if (msg.isError) BannerTone.Error else BannerTone.Success,
                actionLabel = "Dismiss",
                onAction = { vm.clearMessage() },
            )
        }

        // ---- niche ------------------------------------------------------
        SectionTitle("Niche")
        OutlinedTextField(
            value = niche,
            onValueChange = { niche = it },
            label = { Text("Any topic") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            NICHE_SUGGESTIONS.forEach { suggestion ->
                AssistChip(
                    onClick = { niche = suggestion },
                    label = { Text(suggestion, style = MaterialTheme.typography.labelSmall) },
                )
            }
        }

        // How the backend interpreted it - transparency about what will be made.
        preview?.let { p ->
            Card(
                colors = CardDefaults.cardColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant
                ),
                shape = RoundedCornerShape(12.dp),
            ) {
                Column(Modifier.padding(12.dp)) {
                    SectionTitle("Interpreted profile")
                    Text(
                        "Tone: ${p.profile.tone}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        "Visuals: ${p.profile.visualStyle}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        "Pace: ${p.profile.pacing} - a new image about every " +
                            "${p.profile.sceneSeconds}s",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (p.profile.requiresFactCheck) {
                        Text(
                            "Factual niche: claims will be checked and risky " +
                                "ones flagged for review.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.primary,
                        )
                    }
                    p.profile.disclaimers.forEach {
                        Text(
                            "Disclaimer added: $it",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.primary,
                        )
                    }
                }
            }
        }

        // ---- audience + language ---------------------------------------
        SectionTitle("Audience")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            AUDIENCES.forEach { option ->
                FilterChip(
                    selected = audience == option,
                    onClick = { audience = option },
                    label = { Text(option) },
                )
            }
        }

        SectionTitle("Language")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            LANGUAGES.forEach { (code, label) ->
                FilterChip(
                    selected = language == code,
                    onClick = { language = code },
                    label = { Text(label) },
                )
            }
        }

        // ---- format + length -------------------------------------------
        SectionTitle("Format")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = isShort,
                onClick = {
                    isShort = true
                    if (lengthSeconds > 180) lengthSeconds = 45
                },
                label = { Text("Short (9:16)") },
            )
            FilterChip(
                selected = !isShort,
                onClick = {
                    isShort = false
                    if (lengthSeconds < 180) lengthSeconds = 300
                },
                label = { Text("Long-form (16:9)") },
            )
        }

        SectionTitle("Length: ${formatLength(lengthSeconds)}")
        Slider(
            value = lengthSeconds.toFloat(),
            onValueChange = { lengthSeconds = it.roundToInt() },
            // Long-form goes to the backend's own ceiling (3600s). Nothing in
            // the pipeline generates video, so length is not capped by a
            // service - only by YouTube's account limits and render time.
            valueRange = if (isShort) 15f..180f else 120f..3600f,
            steps = if (isShort) 32 else 57,
        )
        if (isShort && lengthSeconds > 60) {
            Text(
                "Shorts up to 3 minutes are supported by YouTube, but 30-60s " +
                    "usually retains best.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (!isShort && lengthSeconds > 900) {
            Text(
                "Over 15 minutes needs a verified YouTube account: Studio -> " +
                    "Settings -> Channel -> Feature eligibility. It is free. " +
                    "Unverified channels are refused at upload, not here.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
        if (!isShort) {
            Text(
                "Long-form needs an LLM key on the backend (Groq or Gemini, " +
                    "both free). Roughly ${estimateRenderMinutes(lengthSeconds)} " +
                    "minutes of render time on a laptop.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        SectionTitle("Style")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            STYLES.forEach { option ->
                FilterChip(
                    selected = style == option,
                    onClick = { style = option },
                    label = { Text(option, style = MaterialTheme.typography.labelSmall) },
                )
            }
        }

        // ---- schedule ---------------------------------------------------
        SectionTitle("Upload frequency")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            listOf(
                "once" to "Just once",
                "daily" to "Daily",
                "weekly" to "Weekly",
                "days" to "Specific days",
            ).forEach { (value, label) ->
                FilterChip(
                    selected = frequency == value,
                    onClick = { frequency = value },
                    label = { Text(label) },
                )
            }
        }

        if (frequency == "days") {
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                WEEKDAYS.forEachIndexed { index, day ->
                    FilterChip(
                        selected = index in selectedDays,
                        onClick = {
                            selectedDays = if (index in selectedDays) {
                                selectedDays - index
                            } else {
                                selectedDays + index
                            }
                        },
                        label = { Text(day) },
                    )
                }
            }
        }

        if (frequency != "once") {
            OutlinedTextField(
                value = uploadTime,
                onValueChange = { input ->
                    // Keep it to HH:MM; the backend validates too.
                    if (input.length <= 5) uploadTime = input
                },
                label = { Text("Publish time (24h, ${vm.store.timezone})") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Text(
                "The video is produced ahead of time and handed to YouTube with a " +
                    "scheduled publish time, so your phone does not need to be " +
                    "online at ${uploadTime}.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        SectionTitle("Number of videos this run: $count")
        Slider(
            value = count.toFloat(),
            onValueChange = { count = it.roundToInt() },
            valueRange = 1f..5f,
            steps = 3,
        )

        // ---- mode -------------------------------------------------------
        SectionTitle("Mode")
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(checked = autoMode, onCheckedChange = { autoMode = it })
            Spacer(Modifier.height(0.dp))
            Column(Modifier.padding(start = 12.dp)) {
                Text(
                    if (autoMode) "AUTO - publish without asking"
                    else "APPROVAL - I review before publishing",
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    if (autoMode) {
                        "Quality gate still blocks anything below your threshold."
                    } else {
                        "Recommended until you trust the output."
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(checked = madeForKids, onCheckedChange = { madeForKids = it })
            Column(Modifier.padding(start = 12.dp)) {
                Text("Made for Kids", style = MaterialTheme.typography.bodyMedium)
                Text(
                    "Sets YouTube's child-directed classification. Required by " +
                        "law if the content targets children.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        if (busy) LoadingRow("Queueing...")

        Button(
            onClick = {
                vm.start(
                    AutomationRequestDto(
                        niche = niche.trim(),
                        audience = audience,
                        language = language,
                        videoFormat = if (isShort) "SHORT" else "LONGFORM",
                        durationSeconds = lengthSeconds,
                        style = style,
                        count = count,
                        mode = if (autoMode) "AUTO" else "APPROVAL",
                        frequency = frequency,
                        days = if (frequency == "days") selectedDays.sorted() else emptyList(),
                        uploadTime = if (frequency == "once") "" else uploadTime,
                        timezone = vm.store.timezone,
                        madeForKids = madeForKids,
                    )
                )
            },
            enabled = !busy && niche.trim().length >= 2,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("START AUTOMATION")
        }

        Spacer(Modifier.height(32.dp))
    }

    // Kids confirmation (spec section 9): explicit, blocking, before anything runs.
    if (kidsPrompt && !madeForKids) {
        AlertDialog(
            onDismissRequest = { vm.dismissKidsPrompt() },
            title = { Text("Is this content for children?") },
            text = {
                Text(
                    "This niche looks child-directed. YouTube requires an accurate " +
                        "\"Made for Kids\" classification, and getting it wrong has " +
                        "legal consequences.\n\nTurning this on also enables a stricter " +
                        "safety profile: no scary or unsafe content, simpler language, " +
                        "and no commercial prompts."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    madeForKids = true
                    vm.dismissKidsPrompt()
                }) { Text("Yes, made for kids") }
            },
            dismissButton = {
                TextButton(onClick = { vm.dismissKidsPrompt() }) {
                    Text("No, general audience")
                }
            },
        )
    }
}

private fun formatLength(seconds: Int): String =
    if (seconds >= 60) "${seconds / 60}m ${seconds % 60}s" else "${seconds}s"

/**
 * Rough render-time estimate.
 *
 * Measured end to end on the dev laptop: a 240-second long-form video took
 * about 62 minutes, i.e. roughly 15x realtime. The final encode and the
 * per-scene clips dominate; image generation adds more when a free provider
 * is rate limiting. Showing a number here stops a 20-minute request looking
 * like a hang. A machine with a GPU or more cores will beat it comfortably.
 */
private fun estimateRenderMinutes(seconds: Int): Int =
    ((seconds * 15.0) / 60).toInt().coerceAtLeast(2)
