package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class SpectrumFrameTest {
    private fun makeFrame(wf1Value: Byte = 0x55): ByteArray {
        val f = ByteArray(1701)
        f[0] = 0x01
        for (i in 1..850) f[i] = wf1Value
        for (i in 851..1700) f[i] = (i - 850).toByte()
        return f
    }

    @Test fun `parses valid 1701-byte frame`() {
        val sf = parseSpectrumFrame(makeFrame())!!
        assertEquals(1, sf.version)
        assertEquals(850, sf.wf1.size)
        assertEquals(850, sf.wf2.size)
        assertEquals(0x55, sf.wf1[0] and 0xFF)
        assertEquals(1, sf.wf2[0] and 0xFF)
    }

    @Test fun `rejects wrong length`() { assertNull(parseSpectrumFrame(ByteArray(100))) }

    @Test fun `rejects wrong version byte`() {
        val bad = makeFrame().also { it[0] = 0x02 }
        assertNull(parseSpectrumFrame(bad))
    }

    @Test fun `handles empty input`() { assertNull(parseSpectrumFrame(ByteArray(0))) }
}
