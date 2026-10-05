package com.hamradio.ft710android.Spectrum

import android.graphics.Bitmap
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import com.hamradio.ft710android.UI.MrrcColors

/**
 * 瀑布（每行 850 点）+ FFT 迹线 + 中心频率标记，对齐手机端 Web：
 * 上半区（fftFraction）画 FFT，下半区画瀑布；配色/Floor/Ceil 来自 Palettes；点击回调按 x 比例 QSY。
 */
@Composable
fun WaterfallCanvas(
    rows: List<IntArray>,
    fft: IntArray,
    theme: String,
    floor: Int,
    ceil: Int,
    fftFraction: Float,
    modifier: Modifier = Modifier,
    onQsyFraction: ((Float) -> Unit)? = null,
) {
    val holder = remember { BitmapHolder() }
    val lut = remember(theme, floor, ceil) { Palettes.lut(theme, floor, ceil) }
    Canvas(
        modifier.pointerInput(onQsyFraction) {
            detectTapGestures { offset ->
                if (size.width > 0) {
                    onQsyFraction?.invoke((offset.x / size.width).coerceIn(0f, 1f))
                }
            }
        }
    ) {
        val fftPx = size.height * fftFraction.coerceIn(0.1f, 0.9f)
        val wfTop = fftPx.toInt()
        val wfHeight = (size.height - fftPx).toInt().coerceAtLeast(1)

        // ── 瀑布：只画在下半区 ──────────────────────────────────
        if (rows.isNotEmpty()) {
            val w = 850
            val h = rows.size
            val bmp = holder.bitmap(w, h)
            val pixels = IntArray(w * h)
            for (y in 0 until h) {
                val row = rows[y]
                val base = y * w
                for (x in 0 until w) pixels[base + x] = lut[row[x].coerceIn(0, 255)]
            }
            bmp.setPixels(pixels, 0, w, 0, 0, w, h)
            drawImage(
                bmp.asImageBitmap(),
                dstOffset = IntOffset(0, wfTop),
                dstSize = IntSize(size.width.toInt().coerceAtLeast(1), wfHeight),
            )
        }

        // ── FFT 迹线：上半区（Web 青色）─────────────────────────
        if (fft.isNotEmpty()) {
            val path = Path()
            for (x in fft.indices) {
                val px = x / 850f * size.width
                val py = fftPx - (fft[x] / 255f) * fftPx
                if (x == 0) path.moveTo(px, py) else path.lineTo(px, py)
            }
            drawPath(path, MrrcColors.WaterfallLine, style = Stroke(width = 2f))
        }

        // ── 中心频率标记（红色竖线 + 底部三角）跨全高 ────────────
        val cx = size.width / 2f
        drawLine(MrrcColors.Danger, Offset(cx, 0f), Offset(cx, size.height), strokeWidth = 1.5f)
        val tri = Path().apply {
            moveTo(cx - 6f, size.height); lineTo(cx + 6f, size.height); lineTo(cx, size.height - 9f); close()
        }
        drawPath(tri, MrrcColors.Danger)
    }
}

private class BitmapHolder {
    private var bmp: Bitmap? = null

    fun bitmap(w: Int, h: Int): Bitmap {
        val cur = bmp
        if (cur != null && cur.width == w && cur.height == h) return cur
        return Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888).also { bmp = it }
    }
}

/** Web jet 的 256 级查表：避免每帧重算颜色（其他调色板走 Palettes.lut）。 */
private val LUT = IntArray(256) { jetArgb(it / 255f) }

/** 逐段对齐 `static/ft710_ui.js:WF_PALETTES.jet`（纯函数，JVM 可测）。 */
internal fun jetArgb(v0: Float): Int {
    val v = v0.coerceIn(0f, 1f)
    val r: Int
    val g: Int
    val b: Int
    when {
        v < 0.125f -> { val u = v / 0.125f; r = 0; g = 0; b = (128 + u * 127).toInt() }
        v < 0.375f -> { val u = (v - 0.125f) / 0.25f; r = 0; g = (u * 255).toInt(); b = 255 }
        v < 0.625f -> { val u = (v - 0.375f) / 0.25f; r = (u * 255).toInt(); g = 255; b = (255 * (1 - u)).toInt() }
        v < 0.875f -> { val u = (v - 0.625f) / 0.25f; r = 255; g = (255 * (1 - u)).toInt(); b = 0 }
        else -> { val u = (v - 0.875f) / 0.125f; r = (255 * (1 - u * 0.5f)).toInt(); g = 0; b = 0 }
    }
    return (0xFF shl 24) or (r shl 16) or (g shl 8) or b
}
