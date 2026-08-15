package com.hamradio.ft710android.Data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MemoryChannelsTest {
    @Test fun `parse six slots with null padding`() {
        val raw = Json.parseToJsonElement(
            """[{"freq":7050000,"mode":"LSB","label":"40m 7.050"},null,{"freq":14270000,"mode":"USB","label":"20m 14.270"},null,null,null]"""
        ).jsonArray
        val list = MemoryChannels.parse(raw)
        assertEquals(6, list.size)
        assertEquals(MemoryChannel(7050000, "LSB", "40m 7.050"), list[0])
        assertNull(list[1])
        assertEquals(MemoryChannel(14270000, "USB", "20m 14.270"), list[2])
    }

    @Test fun `toJson round-trips six slots and drops null padding for save`() {
        val channels = listOf<MemoryChannel?>(
            MemoryChannel(7050000, "LSB", "40m 7.050"), null,
            MemoryChannel(14270000, "USB", "20m 14.270"), null, null, null
        )
        val json = MemoryChannels.toJson(channels)
        val expected = """[{"freq":7050000,"mode":"LSB","label":"40m 7.050"},{"freq":14270000,"mode":"USB","label":"20m 14.270"}]"""
        assertEquals(expected, json)
    }
}
