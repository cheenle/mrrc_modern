package com.hamradio.ft710android.Data

import kotlin.math.roundToLong

/** 频率输入与点击调频的数学，逐条对齐 web `static/ft710_ui.js:commitFreq/wireScopeQSY`。 */
object FreqInput {
    const val MIN_HZ = 30_000L
    const val MAX_HZ = 75_000_000L

    /** 解析 MHz 输入框；无法解析返回 null。 */
    fun parse(raw: String): Long? {
        val s = raw.trim()
        if (s.isEmpty()) return null
        val v = s.toDoubleOrNull() ?: return null
        var hz = v
        if (hz < 100_000 && !s.contains('.')) hz *= 1_000      // kHz
        if (hz < 1_000) hz *= 1_000_000                        // MHz
        return hz.roundToLong().coerceIn(MIN_HZ, MAX_HZ)
    }

    /** 中心模式（服务器恒 EX040200）下，瀑布 x 比例 → 目标频率。 */
    fun qsy(vfoFreq: Long, spanHz: Long, fraction: Float): Long {
        val f = fraction.coerceIn(0f, 1f)
        val hz = vfoFreq - spanHz / 2.0 + f * spanHz
        return hz.roundToLong().coerceIn(MIN_HZ, MAX_HZ)
    }
}
