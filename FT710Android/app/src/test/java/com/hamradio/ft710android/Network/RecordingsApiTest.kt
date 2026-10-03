package com.hamradio.ft710android.Network

import org.junit.Assert.assertEquals
import org.junit.Test

class RecordingsApiTest {
    @Test fun `parses list payload`() {
        val rows = parseRecordingsList(
            """{"recordings":[{"name":"7050000kHz_20261003_120000.mp3","freq_hz":7050000,"started_at":"2026-10-03T12:00:00","duration":61.0,"bytes":488123,"recording":false}],"count":1,"total_bytes":488123}"""
        )
        assertEquals(1, rows.size)
        assertEquals("7050000kHz_20261003_120000.mp3", rows[0].name)
        assertEquals(7050000L, rows[0].freqHz)
        assertEquals(61.0, rows[0].duration, 0.001)
        assertEquals(488123L, rows[0].bytes)
    }

    @Test fun `malformed payload yields empty list`() {
        assertEquals(emptyList<RecordingRow>(), parseRecordingsList("<html>not json</html>"))
    }
}
