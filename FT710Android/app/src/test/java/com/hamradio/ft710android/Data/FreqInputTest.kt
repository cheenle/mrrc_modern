package com.hamradio.ft710android.Data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class FreqInputTest {
    @Test fun `mhz with dot`() = assertEquals(7_050_000L, FreqInput.parse("7.05"))
    @Test fun `khz without dot under 100000`() = assertEquals(7_050_000L, FreqInput.parse("7050"))
    @Test fun `hz over 100000`() = assertEquals(7_050_000L, FreqInput.parse("7050000"))
    @Test fun `sub-mhz`() = assertEquals(500_000L, FreqInput.parse("0.5"))
    @Test fun `blank and junk are null`() {
        assertNull(FreqInput.parse(""))
        assertNull(FreqInput.parse("   "))
        assertNull(FreqInput.parse("abc"))
    }
    @Test fun `clamps to the transceiver range`() {
        assertEquals(30_000L, FreqInput.parse("0.001"))
        assertEquals(100_000L, FreqInput.parse("100"))      // 无小数点 → kHz
        assertEquals(75_000_000L, FreqInput.parse("100.0")) // 有小数点 → MHz，超高被夹住
    }
    @Test fun `qsy maps a fraction of the span around the vfo`() {
        assertEquals(14_270_000L, FreqInput.qsy(14_270_000L, 100_000L, 0.5f))
        assertEquals(14_220_000L, FreqInput.qsy(14_270_000L, 100_000L, 0f))
        assertEquals(14_320_000L, FreqInput.qsy(14_270_000L, 100_000L, 1f))
        assertEquals(30_000L, FreqInput.qsy(50_000L, 100_000L, 0f))
        assertEquals(75_000_000L, FreqInput.qsy(75_000_000L, 100_000L, 1f))
    }
}
