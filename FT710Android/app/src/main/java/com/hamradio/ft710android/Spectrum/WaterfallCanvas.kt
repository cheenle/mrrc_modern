package com.hamradio.ft710android.Spectrum

import android.graphics.Bitmap
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.IntSize
import com.hamradio.ft710android.UI.MrrcColors

/** 瀑布宽度（服务端每行 850 点）。 */
private const val WF_WIDTH = 850

/**
 * 瀑布（每行 850 点）+ FFT 迹线 + 中心频率标记，对齐手机端 Web：
 * 上半区（fftFraction）画 FFT，下半区画瀑布；配色/Floor/Ceil 来自 Palettes；点击回调按 x 比例 QSY。
 *
 * **性能**：瀑布用环形位图增量绘制（[WaterfallRingBuffer]）——每帧只把新到的那一行
 * （850 像素）写进固定位图，再用两段 drawImage 展开成"最旧在上、最新在下"。
 * 旧实现每帧重建整幅（850×120 = 102,000 次查表 + 408KB 分配 + 整幅 setPixels，全在主线程
 * draw 阶段），中端机（荣耀）会卡到把音频线程饿死 → "卡顿 + 声音几乎出不来"。
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
    val lut = remember(theme, floor, ceil) { Palettes.lut(theme, floor, ceil) }
    // 换主题/Floor/Ceil → 新 holder（旧像素全部作废，按新 LUT 重建）
    val ring = remember(lut) { WaterfallRingBuffer() }
    // 组合阶段就把新行写进环里：draw 阶段只贴图，不做副作用
    remember(rows) { ring.push(rows, lut) }

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

        // ── 瀑布：环形位图两段贴图 ────────────────────────────────
        if (ring.filled > 0) {
            val img = ring.image()
            val cap = ring.capacity
            val first = WaterfallRing.firstRingIndex(ring.head, ring.filled, cap)
            val (n1, n2) = WaterfallRing.segments(first, ring.filled, cap)
            val rowH = wfHeight.toFloat() / cap
            // 启动阶段历史不足：最新行贴底，上方留白（不做拉伸，避免比例失真）
            val topPad = ((cap - ring.filled) * rowH).toInt().coerceAtLeast(0)
            val cw = size.width.toInt().coerceAtLeast(1)
            if (n1 > 0) {
                val dh = (n1 * rowH).toInt().coerceAtLeast(1)
                drawImage(
                    img,
                    dstOffset = IntOffset(0, wfTop + topPad),
                    dstSize = IntSize(cw, dh),
                    srcOffset = IntOffset(0, first),
                    srcSize = IntSize(WF_WIDTH, n1),
                )
            }
            if (n2 > 0) {
                val dh1 = (n1 * rowH).toInt()
                val dh = (wfHeight - topPad - dh1).coerceAtLeast(1)
                drawImage(
                    img,
                    dstOffset = IntOffset(0, wfTop + topPad + dh1),
                    dstSize = IntSize(cw, dh),
                    srcOffset = IntOffset(0, 0),
                    srcSize = IntSize(WF_WIDTH, n2),
                )
            }
        }

        // ── FFT 迹线：上半区（Web 青色）─────────────────────────
        if (fft.isNotEmpty()) {
            val path = Path()
            for (x in fft.indices) {
                val px = x / WF_WIDTH.toFloat() * size.width
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

/**
 * 瀑布环形位图：固定 [WF_WIDTH]×[SpectrumProcessor.MAX_ROWS]，
 * 新行只写自己那一格（850 像素 + 一次单行 setPixels），**永不整幅重建**。
 *
 * "哪些行是新的"按对象身份判定（[WaterfallRing.newRowCount]）：处理器是
 * `addLast(newRow)` + `removeFirst()`，所以旧行对象会整体左移一格、新行在尾部。
 */
private class WaterfallRingBuffer(
    val width: Int = WF_WIDTH,
    val capacity: Int = SpectrumProcessor.MAX_ROWS,
) {
    private val backing = IntArray(width * capacity)
    private val bmp: Bitmap = Bitmap.createBitmap(width, capacity, Bitmap.Config.ARGB_8888)
    private var cached: ImageBitmap? = null
    private var lastRows: List<IntArray>? = null

    /** 下一个要写入的环槽位。 */
    var head = 0
        private set

    /** 已写入的有效行数（≤ capacity）。 */
    var filled = 0
        private set

    fun image(): ImageBitmap = cached ?: bmp.asImageBitmap().also { cached = it }

    /** 把相对上次新增的行写进环；返回写了几行（0 = 本帧无需触碰位图）。 */
    fun push(rows: List<IntArray>, lut: IntArray): Int {
        val n = WaterfallRing.newRowCount(lastRows, rows)
        lastRows = rows
        if (n <= 0) return 0
        val start = rows.size - n
        for (k in 0 until n) {
            val row = rows[start + k]
            val base = head * width
            val count = minOf(width, row.size)
            for (x in 0 until count) backing[base + x] = lut[row[x].coerceIn(0, 255)]
            for (x in count until width) backing[base + x] = lut[0]
            bmp.setPixels(backing, base, width, 0, head, width, 1)
            head = (head + 1) % capacity
            if (filled < capacity) filled++
        }
        return n
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
