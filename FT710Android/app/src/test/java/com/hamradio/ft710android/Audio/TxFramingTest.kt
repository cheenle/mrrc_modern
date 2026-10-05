package com.hamradio.ft710android.Audio

import org.junit.Assert.assertEquals
import org.junit.Test

class TxFramingTest {
    @Test fun `frame length is 20ms at either rate`() {
        assertEquals(960, TxFraming.frameSamples(48000))
        assertEquals(882, TxFraming.frameSamples(44100))
    }

    @Test fun `mic volume scales and clamps`() {
        val half = TxFraming.applyMicVol(shortArrayOf(1000, -1000), 50)
        assertEquals(500, half[0].toInt())
        assertEquals(-500, half[1].toInt())
        val loud = TxFraming.applyMicVol(shortArrayOf(20000, -20000), 200)
        assertEquals(32767, loud[0].toInt())
        assertEquals(-32768, loud[1].toInt())
        val zero = TxFraming.applyMicVol(shortArrayOf(1000), 0)
        assertEquals(0, zero[0].toInt())
    }

    @Test fun `unity volume leaves samples untouched`() {
        val src = shortArrayOf(123, -456)
        assertEquals(123, TxFraming.applyMicVol(src, 100)[0].toInt())
    }
}
