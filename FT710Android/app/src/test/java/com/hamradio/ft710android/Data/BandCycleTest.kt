package com.hamradio.ft710android.Data

import org.junit.Assert.assertEquals
import org.junit.Test

class BandCycleTest {
    @Test fun `cycles forward through the table`() {
        assertEquals("30m", BandCycle.next("40m").name)
        assertEquals("60m", BandCycle.next("80m").name)
    }

    @Test fun `unknown band starts from 160m then advances`() {
        assertEquals("80m", BandCycle.next("GEN").name)   // web: idx<0 → 0 → +1
    }

    @Test fun `last band wraps to 160m`() {
        assertEquals("160m", BandCycle.next("4m").name)
    }

    @Test fun `default frequencies match the web table`() {
        assertEquals(7_050_000L, BandCycle.bands.first { it.name == "40m" }.defaultFreq)
        assertEquals(14_270_000L, BandCycle.bands.first { it.name == "20m" }.defaultFreq)
        assertEquals(29_700_000L, BandCycle.bands.first { it.name == "10m" }.end)
    }
}
