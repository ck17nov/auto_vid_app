package com.autotube.ai.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.autotube.ai.data.local.JobEntity
import com.autotube.ai.ui.components.BannerTone
import com.autotube.ai.ui.components.EmptyState
import com.autotube.ai.ui.components.InfoBanner
import com.autotube.ai.ui.components.LoadingRow
import com.autotube.ai.ui.components.MetricTile
import com.autotube.ai.ui.components.ScoreBar
import com.autotube.ai.ui.components.SectionTitle
import com.autotube.ai.ui.components.StatusChip
import com.autotube.ai.ui.components.compactNumber
import com.autotube.ai.ui.vm.DashboardViewModel
import com.autotube.ai.ui.vm.appViewModel

@Composable
fun DashboardScreen(
    onOpenJob: (String) -> Unit,
    onCreate: () -> Unit,
    onOpenSettings: () -> Unit,
    onOpenContent: () -> Unit,
) {
    val vm: DashboardViewModel = appViewModel()
    val jobs by vm.jobs.collectAsStateWithLifecycle()
    val awaiting by vm.awaitingApproval.collectAsStateWithLifecycle()
    val published by vm.publishedCount.collectAsStateWithLifecycle()
    val scheduled by vm.scheduledCount.collectAsStateWithLifecycle()
    val failed by vm.failedCount.collectAsStateWithLifecycle()
    val today by vm.todayCount.collectAsStateWithLifecycle()
    val views by vm.totalViews.collectAsStateWithLifecycle()
    val health by vm.health.collectAsStateWithLifecycle()
    val quota by vm.quota.collectAsStateWithLifecycle()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val message by vm.message.collectAsStateWithLifecycle()

    LaunchedEffect(Unit) { if (vm.isConfigured) vm.refresh() }

    LazyColumn(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Row(
                Modifier.fillMaxWidth().padding(top = 16.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text("AutoTube AI", style = MaterialTheme.typography.displaySmall)
                    Text(
                        "Research to publish, automatically",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                IconButton(onClick = { vm.refresh() }) {
                    Icon(Icons.Filled.Refresh, contentDescription = "Refresh")
                }
            }
        }

        if (!vm.isConfigured) {
            item {
                InfoBanner(
                    text = "Connect to your backend to begin. You need the backend " +
                        "URL and its API key.",
                    tone = BannerTone.Warning,
                    actionLabel = "Settings",
                    onAction = onOpenSettings,
                )
            }
        }

        message?.let { msg ->
            item {
                InfoBanner(
                    text = msg.text,
                    tone = if (msg.isError) BannerTone.Error else BannerTone.Success,
                    actionLabel = "Dismiss",
                    onAction = { vm.clearMessage() },
                )
            }
        }

        if (busy) item { LoadingRow("Syncing with backend...") }

        // ---- headline metrics ------------------------------------------
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                MetricTile(
                    "Today", today.toString(), Modifier.weight(1f),
                    hint = "videos completed",
                )
                MetricTile(
                    "Queue", jobs.count { it.status.isWorking() }.toString(),
                    Modifier.weight(1f), hint = "in progress",
                )
            }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                MetricTile(
                    "Published", published.toString(), Modifier.weight(1f),
                    accent = MaterialTheme.colorScheme.tertiary,
                )
                MetricTile("Scheduled", scheduled.toString(), Modifier.weight(1f))
                MetricTile(
                    "Failed", failed.toString(), Modifier.weight(1f),
                    accent = if (failed > 0) MaterialTheme.colorScheme.error else null,
                )
            }
        }
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                MetricTile(
                    "Views", views?.let { compactNumber(it) } ?: "--",
                    Modifier.weight(1f), hint = "own channel",
                )
                // Revenue is a placeholder by design: YouTube revenue requires
                // monetisation plus a separate reporting scope (spec section 32).
                MetricTile(
                    "Revenue", "--", Modifier.weight(1f),
                    hint = "needs monetisation",
                )
            }
        }

        // ---- backend status --------------------------------------------
        health?.let { h ->
            item {
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.surfaceVariant
                    ),
                    shape = RoundedCornerShape(14.dp),
                ) {
                    Column(Modifier.padding(14.dp)) {
                        SectionTitle("Backend")
                        StatusLine("ffmpeg", if (h.ffmpeg) "ready" else "MISSING")
                        StatusLine(
                            "LLM",
                            h.llmProviders.joinToString().ifBlank {
                                "none - scripts will use the template fallback"
                            },
                        )
                        StatusLine("Voice", h.ttsProviders.joinToString().ifBlank { "none" })
                        StatusLine(
                            "Research",
                            if (h.researchConfigured) "YouTube API key set"
                            else "no YouTube API key",
                        )
                        StatusLine(
                            "Mode",
                            buildString {
                                append(if (h.approvalRequired) "APPROVAL" else "AUTO")
                                if (h.dryRun) append(" - DRY RUN (no uploads)")
                                else if (!h.uploadEnabled) append(" - uploads disabled")
                            },
                        )
                        quota?.let { q ->
                            StatusLine(
                                "YouTube quota",
                                "${q.usedToday}/${q.limit} units - " +
                                    "max ${q.maxUploadsPerDay} uploads/day",
                            )
                        }
                    }
                }
            }
        }

        // ---- approvals -------------------------------------------------
        if (awaiting.isNotEmpty()) {
            item { SectionTitle("Waiting for your approval") }
            // Keys are namespaced per section, NOT the bare job id.
            //
            // A job awaiting approval is in both `awaiting` and `jobs`, so a
            // bare jobId appeared twice in one LazyColumn. Compose's
            // SaveableStateHolder throws "Key was used multiple times" the
            // moment both copies are composed - which is why the app died on
            // scroll, and on launch once a job was near the top, and why the
            // only apparent cure was clearing app data: that wiped the job
            // database and with it the duplicate.
            items(awaiting, key = { "approval-${it.jobId}" }) { job ->
                ApprovalCard(
                    job = job,
                    onOpen = { onOpenJob(job.jobId) },
                    onApprove = { vm.approve(job.jobId) },
                    onReject = { vm.reject(job.jobId) },
                )
            }
        }

        // ---- queue -----------------------------------------------------
        item {
            Row(
                Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                SectionTitle("Recent jobs", Modifier.weight(1f))
                TextButton(onClick = onOpenContent) { Text("All content") }
            }
        }

        if (jobs.isEmpty()) {
            item {
                EmptyState(
                    title = "No videos yet",
                    body = "Create an automation to research your niche and " +
                        "produce your first video.",
                    actionLabel = "Create automation",
                    onAction = onCreate,
                )
            }
        } else {
            items(jobs.take(20), key = { "recent-${it.jobId}" }) { job ->
                JobRow(job = job, onClick = { onOpenJob(job.jobId) })
            }
        }

        item { Spacer(Modifier.height(24.dp)) }
    }
}

