package com.hamradio.ft710android.Data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RadioStateTest {
    private val json = Json { ignoreUnknownKeys = true }

    private fun obj(vararg pairs: Pair<String, String>): Map<String, JsonElement> =
        json.parseToJsonElement(
            pairs.joinToString(prefix = "{", postfix = "}") { (k, v) -> "\"$k\":$v" }
        ).jsonObject

    @Test fun `applyFullState populates all field types`() {
        val state = RadioState()
        state.apply(obj(
            "vfo_a_freq" to "7050000",
            "vfo_b_freq" to "14270000",
            "active_vfo" to "\"A\"",
            "mode" to "1",
            "tx_status" to "0",
            "s_meter" to "4",
            "power_watts" to "12.5",
            "swr_ratio" to "1.4",
            "noise_reduction" to "true",
            "filter_width" to "5",
            "serial_connected" to "true",
            "mode_name" to "\"USB\"",
            "band_name" to "\"20m\""
        ))
        assertEquals(7050000L, state.vfoAFreq)
        assertEquals(14270000L, state.vfoBFreq)
        assertEquals("A", state.activeVfo)
        assertEquals(1, state.mode)
        assertEquals(0, state.txStatus)
        assertEquals(4, state.sMeter)
        assertEquals(12.5, state.powerWatts, 1e-9)
        assertEquals(1.4, state.swrRatio, 1e-9)
        assertTrue(state.noiseReduction)
        assertEquals(5, state.filterWidth)
        assertTrue(state.serialConnected)
        assertEquals("USB", state.modeName)
        assertEquals("20m", state.bandName)
    }

    @Test fun `applyUpdate partial overwrite only touches given keys and returns them`() {
        val state = RadioState()
        state.apply(obj("freq" to "7000000", "mode" to "2"))
        val dirty = state.apply(obj("vfo_a_freq" to "7100000"))
        assertEquals(setOf("vfo_a_freq"), dirty)
        assertEquals(7100000L, state.vfoAFreq)
        assertEquals(2, state.mode) // 未触碰
    }

    @Test fun `unknown keys are ignored and reported as not dirty`() {
        val state = RadioState()
        val dirty = state.apply(obj("bogus_field" to "1"))
        assertFalse(dirty.contains("bogus_field"))
    }

    @Test fun `booleans accept true as json bool`() {
        val state = RadioState()
        state.apply(obj("vox" to "true", "break_in" to "false"))
        assertTrue(state.vox)
        assertFalse(state.breakIn)
    }
}
