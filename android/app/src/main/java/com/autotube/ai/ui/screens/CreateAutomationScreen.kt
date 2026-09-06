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
import androidx.compose.runtime.saveable.rememberSaveable
import com.autotube.ai.ui.components.LabeledDropdown
import kotlinx.coroutines.delay

// Common niches. "Other…" in the dropdown opens a free-text field, so this
// does not need to be exhaustive - it only needs to cover the usual cases
// without making the user type.
val NICHE_OPTIONS = listOf(
    "science", "space", "technology", "AI", "history", "interesting facts",
    "psychology", "finance basics", "productivity", "programming",
    "nature", "animals", "geography", "health myths", "food science",
    "ancient engineering", "true stories", "kids bedtime stories",
    "kids alphabet learning", "kids numbers and counting",
)

// Niches that are child-directed by definition. Picking one of these sets the
// Made for Kids flag without prompting: being asked to confirm on every
// keystroke, for a niche literally named "kids", is noise rather than consent.
val KIDS_NICHES = setOf(
    "kids bedtime stories", "kids alphabet learning", "kids numbers and counting",
)

// Four languages, not twelve.
//
// The backend still has voices for more, but offering them here invited a
// choice nobody wanted to make. "hi-Latn" is Hinglish: Latin-script
// Hindi-English code-mixing, voiced by an Indian-English speaker, because a
// Hindi voice expects Devanagari and mispronounces romanised text.
val LANGUAGES = listOf(
    "hi" to "Hindi",
    "en-IN" to "Indian English",
    "en" to "English",
    "hi-Latn" to "Hinglish",
)

val STYLES = listOf(
    "fast-paced, curiosity-driven",
    "calm and cinematic",
    "storytelling",
    "educational and clear",
    "high-energy entertainment",
    "gentle and simple (for young children)",
)

// Under-13 bands were missing entirely, which made it impossible to describe
// the audience for children's content - the one category where age actually
// changes the safety profile and the vocabulary.
val AUDIENCES = listOf(
    "2-4", "5-7", "8-12", "13-17", "18-24", "18-35", "25-44", "35+", "all ages",
)