private fun String.isWorking(): Boolean = uppercase() in setOf(
    "IDEA", "RESEARCH", "SCRIPT", "VOICE", "VISUALS", "RENDERING", "QUALITY_CHECK",
)

@Composable
private fun StatusLine(label: String, value: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(
            label,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(104.dp),
        )
        Text(value, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun ApprovalCard(
    job: JobEntity,
    onOpen: () -> Unit,
    onApprove: () -> Unit,
    onReject: () -> Unit,
) {
    Card(shape = RoundedCornerShape(14.dp)) {
        Column(Modifier.padding(14.dp)) {
            Text(
                job.title.ifBlank { job.niche },
                style = MaterialTheme.typography.titleMedium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(8.dp))
            ScoreBar("Quality", job.qualityScore)
            Spacer(Modifier.height(6.dp))
            ScoreBar("Retention", job.retentionScore, threshold = 70)
            if (job.blockers.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "Blockers: ${job.blockers.joinToString("; ")}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
            Spacer(Modifier.height(12.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onApprove, modifier = Modifier.weight(1f)) {
                    Text("Approve")
                }
                OutlinedButton(onClick = onOpen, modifier = Modifier.weight(1f)) {
                    Text("Preview")
                }
                OutlinedButton(onClick = onReject) { Text("Reject") }
            }
        }
    }
}

@Composable
fun JobRow(job: JobEntity, onClick: () -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth().clickable(onClick = onClick),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface
        ),
        shape = RoundedCornerShape(12.dp),
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    job.title.ifBlank { "(${job.niche})" },
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                StatusChip(job.status)
            }
            Spacer(Modifier.height(4.dp))
            Text(
                buildString {
                    append(job.niche)
                    if (job.duration > 0) append(" - ${job.duration.toInt()}s")
                    if (job.qualityScore > 0) {
                        append(" - quality ${job.qualityScore.toInt()}/100")
                    }
                    if (job.retryCount > 0) append(" - ${job.retryCount} retries")
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (job.error.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    job.error,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}
