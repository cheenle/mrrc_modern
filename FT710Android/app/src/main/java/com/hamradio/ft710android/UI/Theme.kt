package com.hamradio.ft710android.UI

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp

/**
 * 设计令牌 —— 逐字对应手机端 Web（`static/ft710.css` 的 `:root`）。
 * 改这里就等于同步 Web 配色；不要各页面就地写死颜色。
 */
object MrrcColors {
    val BgPrimary = Color(0xFF1A1A1A)
    val BgSecondary = Color(0xFF242424)
    val BgTertiary = Color(0xFF2A2A2A)
    val BgCard = Color(0xFF333333)
    val TextPrimary = Color(0xFFEEEEEE)
    val TextSecondary = Color(0xFF999999)
    val TextMuted = Color(0xFF666666)
    val Accent = Color(0xFFF59E0B)          // --accent
    val AccentDim = Color(0x33F59E0B)       // rgba(245,158,11,.2)
    val AccentGlow = Color(0x66F59E0B)      // rgba(245,158,11,.4)
    val Danger = Color(0xFFEF4444)
    val DangerGlow = Color(0x66EF4444)
    val Success = Color(0xFF22C55E)
    val Warning = Color(0xFFEAB308)         // TUNE 底色（黑字）
    val Border = Color(0xFF444444)
    val Cyan = Color(0xFF06B6D4)            // Id 表（web .meter-bar-fill.id）
    val Purple = Color(0xFF6B5BD2)          // Vd 表（web 紫色条）
    val WaterfallLine = Color(0xFF06B6D4)   // FFT 迹线（web cyan）
}

val MonoFont = FontFamily.Monospace

private val MrrcScheme = darkColorScheme(
    primary = MrrcColors.Accent,
    onPrimary = MrrcColors.BgPrimary,
    secondary = MrrcColors.Accent,
    onSecondary = MrrcColors.BgPrimary,
    background = MrrcColors.BgPrimary,
    onBackground = MrrcColors.TextPrimary,
    surface = MrrcColors.BgSecondary,
    onSurface = MrrcColors.TextPrimary,
    surfaceVariant = MrrcColors.BgTertiary,
    onSurfaceVariant = MrrcColors.TextSecondary,
    outline = MrrcColors.Border,
    error = MrrcColors.Danger,
    onError = Color.White,
)

private val MrrcShapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(6.dp),
    medium = RoundedCornerShape(10.dp),
    large = RoundedCornerShape(14.dp),
)

/** 全局主题（对齐手机端 Web：深灰底 + 琥珀强调）。 */
@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = MrrcScheme, shapes = MrrcShapes, content = content)
}