val FREQUENCIES = listOf("once", "daily", "weekly", "days")
private val WEEKDAYS = listOf("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun CreateAutomationScreen(onStarted: () -> Unit) {
    val vm: CreateViewModel = appViewModel()
    val preview by vm.preview.collectAsStateWithLifecycle()
    val forcePrivate by vm.forcePrivate.collectAsStateWithLifecycle()
    val kidsPrompt by vm.kidsPrompt.collectAsStateWithLifecycle()
    val started by vm.started.collectAsStateWithLifecycle()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val message by vm.message.collectAsStateWithLifecycle()

    // rememberSaveable, NOT remember.
    //
    // The bottom bar navigates with saveState/restoreState, which restores the
    // NavBackStackEntry - but plain `remember` is not part of that. Every
    // field reset to its default the moment you switched tab and came back,
    // silently discarding whatever had been filled in.
    var niche by rememberSaveable { mutableStateOf(vm.store.defaultNiche) }
    var audience by rememberSaveable { mutableStateOf("18-35") }
    var language by rememberSaveable { mutableStateOf(vm.store.defaultLanguage) }
    var isShort by rememberSaveable { mutableStateOf(true) }
    var lengthSeconds by rememberSaveable { mutableIntStateOf(45) }
    var style by rememberSaveable { mutableStateOf(STYLES.first()) }
    var frequency by rememberSaveable { mutableStateOf("daily") }
    // A List, not a Set: ArrayList is saveable out of the box, whereas a Set
    // needs a custom Saver whose types Kotlin cannot infer through the `by`
    // delegate. Order is irrelevant here - it is sorted before being sent.
    var selectedDays by rememberSaveable { mutableStateOf(listOf(0, 1, 2, 3, 4)) }
    var uploadTime by rememberSaveable { mutableStateOf("20:00") }
    var count by rememberSaveable { mutableIntStateOf(1) }
    var autoMode by rememberSaveable { mutableStateOf(vm.store.autoApprove) }
    var madeForKids by rememberSaveable { mutableStateOf(false) }
    // Remembers which niche the kids question was already answered for, so it
    // is asked once per niche rather than on every preview refresh.
    var kidsAnsweredFor by rememberSaveable { mutableStateOf("") }
    // "scheduled" keeps the previous behaviour; frequency and publish timing
    // used to be the same setting, so a daily automation could not put each
    // video up straight away.
    var publishMode by rememberSaveable { mutableStateOf("scheduled") }

    // A niche whose name says "kids" needs no confirmation dialog.
    val nicheIsKids = niche.trim().lowercase() in KIDS_NICHES
    LaunchedEffect(nicheIsKids) {
        if (nicheIsKids) {
            madeForKids = true
            audience = if (audience in listOf("2-4", "5-7", "8-12")) audience else "5-7"
            vm.dismissKidsPrompt()
        }
    }

    // Ask the backend how it will interpret the niche. DEBOUNCED so that
    // typing a custom topic fires one request instead of one per keystroke.
    //
    // (The rate limiter is NOT why this screen used to report "cannot reach
    // backend": /niche/preview answers 200 in under a second and survives a
    // 10-call burst. The transport was the problem - see RetryIdempotent in
    // ApiClient.)
    LaunchedEffect(niche, audience, style, lengthSeconds) {
        if (niche.trim().length >= 3) {
            delay(600)
            vm.previewNiche(niche.trim(), audience, style, lengthSeconds)
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
        LabeledDropdown(
            label = "Topic",
            value = niche,
            options = NICHE_OPTIONS,
            allowOther = true,
            otherLabel = "Other topic…",
            onValueChange = { niche = it },
        )
        if (nicheIsKids) {
            Text(
                "Child-directed niche: Made for Kids is set automatically and " +
                    "the stricter safety profile applies.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary,
            )
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
        LabeledDropdown(
            label = "Audience age",
            value = audience,
            options = AUDIENCES,
            onValueChange = { audience = it },
        )

        LabeledDropdown(
            label = "Language",
            value = language,
            options = LANGUAGES.map { it.first },
            display = { code -> LANGUAGES.firstOrNull { it.first == code }?.second ?: code },
            onValueChange = { language = it },
        )

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

        LabeledDropdown(
            label = "Style",
            value = style,
            options = STYLES,
            allowOther = true,
            otherLabel = "Other style…",
            onValueChange = { style = it },
        )

        // ---- schedule ---------------------------------------------------
        LabeledDropdown(
            label = "Upload frequency",
            value = frequency,
            options = FREQUENCIES,
            display = {
                when (it) {
                    "once" -> "Just once"
                    "daily" -> "Daily"
                    "weekly" -> "Weekly"
                    else -> "Specific days"
                }
            },
            onValueChange = { frequency = it },
        )

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
                            }.distinct()
                        },
                        label = { Text(day) },
                    )
                }
            }
        }

        // ---- publish mode ------------------------------------------------
        SectionTitle("Publishing")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = publishMode == "immediate",
                onClick = { publishMode = "immediate" },
                label = { Text("Publish immediately") },
            )
            FilterChip(
                selected = publishMode == "scheduled",
                onClick = { publishMode = "scheduled" },
                label = { Text("Schedule") },
            )
        }
        Text(
            when {
                forcePrivate ->
                    "The backend is set to force private, so every upload stays " +
                        "private and nothing is scheduled - publishing has no " +
                        "effect until that is turned off. Useful for checking " +
                        "that uploads work without subscribers seeing anything."
                publishMode == "immediate" ->
                    "Uploaded as soon as you approve the video."
                else ->
                    "Handed to YouTube with a scheduled publish time, so your " +
                        "phone does not need to be online for it."
            },
            style = MaterialTheme.typography.bodySmall,
            color = if (forcePrivate) MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurfaceVariant,
        )

        if (frequency != "once" && publishMode == "scheduled") {
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
                        uploadTime = if (frequency == "once" ||
                            publishMode == "immediate") "" else uploadTime,
                        publishMode = publishMode,
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

    // Kids confirmation (spec section 9): explicit, blocking, before anything
    // runs - but asked ONCE PER NICHE, not on every preview refresh. The
    // preview is re-requested whenever the niche, audience, style or length
    // changes, and the prompt was re-armed from its result each time, so the
    // dialog reappeared constantly. A consent dialog you have to dismiss
    // repeatedly stops being consent and becomes an obstacle.
    val askKids = kidsPrompt && !madeForKids && !nicheIsKids &&
        kidsAnsweredFor != niche.trim().lowercase()
    if (askKids) {
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
                    kidsAnsweredFor = niche.trim().lowercase()
                    vm.dismissKidsPrompt()
                }) { Text("Yes, made for kids") }
            },
            dismissButton = {
                TextButton(onClick = {
                    kidsAnsweredFor = niche.trim().lowercase()
                    vm.dismissKidsPrompt()
                }) {
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
