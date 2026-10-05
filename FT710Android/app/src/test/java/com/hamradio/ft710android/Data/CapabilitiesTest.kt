package com.hamradio.ft710android.Data

import com.hamradio.ft710android.Network.CapabilitiesDto
import com.hamradio.ft710android.Network.FilterTables
import com.hamradio.ft710android.Network.ScopeSpanDto
import org.junit.Assert.assertEquals
import org.junit.Test

class CapabilitiesTest {
    @Test fun `fallback is the ft710 table`() {
        val caps = RadioCaps.from(null)
        assertEquals(10000L, caps.spanHz(3))
        assertEquals(100000L, caps.spanHz(6))
        assertEquals(1000000L, caps.spanHz(9))
        assertEquals("100 kHz", caps.spanName(6))
        assertEquals(4, caps.attStepCount)
        assertEquals(3, caps.preampStepCount)
        assertEquals(10f, caps.audioBoost)
    }

    @Test fun `civ27 spans are half-width and doubled`() {
        val dto = CapabilitiesDto(
            scopeType = "civ27",
            scopeSpans = mapOf("0" to ScopeSpanDto("±100 kHz", 100_000)),
            attSteps = listOf(0, 12),
            preampSteps = listOf("OFF"),
            audioGainBoost = 1.0,
        )
        val caps = RadioCaps.from(dto)
        assertEquals(200000L, caps.spanHz(0))
        assertEquals("±100 kHz", caps.spanName(0))
        assertEquals(2, caps.attStepCount)
        assertEquals(1, caps.preampStepCount)
        assertEquals(1f, caps.audioBoost)
    }

    @Test fun `voice filter rotation matches the web curated list`() {
        assertEquals(13, Capabilities.nextFilter(9, "USB", null))
        assertEquals(17, Capabilities.nextFilter(13, "USB", null))
        assertEquals(23, Capabilities.nextFilter(20, "USB", null))
        assertEquals(9, Capabilities.nextFilter(23, "USB", null))
        assertEquals(9, Capabilities.nextFilter(5, "USB", null))
    }

    @Test fun `narrow modes use the narrow list from the server table`() {
        val tables = FilterTables(narrowModes = listOf("CW-U"))
        assertEquals(6, Capabilities.nextFilter(3, "CW-U", tables))
        assertEquals(10, Capabilities.nextFilter(6, "CW-U", tables))
        assertEquals(21, Capabilities.nextFilter(17, "CW-U", tables))
        assertEquals(3, Capabilities.nextFilter(21, "CW-U", tables)) // 末项回卷首项
    }

    @Test fun `fil123 rotates fil1 to fil3`() {
        assertEquals(2, Capabilities.nextFilter(1, "USB", null, "fil123"))
        assertEquals(3, Capabilities.nextFilter(2, "USB", null, "fil123"))
        assertEquals(1, Capabilities.nextFilter(3, "USB", null, "fil123"))
    }

    @Test fun `filter labels use the width tables`() {
        val tables = FilterTables(voice = listOf(listOf(13, 2400), listOf(23, 4000)))
        assertEquals("2.4k", Capabilities.filterLabel(13, "USB", tables, "width_table"))
        assertEquals("无", Capabilities.filterLabel(23, "USB", tables, "width_table"))
        assertEquals("--", Capabilities.filterLabel(7, "USB", tables, "width_table"))
    }

    @Test fun `fil123 labels use per-mode defaults`() {
        val tables = FilterTables(
            model = "fil123",
            filDefaults = mapOf("USB" to listOf(300, 2400, 3000)),
        )
        assertEquals("FIL2 2.4k", Capabilities.filterLabel(2, "USB", tables, "fil123"))
        assertEquals("FIL1 300Hz", Capabilities.filterLabel(1, "USB", tables, "fil123"))
    }
}
