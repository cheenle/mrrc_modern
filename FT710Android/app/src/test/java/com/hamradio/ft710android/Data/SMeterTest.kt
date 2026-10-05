package com.hamradio.ft710android.Data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SMeterTest {
    @Test fun `fraction uses the full 0-255 range`() {
        assertEquals(0f, SMeter.fraction(0), 0.0001f)
        assertEquals(1f, SMeter.fraction(255), 0.0001f)
        assertEquals(0.5098f, SMeter.fraction(130), 0.0005f)   // S9 的刻度
        assertEquals(1f, SMeter.fraction(999))                 // 超界夹住
        assertEquals(0f, SMeter.fraction(-5))
    }

    @Test fun `markers match the web marks array`() {
        assertEquals(
            listOf(0, 12, 27, 40, 55, 65, 80, 95, 112, 130, 150, 172, 190, 220, 240, 255),
            SMeter.MARKERS.toList(),
        )
        assertTrue(SMeter.MARKERS.first() == 0 && SMeter.MARKERS.last() == SMeter.RAW_MAX)
    }

    @Test fun `labels are the eight web ticks`() {
        assertEquals(listOf("S1", "S3", "S5", "S7", "S9", "+20", "+40", "+60"), SMeter.LABELS)
    }
}
