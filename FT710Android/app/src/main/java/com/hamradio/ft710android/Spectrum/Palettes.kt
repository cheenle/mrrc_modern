package com.hamradio.ft710android.Spectrum

/**
 * 六套瀑布配色 + floor/ceil 映射，逐段对齐 web `static/ft710_ui.js:WF_PALETTES`。
 * 分段边界与 `toInt()` 截断都与 JS `Math.floor`（正值）一致。
 */
object Palettes {
    val NAMES = listOf("jet", "hot", "cold", "thermal", "night", "gray")

    /** ARGB；未知主题回退 jet（web `WF_PALETTES[scopeTheme] || WF_PALETTES.jet`）。 */
    fun argb(theme: String, v0: Float): Int {
        val v = v0.coerceIn(0f, 1f)
        if (theme == "jet" || theme !in NAMES) return jetArgb(v)
        val (r, g, b) = rgb(theme, v.toDouble())
        return 0xFF000000.toInt() or (r shl 16) or (g shl 8) or b
    }

    /** 256 级查表：raw 0..255 → clamp((raw-floor)/(ceil-floor)) → palette。 */
    fun lut(theme: String, floor: Int, ceil: Int): IntArray {
        val range = (ceil - floor).coerceAtLeast(1)
        return IntArray(256) { raw ->
            val v = ((raw - floor).toFloat() / range).coerceIn(0f, 1f)
            argb(theme, v)
        }
    }

    // JS 是双精度：Float 会在 (1-0.66)/0.34 这类段末算出 254.99997 而截断成 254，
    // 与 Web 的 255 不一致，所以逐段用 Double 运算。
    private fun rgb(theme: String, v: Double): Triple<Int, Int, Int> = when (theme) {
        "hot" -> when {
            v < 0.33 -> { val u = v / 0.33; Triple((u * 255).toInt(), 0, 0) }
            v < 0.66 -> { val u = (v - 0.33) / 0.33; Triple(255, (u * 255).toInt(), 0) }
            else -> { val u = (v - 0.66) / 0.34; Triple(255, 255, (u * 255).toInt()) }
        }
        "cold" -> when {
            v < 0.5 -> { val u = v / 0.5; Triple(0, (u * 200).toInt(), (40 + u * 215).toInt()) }
            else -> { val u = (v - 0.5) / 0.5; Triple((u * 255).toInt(), (200 + u * 55).toInt(), 255) }
        }
        "thermal" -> when {
            v < 0.25 -> { val u = v / 0.25; Triple((60 + u * 140).toInt(), 0, 0) }
            v < 0.5 -> { val u = (v - 0.25) / 0.25; Triple((200 + u * 55).toInt(), (u * 180).toInt(), 0) }
            v < 0.75 -> { val u = (v - 0.5) / 0.25; Triple(255, (180 + u * 75).toInt(), (u * 200).toInt()) }
            else -> { val u = (v - 0.75) / 0.25; Triple(255, 255, (200 + u * 55).toInt()) }
        }
        "night" -> when {
            v < 0.33 -> { val u = v / 0.33; Triple(0, 0, (u * 128).toInt()) }
            v < 0.66 -> { val u = (v - 0.33) / 0.33; Triple((u * 180).toInt(), 0, (128 + u * 127).toInt()) }
            else -> { val u = (v - 0.66) / 0.34; Triple((180 + u * 75).toInt(), (u * 200).toInt(), 255) }
        }
        "gray" -> { val q = (v * 255).toInt().coerceIn(0, 255); Triple(q, q, q) }
        else -> Triple(0, 0, 0)
    }
}
