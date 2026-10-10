package com.hamradio.ft710android.Data

import com.hamradio.ft710android.Network.CapabilitiesDto
import com.hamradio.ft710android.Network.FilterTables

/** 量程选项：idx 是发给服务端的 scope_span 值，hz 是**全宽**（CI-V 半幅已 ×2）。 */
data class SpanChoice(val idx: Int, val name: String, val hz: Long)

/**
 * 能力表的 JVM 侧视图：服务端 capabilities 缺失/字段缺失时逐项回退到 FT-710 值，
 * 行为与 web `applyRadioCapabilities` 的无能力分支一致（老服务端不受影响）。
 */
data class RadioCaps(
    val modelName: String = "ft710",
    val displayName: String = "Yaesu FT-710",
    val verified: Boolean = true,
    val txGated: Boolean = false,
    val hasAtu: Boolean = true,
    val hasAutoNotch: Boolean = true,
    val hasVdIdMeters: Boolean = true,
    val filterModel: String = "width_table",
    val attStepCount: Int = 4,
    val preampStepCount: Int = 3,
    val scopeType: String = "ft4222",
    val spans: List<SpanChoice> = FT710_SPANS,
    val speeds: List<String> = listOf("1", "2", "3", "4", "5"),
    val audioBoost: Float = 10f,
) {
    fun spanHz(idx: Int): Long = spans.firstOrNull { it.idx == idx }?.hz
        ?: spans.minByOrNull { Math.abs(it.idx - idx) }?.hz ?: 100_000L

    fun spanName(idx: Int): String = spans.firstOrNull { it.idx == idx }?.name ?: "100 kHz"

    companion object {
        val FT710_SPANS = listOf(
            SpanChoice(0, "1 kHz", 1_000),
            SpanChoice(1, "2 kHz", 2_000),
            SpanChoice(2, "5 kHz", 5_000),
            SpanChoice(3, "10 kHz", 10_000),
            SpanChoice(4, "20 kHz", 20_000),
            SpanChoice(5, "50 kHz", 50_000),
            SpanChoice(6, "100 kHz", 100_000),
            SpanChoice(7, "200 kHz", 200_000),
            SpanChoice(8, "500 kHz", 500_000),
            SpanChoice(9, "1 MHz", 1_000_000),
        )

        fun from(dto: CapabilitiesDto?): RadioCaps {
            if (dto == null) return RadioCaps()
            val half = dto.scopeType == "civ27"
            val spans = dto.scopeSpans.entries
                .mapNotNull { (k, v) ->
                    val idx = k.toIntOrNull() ?: return@mapNotNull null
                    if (v.freq <= 0) return@mapNotNull null
                    SpanChoice(idx, v.name.ifEmpty { "${v.freq / 1000} kHz" }, if (half) v.freq * 2 else v.freq)
                }
                .sortedBy { it.idx }
                .ifEmpty { FT710_SPANS }
            return RadioCaps(
                modelName = dto.modelName,
                displayName = dto.displayName,
                verified = dto.verified,
                txGated = dto.txGated,
                hasAtu = dto.hasAtu,
                hasAutoNotch = dto.hasAutoNotch,
                hasVdIdMeters = dto.hasVdIdMeters,
                filterModel = dto.filterModel,
                attStepCount = if (dto.attSteps.isEmpty()) 4 else dto.attSteps.size,
                preampStepCount = if (dto.preampSteps.isEmpty()) 3 else dto.preampSteps.size,
                scopeType = dto.scopeType,
                spans = spans,
                speeds = dto.scopeSpeeds.ifEmpty { listOf("1", "2", "3", "4", "5") },
                audioBoost = dto.audioGainBoost.toFloat(),
            )
        }
    }
}

/** filter/ATT/PRE 循环与标签（web `getNextFilter/getFilterLabel` 的 JVM 版）。 */
object Capabilities {
    private val LEGACY_NARROW = setOf("CW-U", "CW-L", "RTTY-L", "RTTY-U", "DATA-L", "DATA-U", "PSK")

    fun isNarrowMode(mode: String, tables: FilterTables?): Boolean {
        val list = tables?.narrowModes
        return if (!list.isNullOrEmpty()) list.contains(mode) else LEGACY_NARROW.contains(mode)
    }

    /** Web `getNextFilter`：voice[9,13,17,20,23] / narrow[3,6,10,13,17,21]，列表外/末项回卷首项。 */
    fun nextFilter(current: Int, mode: String, tables: FilterTables?, model: String = "width_table"): Int {
        if (model == "fil123") return if (current in 1..2) current + 1 else 1
        val list = if (isNarrowMode(mode, tables)) intArrayOf(3, 6, 10, 13, 17, 21)
                   else intArrayOf(9, 13, 17, 20, 23)
        val pos = list.indexOf(current)
        return if (pos < 0 || pos >= list.size - 1) list[0] else list[pos + 1]
    }

    /** Web `getFilterLabel`：width_table 查 [idx,hz] 数对（4000 = “无”）；fil123 用 filDefaults。 */
    fun filterLabel(idx: Int, mode: String, tables: FilterTables?, model: String = "width_table"): String {
        if (model == "fil123") {
            val hz = tables?.filDefaults?.get(mode)?.getOrNull(idx - 1) ?: return "FIL$idx"
            val w = if (hz >= 1000) "%.1fk".format(java.util.Locale.US, hz / 1000f) else "${hz}Hz"
            return "FIL$idx $w"
        }
        val pair = (if (isNarrowMode(mode, tables)) tables?.narrow else tables?.voice)
            ?.firstOrNull { it.size >= 2 && it[0] == idx }
        val hz = pair?.get(1) ?: return "--"
        if (hz == 4000) return "无"
        return if (hz >= 1000) "%.1fk".format(java.util.Locale.US, hz / 1000f) else "${hz}Hz"
    }
}
