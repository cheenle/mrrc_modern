package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Test

class SpectrumProcessorTest {
    @Test fun `feeds rows and keeps ring buffer at 120`() {
        val sp = SpectrumProcessor()
        for (k in 0 until 200) {
            val f = ByteArray(1701).also { it[0] = 0x01; for (i in 1..850) it[i] = k.toByte() }
            sp.onFrame(f)
        }
        assertEquals(120, sp.waterfall.size)
        assertEquals(850, sp.fft.size)
    }

    @Test fun `invalid frame is ignored`() {
        val sp = SpectrumProcessor()
        sp.onFrame(ByteArray(100))
        assertEquals(0, sp.waterfall.size)
    }
}
