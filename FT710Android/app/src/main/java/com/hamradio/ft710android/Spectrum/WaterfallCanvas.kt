package com.hamradio.ft710android.Spectrum

import androidx.compose.foundation.Canvas
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/** 瀑布（每行 850 点，颜色映射对齐 Web Jet colormap）+ 顶部 FFT 折线。点击调频由外层 pointerInput 处理。 */
@Composable
fun WaterfallCanvas(
    rows: List<IntArray>,
    fft: IntArray,
    modifier: Modifier = Modifier,
) {
    Canvas(modifier) {
        if (rows.isEmpty()) return@Canvas
        val cellH = size.height / rows.size
        val cellW = size.width / 850f
        for (r in rows.indices) {
            val row = rows[r]
            for (x in row.indices) {
                drawRect(jetColor(row[x] / 255f),
                    topLeft = Offset(x * cellW, r * cellH),
                    size = Size(cellW, cellH))
            }
        }
        // FFT 折线
        val path = Path()
        for (x in fft.indices) {
            val px = x / 850f * size.width
            val py = size.height - (fft[x] / 255f) * size.height
            if (x == 0) path.moveTo(px, py) else path.lineTo(px, py)
        }
        drawPath(path, Color(0xFF06B6D4), style = Stroke(width = 2f))
    }
}

/** 对齐 Web Jet colormap：深蓝→青→黄→红。 */
private fun jetColor(t: Float): Color {
    val tt = t.coerceIn(0f, 1f)
    val r = (255f * max(0f, min(1f, 1.5f - abs(4f * tt - 3f)))).toInt()
    val g = (255f * max(0f, min(1f, 1.5f - abs(4f * tt - 2f)))).toInt()
    val b = (255f * max(0f, min(1f, 1.5f - abs(4f * tt - 1f)))).toInt()
    return Color(r, g, b)
}
