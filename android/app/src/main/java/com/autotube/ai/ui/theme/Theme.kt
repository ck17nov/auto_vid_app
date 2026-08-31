package com.autotube.ai.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp

// A dark-first palette: the app is used at night, and a video-production tool
// reads better against dark chrome.
private val Ink = Color(0xFF0B0D12)
private val Surface1 = Color(0xFF141821)
private val Surface2 = Color(0xFF1C2230)
private val Accent = Color(0xFFE5484D)      // record red
private val AccentAlt = Color(0xFF3B82F6)   // action blue
private val Mint = Color(0xFF34D399)
private val Amber = Color(0xFFFBBF24)
private val TextHigh = Color(0xFFF2F4F8)
private val TextLow = Color(0xFF9AA4B8)

val StatusPublished = Mint
val StatusScheduled = AccentAlt
val StatusAwaiting = Amber
val StatusFailed = Accent
val StatusWorking = Color(0xFF8B5CF6)

private val DarkColors = darkColorScheme(
    primary = AccentAlt,
    onPrimary = Color.White,
    primaryContainer = Color(0xFF1E3A8A),
    onPrimaryContainer = Color(0xFFDBEAFE),
    secondary = Accent,
    onSecondary = Color.White,
    tertiary = Mint,
    onTertiary = Color(0xFF06281C),
    background = Ink,
    onBackground = TextHigh,
    surface = Surface1,
    onSurface = TextHigh,
    surfaceVariant = Surface2,
    onSurfaceVariant = TextLow,
    outline = Color(0xFF394152),
    error = Accent,
    onError = Color.White,
)

private val LightColors = lightColorScheme(
    primary = Color(0xFF1D4ED8),
    secondary = Color(0xFFC1272D),
    tertiary = Color(0xFF047857),
    background = Color(0xFFF7F8FA),
    surface = Color.White,
    surfaceVariant = Color(0xFFEAEEF5),
    onSurfaceVariant = Color(0xFF4B5563),
)

private val AppTypography = Typography(
    displaySmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Black,
        fontSize = 30.sp,
        letterSpacing = (-0.5).sp,
    ),
    headlineSmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Bold,
        fontSize = 21.sp,
    ),
    titleMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.SemiBold,
        fontSize = 16.sp,
    ),
    bodyMedium = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Normal,
        fontSize = 14.sp,
        lineHeight = 20.sp,
    ),
    bodySmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Normal,
        fontSize = 12.sp,
        lineHeight = 16.sp,
    ),
    labelSmall = TextStyle(
        fontFamily = FontFamily.SansSerif,
        fontWeight = FontWeight.Medium,
        fontSize = 11.sp,
        letterSpacing = 0.4.sp,
    ),
)

@Composable
fun AutoTubeTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = if (darkTheme) DarkColors else LightColors,
        typography = AppTypography,
        content = content,
    )
}

/** Colour for a job status chip. */
fun statusColor(status: String): Color = when (status.uppercase()) {
    "PUBLISHED" -> StatusPublished
    "SCHEDULED" -> StatusScheduled
    "READY" -> StatusPublished
    "AWAITING_APPROVAL" -> StatusAwaiting
    "FAILED", "REJECTED" -> StatusFailed
    else -> StatusWorking
}
