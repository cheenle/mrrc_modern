package com.hamradio.ft710android.Data

import com.hamradio.ft710android.Network.BandDto

/**
 * 波段循环表 —— 与 `static/ft710_ui.js:DEFAULT_BAND_CYCLE` 逐字一致。
 * 纯逻辑，JVM 可测。
 */
data class Band(val name: String, val start: Long, val end: Long, val defaultFreq: Long)

object BandCycle {
    val bands: List<Band> = listOf(
        Band("160m", 1_800_000, 2_000_000, 1_845_500),
        Band("80m", 3_500_000, 4_000_000, 3_850_000),
        Band("60m", 5_250_000, 5_450_000, 5_350_000),
        Band("40m", 7_000_000, 7_300_000, 7_050_000),
        Band("30m", 10_100_000, 10_150_000, 10_140_000),
        Band("20m", 14_000_000, 14_350_000, 14_270_000),
        Band("17m", 18_068_000, 18_168_000, 18_132_500),
        Band("15m", 21_000_000, 21_450_000, 21_400_000),
        Band("12m", 24_890_000, 24_990_000, 24_952_500),
        Band("10m", 28_000_000, 29_700_000, 28_450_000),
        Band("6m", 50_000_000, 54_000_000, 50_150_000),
        Band("4m", 70_000_000, 70_500_000, 70_250_000),
    )

    /** Web 语义：未知波段从 160m 起算；末位回卷到 160m。 */
    fun next(current: String): Band {
        val idx = bands.indexOfFirst { it.name == current }
        val from = if (idx < 0) 0 else idx
        return bands[(from + 1) % bands.size]
    }

    /**
     * 服务端不发 `default_freq` 时用本表补齐。
     *
     * Yaesu 家族的 `bands` 只有 `[name, low, high]`（`backends/yaesu/backend.py`），
     * 而波段格发的是 **`freq`** 而不是 `band` 名 —— 补 0 就直接把电台调到 0 Hz。
     * 表里没有的（2m / 70cm / 空气段）取波段中点、向下对齐 1 kHz。
     */
    fun withDefaults(server: List<BandDto>): List<BandDto> = server.map { band ->
        if (band.defaultFreq > 0) {
            band
        } else {
            val known = bands.firstOrNull { it.name == band.name }
            when {
                known != null -> band.copy(defaultFreq = known.defaultFreq)
                else -> band.copy(
                    defaultFreq = ((band.start + band.end) / 2 / 1_000) * 1_000,
                )
            }
        }
    }
}
