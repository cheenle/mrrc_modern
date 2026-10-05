package com.hamradio.ft710android.Data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FreqScaleTest {
    @Test fun `step matches the web ladder`() {
        assertEquals(200L, FreqScale.step(1_000))
        assertEquals(200L, FreqScale.step(2_000))
        assertEquals(500L, FreqScale.step(5_000))
        assertEquals(1_000L, FreqScale.step(10_000))
        assertEquals(2_500L, FreqScale.step(20_000))
        assertEquals(5_000L, FreqScale.step(50_000))
        assertEquals(10_000L, FreqScale.step(100_000))
        assertEquals(25_000L, FreqScale.step(200_000))
        assertEquals(50_000L, FreqScale.step(500_000))
        assertEquals(100_000L, FreqScale.step(1_000_000))
    }

    @Test fun `label format follows the step`() {
        assertEquals("7050.0k", FreqScale.label(7_050_000, 200))
        assertEquals("7050k", FreqScale.label(7_050_000, 5_000))
        assertEquals("7.05", FreqScale.label(7_050_000, 25_000))
        assertEquals("7.050", FreqScale.label(7_050_000, 100_000))
    }

    @Test fun `left edge is the vfo minus half span (never scope_start_freq)`() {
        assertEquals(14_220_000.0, FreqScale.leftEdge(14_270_000, 100_000), 0.001)
        assertEquals(13_770_000.0, FreqScale.leftEdge(14_270_000, 1_000_000), 0.001)
    }

    @Test fun `ticks are centered on the vfo and about 8-12 across`() {
        val t = FreqScale.ticks(14_270_000, 100_000)   // step 10k → 11 marks
        assertEquals(11, t.size)
        assertEquals(14_220_000L, t.first().first)
        assertEquals(14_320_000L, t.last().first)
        assertEquals(0f, t.first().second, 0.0001f)
        assertEquals(1f, t.last().second, 0.0001f)
        // 中间必有一个正好在 VFO 上的刻度（100k / 10k = 10 段）
        assertTrue(t.any { it.first == 14_270_000L && kotlin.math.abs(it.second - 0.5f) < 0.0001f })
    }

    @Test fun `ticks stay inside the visible range when the edge falls between marks`() {
        val t = FreqScale.ticks(14_270_000, 200_000)   // step 25k，左边缘 14.17M 不被 25k 整除
        assertTrue(t.size in 8..12)
        assertTrue(t.all { it.second in 0f..1f })
        assertTrue(t.first().first >= 14_170_000L)
        assertTrue(t.last().first <= 14_370_000L)
    }
}
