package com.hamradio.ft710android.Data

/**
 * S 表标尺的纯数字部分，逐条对齐 web `static/ft710_ui.js:renderSMeter`：
 * 原始值 0..255，刻度位置是 Web 的 marks 数组（与 S1/S3/…/+60 标签对应）。
 */
object SMeter {
    const val RAW_MAX = 255

    /** Web renderSMeter 的 `marks` 数组（raw 值）。 */
    val MARKERS = intArrayOf(0, 12, 27, 40, 55, 65, 80, 95, 112, 130, 150, 172, 190, 220, 240, 255)

    /** Web 页面上的 8 个刻度标签。 */
    val LABELS = listOf("S1", "S3", "S5", "S7", "S9", "+20", "+40", "+60")

    /** 填充比例 = raw/255（不再是 app 早期的 raw/32）。 */
    fun fraction(raw: Int): Float = raw.coerceIn(0, RAW_MAX) / RAW_MAX.toFloat()
}
