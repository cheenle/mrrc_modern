package com.hamradio.ft710android.Audio

import org.junit.Assert.assertEquals
import org.junit.Test

class ResamplerTest {
    @Test fun `882 to 960 keeps a DC level`() {
        val out = Resampler.resample882To960(ShortArray(882) { 1000 })
        assertEquals(960, out.size)
        assertEquals(1000.toShort(), out[0])
        assertEquals(1000.toShort(), out[959])
    }

    @Test fun `linear interpolation follows a ramp`() {
        val input = ShortArray(882) { it.toShort() }
        val out = Resampler.resample882To960(input)
        assertEquals(0, out.first().toInt())
        // 最后一点的位置 = 959 × 44100/48000 ≈ 881.08 → 值 ≈ 881
        assertEquals(881, out.last().toInt())
    }

    @Test fun `empty input yields empty output`() {
        assertEquals(0, Resampler.resample(ShortArray(0), 44100, 48000).size)
    }

    @Test fun `clamps via rounding`() {
        val out = Resampler.resample(shortArrayOf(-32768, 32767), 44100, 48000)
        assertEquals(-32768, out.first().toInt())
    }
}
