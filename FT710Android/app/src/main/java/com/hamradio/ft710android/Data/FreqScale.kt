package com.hamradio.ft710android.Data

import kotlin.math.floor

/**
 * 频谱标尺的纯数字部分，逐条对齐 web `static/ft710_ui.js`：
 *  - 范围恒为 **VFO ± span/2**（服务端恒 CENTER 模式；`scope_start_freq` 调谐后滞后，不可用于标尺）
 *  - 步进用 `_freqStep` 自适应（屏幕上约 8~12 个刻度）
 *  - 标签格式随步进变化（`_formatFreqLabel`）
 */
object FreqScale {
    /** VFO 居中时的左边缘（Hz，可为小数）。 */
    fun leftEdge(vfoFreq: Long, spanHz: Long): Double = vfoFreq - spanHz / 2.0

    /** Web `_freqStep`：按 span 选刻度间隔。 */
    fun step(spanHz: Long): Long = when {
        spanHz <= 2_000 -> 200
        spanHz <= 5_000 -> 500
        spanHz <= 10_000 -> 1_000
        spanHz <= 25_000 -> 2_500
        spanHz <= 50_000 -> 5_000
        spanHz <= 100_000 -> 10_000
        spanHz <= 200_000 -> 25_000
        spanHz <= 500_000 -> 50_000
        else -> 100_000
    }

    /** Web `_formatFreqLabel`。 */
    fun label(hz: Long, step: Long): String = when {
        step < 1_000 -> "%.1fk".format(java.util.Locale.US, hz / 1000.0)
        step <= 5_000 -> "%.0fk".format(java.util.Locale.US, hz / 1000.0)
        step <= 25_000 -> "%.2f".format(java.util.Locale.US, hz / 1e6)
        else -> "%.3f".format(java.util.Locale.US, hz / 1e6)
    }

    /** 第一个刻度 = floor(左边缘/step)*step（与 Web 一致，可能落在可视区左侧一点点外）。 */
    fun firstMark(leftHz: Double, step: Long): Long =
        (floor(leftHz / step) * step).toLong()

    /**
     * 可见刻度：频率 + 0..1 的水平位置。只返回落在 [0,1] 内的刻度。
     */
    fun ticks(vfoFreq: Long, spanHz: Long): List<Pair<Long, Float>> {
        if (spanHz <= 0) return emptyList()
        val left = leftEdge(vfoFreq, spanHz)
        val st = step(spanHz)
        val out = ArrayList<Pair<Long, Float>>()
        var f = firstMark(left, st)
        val right = left + spanHz
        var guard = 0
        while (f <= right && guard < 1000) {
            val frac = ((f - left) / spanHz).toFloat()
            if (frac in 0f..1f) out += f to frac
            f += st
            guard++
        }
        return out
    }
}
