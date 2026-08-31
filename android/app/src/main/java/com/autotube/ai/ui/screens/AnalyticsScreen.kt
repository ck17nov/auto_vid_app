package com.autotube.ai.ui.screens

import androidx.compose.foundation.Canvas
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
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.autotube.ai.data.local.AnalyticsEntity
import com.autotube.ai.ui.components.BannerTone
import com.autotube.ai.ui.components.EmptyState
import com.autotube.ai.ui.components.InfoBanner
import com.autotube.ai.ui.components.LoadingRow
import com.autotube.ai.ui.components.MetricTile
import com.autotube.ai.ui.components.SectionTitle
import com.autotube.ai.ui.components.compactNumber
import com.autotube.ai.ui.vm.AnalyticsViewModel
import com.autotube.ai.ui.vm.appViewModel
import kotlin.math.max

/**
 * Screen 7: analytics for the user's OWN channel.
 *
 * Charts are drawn with Compose Canvas rather than a charting library: the two
 * shapes needed here are a bar chart and a comparison row, and avoiding the
 * dependency keeps the APK small and the build simple.
 */
@Composable
fun AnalyticsScreen() {
    val vm: AnalyticsViewModel = appViewModel()
    val rows by vm.rows.collectAsStateWithLifecycle()
    val totalViews by vm.totalViews.collectAsStateWithLifecycle()
    val avgRetention by vm.avgRetention.collectAsStateWithLifecycle()
    val totalSubs by vm.totalSubs.collectAsStateWithLifecycle()
    val busy by vm.busy.collectAsStateWithLifecycle()
    val message by vm.message.collectAsStateWithLifecycle()

    LazyColumn(
        Modifier.fillMaxWidth().padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        item {
            Text(
                "Analytics",
                style = MaterialTheme.typography.displaySmall,
                modifier = Modifier.padding(top = 16.dp),
            )
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

        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = { vm.refresh(collect = true) }, enabled = !busy) {
                    Text("Collect from YouTube")
                }
                OutlinedButton(onClick = { vm.refresh(false) }, enabled = !busy) {
                    Text("Refresh")
                }
            }
        }

        if (busy) item { LoadingRow("Talking to the YouTube Analytics API...") }

        item {
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                MetricTile(
                    "Views", totalViews?.let { compactNumber(it) } ?: "--",
                    Modifier.weight(1f),
                )
                MetricTile(
                    "Avg retention",
                    avgRetention?.let { String.format("%.0f%%", it) } ?: "--",
                    Modifier.weight(1f),
                    accent = MaterialTheme.colorScheme.tertiary,
                )
                MetricTile(
                    "Subs gained", totalSubs?.let { compactNumber(it) } ?: "--",
                    Modifier.weight(1f),
                )
            }
        }

        item {
            InfoBanner(
                text = "CTR and impressions are available only for your own " +
                    "authenticated channel. No competitor's CTR is ever shown, " +
                    "because YouTube does not expose it.",
                tone = BannerTone.Info,
            )
        }

        if (rows.isEmpty()) {
            item {
                EmptyState(
                    title = "No analytics yet",
                    body = "Publish a video, wait a day, then collect. YouTube " +
                        "needs time before per-video data appears.",
                )
            }
        } else {
            item { SectionTitle("Views by video") }
            item { BarChart(rows.take(10).map { it.views.toFloat() }) }

            item { SectionTitle("Retention by video") }
            item {
                BarChart(
                    rows.take(10).map { it.avgViewPercentage.toFloat() },
                    maxOverride = 100f,
                    color = MaterialTheme.colorScheme.tertiary,
                )
            }

            item { SectionTitle("Per video") }
            items(rows, key = { it.videoId }) { row -> AnalyticsRow(row) }
        }

        item { Spacer(Modifier.height(24.dp)) }
    }
}

@Composable
private fun BarChart(
    values: List<Float>,
    modifier: Modifier = Modifier,
    maxOverride: Float? = null,
    color: Color = MaterialTheme.colorScheme.primary,
) {
    if (values.isEmpty()) return
    val peak = maxOverride ?: max(values.maxOrNull() ?: 1f, 1f)
    val track = MaterialTheme.colorScheme.surfaceVariant
    Canvas(
        modifier
            .fillMaxWidth()
            .height(120.dp)
            .padding(vertical = 6.dp)
    ) {
        val count = values.size
        val gap = size.width * 0.02f
        val barWidth = (size.width - gap * (count - 1)) / count
        values.forEachIndexed { index, value ->
            val fraction = (value / peak).coerceIn(0f, 1f)
            val barHeight = size.height * fraction
            val x = index * (barWidth + gap)
            // Track behind each bar gives a sense of the maximum.
            drawRect(
                color = track,
                topLeft = Offset(x, 0f),
                size = Size(barWidth, size.height),
            )
            drawRect(
                color = color,
                topLeft = Offset(x, size.height - barHeight),
                size = Size(barWidth, barHeight),
            )
        }
    }
}

@Composable
private fun AnalyticsRow(row: AnalyticsEntity) {
    Card(
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surfaceVariant
        ),
        shape = RoundedCornerShape(10.dp),
    ) {
        Column(Modifier.padding(10.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    row.videoId,
                    style = MaterialTheme.typography.bodyMedium,
                    fontWeight = FontWeight.Medium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    compactNumber(row.views) + " views",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Spacer(Modifier.height(4.dp))
            Text(
                buildString {
                    append("retention ")
                    append(String.format("%.1f%%", row.avgViewPercentage))
                    if (row.ctr > 0) {
                        append(" - CTR ").append(String.format("%.2f%%", row.ctr))
                    } else {
                        append(" - CTR n/a")
                    }
                    append(" - +").append(row.subscribersGained).append(" subs")
                    append(" - ").append(row.likes).append(" likes")
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
