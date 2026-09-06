package com.autotube.ai.ui.screens

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
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.autotube.ai.data.remote.ContentGapDto
import com.autotube.ai.data.remote.ResearchVideoDto
import com.autotube.ai.data.remote.TopicClusterDto
import com.autotube.ai.ui.components.BannerTone
import com.autotube.ai.ui.components.EmptyState
import com.autotube.ai.ui.components.InfoBanner
import com.autotube.ai.ui.components.LoadingRow
import com.autotube.ai.ui.components.ScoreBar
import com.autotube.ai.ui.components.SectionTitle
import com.autotube.ai.ui.components.compactNumber
import com.autotube.ai.ui.vm.ResearchViewModel
import com.autotube.ai.ui.vm.appViewModel

@Composable
fun ResearchScreen() {
    val vm: ResearchViewModel = appViewModel()
    val result by vm.result.collectAsStateWithLifecycle()
    val cached by vm.cached.collectAsStateWithLifecycle()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val message by vm.message.collectAsStateWithLifecycle()
    var niche by remember { mutableStateOf(vm.store.defaultNiche) }

    LazyColumn(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Text(
                "Research",
                style = MaterialTheme.typography.displaySmall,
                modifier = Modifier.padding(top = 16.dp),
            )
        }

        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = niche,
                    onValueChange = { niche = it; vm.setNiche(it) },
                    label = { Text("Niche") },
                    singleLine = true,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                Button(onClick = { vm.setNiche(niche); vm.run() }, enabled = !busy) {
                    Text("Run")
                }
            }
        }

        message?.let { msg ->
            item {
                InfoBanner(
                    text = msg.text,
                    tone = if (msg.isError) BannerTone.Error else BannerTone.Info,
                    actionLabel = "Dismiss",
                    onAction = { vm.clearMessage() },
                )
            }
        }

        if (busy) {
            item {
                LoadingRow(
                    "Querying the YouTube Data API and scoring results..."
                )
            }
        }

        result?.let { r ->
            item {
                InfoBanner(
                    text = "API quota used today: ${r.quotaUsedToday}/${r.quotaLimit} " +
                        "units. Each search costs 100; an upload costs 1600.",
                    tone = BannerTone.Info,
                )
            }
            // The spec is explicit: never imply we know a competitor's CTR.
            item {
                Text(
                    r.disclaimer.ifBlank {
                        "CTR potential is a heuristic over public signals. " +
                            "YouTube does not expose other channels' real CTR."
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            if (r.gaps.isNotEmpty()) {
                item { SectionTitle("Content opportunities") }
                // Namespaced: a topic can be both a gap and a cluster, and a duplicate
                // key in one LazyColumn is a crash, not a glitch.
                items(r.gaps, key = { "gap-${it.topic}" }) { gap -> GapCard(gap) }
            }

            if (r.clusters.isNotEmpty()) {
                item { SectionTitle("Topic momentum") }
                items(r.clusters.take(6), key = { "cluster-${it.topic}" }) { c -> ClusterRow(c) }
            }

            item { SectionTitle("Top videos (${r.videos.size})") }
            items(r.videos, key = { "live-${it.videoId}" }) { v -> ResearchVideoRow(v) }
        }

        if (result == null && !busy) {
            if (cached.isEmpty()) {
                item {
                    EmptyState(
                        title = "No research yet",
                        body = "Run research to find recent, fast-growing videos " +
                            "in your niche and the angles nobody has covered.",
                    )
                }
            } else {
                item { SectionTitle("Cached results") }
                items(cached, key = { "cached-${it.videoId}" }) { entity ->
                    ResearchVideoRow(
                        ResearchVideoDto(
                            videoId = entity.videoId,
                            title = entity.title,
                            channelTitle = entity.channelTitle,
                            views = entity.views,
                            ageDays = entity.ageDays,
                            viewVelocity = entity.viewVelocity,
                            engagementRate = entity.engagementRate,
                            performanceRatio = entity.performanceRatio,
                            isBreakout = entity.isBreakout,
                            viralScore = entity.viralScore,
                            ctrPotentialScore = entity.ctrPotentialScore,
                        )
                    )
                }
            }
        }

        item { Spacer(Modifier.height(24.dp)) }
    }
}

@Composable
private fun GapCard(gap: ContentGapDto) {
    Card(shape = RoundedCornerShape(12.dp)) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    gap.topic,
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.weight(1f),
                )
                Text(
                    "gap ${(gap.gapScore * 100).toInt()}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.tertiary,
                )
            }
            if (gap.commonAngles.isNotEmpty()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    "Already covered: ${gap.commonAngles.joinToString(", ")}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (gap.missingAngles.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    "Not covered:",
                    style = MaterialTheme.typography.bodySmall,
                    fontWeight = FontWeight.SemiBold,
                )
                gap.missingAngles.take(3).forEach {
                    Text("- $it", style = MaterialTheme.typography.bodySmall)
                }
            }
            if (gap.audienceCuriosity.isNotBlank()) {
                Spacer(Modifier.height(6.dp))
                Text(
                    gap.audienceCuriosity,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun ClusterRow(cluster: TopicClusterDto) {
    Card(
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        ),
        shape = RoundedCornerShape(10.dp),
    ) {
        Column(Modifier.padding(10.dp)) {
            Row {
                Text(
                    cluster.topic,
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium,
                    modifier = Modifier.weight(1f),
                )
                Text(
                    "${cluster.videoIds.size} videos",
                    style = MaterialTheme.typography.labelSmall,
                )
            }
            Spacer(Modifier.height(4.dp))
            ScoreBar("Momentum", cluster.momentum * 100, threshold = 50)
            if (cluster.titlePatterns.isNotEmpty()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    "Angles used: ${cluster.titlePatterns.joinToString(", ")}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun ResearchVideoRow(video: ResearchVideoDto) {
    Card(shape = RoundedCornerShape(10.dp)) {
        Column(Modifier.padding(10.dp)) {
            Row(verticalAlignment = Alignment.Top) {
                Text(
                    video.title,
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                Column(horizontalAlignment = Alignment.End) {
                    Text(
                        "${video.viralScore.toInt()}",
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.primary,
                    )
                    Text("viral", style = MaterialTheme.typography.labelSmall)
                }
            }
            Spacer(Modifier.height(4.dp))
            Text(
                buildString {
                    append(compactNumber(video.views)).append(" views")
                    append(" - ").append(compactNumber(video.viewVelocity.toLong()))
                    append("/day")
                    append(" - ").append(video.ageDays.toInt()).append("d old")
                    if (video.performanceRatio > 0) {
                        append(" - ").append(String.format("%.1f", video.performanceRatio))
                        append("x channel norm")
                    }
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(2.dp))
            Text(
                buildString {
                    append(video.channelTitle)
                    append(" - engagement ")
                    append(String.format("%.1f", video.engagementRate * 100)).append("%")
                    append(" - CTR potential ").append(video.ctrPotentialScore.toInt())
                    if (video.isBreakout) append("  [BREAKOUT]")
                },
                style = MaterialTheme.typography.bodySmall,
                color = if (video.isBreakout) MaterialTheme.colorScheme.tertiary
                else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
