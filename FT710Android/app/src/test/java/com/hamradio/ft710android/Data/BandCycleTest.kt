package com.hamradio.ft710android.Data

import com.hamradio.ft710android.Network.BandDto
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

    /**
     * Yaesu 家族的 bands 没有 `default_freq`（`[name, low, high]`），而波段格发的是
     * `freq`；不补就会往电台写 0 Hz。补的来源必须是本地这张与 web 逐字一致的表。
     */
    @Test fun `server bands without default frequency are filled from the table`() {
        val fromServer = listOf(
            BandDto("20m", 14_000_000, 14_350_000),
            BandDto("160m", 1_800_000, 2_000_000),
        )
        val filled = BandCycle.withDefaults(fromServer)
        assertEquals(14_270_000L, filled[0].defaultFreq)
        assertEquals(1_845_500L, filled[1].defaultFreq)
    }

    /** 表里没有的波段（2m / 70cm / 空气段）取中点，向下对齐 1 kHz。 */
    @Test fun `unknown bands fall back to the midpoint`() {
        val filled = BandCycle.withDefaults(listOf(BandDto("2m", 144_000_000, 148_000_000)))
        assertEquals(146_000_000L, filled[0].defaultFreq)
        val odd = BandCycle.withDefaults(listOf(BandDto("air", 118_000_000, 118_000_999)))
        assertEquals(118_000_000L, odd[0].defaultFreq)
    }

    /** 服务端已经给的（FT-710 对象形状）原样保留。 */
    @Test fun `a server supplied default frequency is not overwritten`() {
        val filled = BandCycle.withDefaults(listOf(BandDto("40m", 7_000_000, 7_300_000, 7_074_000)))
        assertEquals(7_074_000L, filled[0].defaultFreq)
    }
}
