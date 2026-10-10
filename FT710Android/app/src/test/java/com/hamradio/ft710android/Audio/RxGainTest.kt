package com.hamradio.ft710android.Audio

import org.junit.Assert.assertEquals
import org.junit.Test

class RxGainTest {
    @Test fun `ft710 needs the 10x boost, capped`() {
        assertEquals(5.0196075f, RxGain.target(128, 10f, transmitting = false), 0.0001f)
        assertEquals(10f, RxGain.target(255, 10f, transmitting = false), 0.0001f)
    }

    @Test fun `ic7300 boost is unity`() {
        assertEquals(0.5019608f, RxGain.target(128, 1f, transmitting = false), 0.0001f)
    }

    @Test fun `transmitting mutes playback`() {
        assertEquals(0f, RxGain.target(255, 10f, transmitting = true), 0.0001f)
    }
}
