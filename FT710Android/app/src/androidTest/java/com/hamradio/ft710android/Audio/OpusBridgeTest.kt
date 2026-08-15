package com.hamradio.ft710android.Audio

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class OpusBridgeTest {
    @Test fun `encode decode round trip on silence`() {
        val enc = OpusBridge.encoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS, OpusBridge.BITRATE)
        val dec = OpusBridge.decoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS)
        assertTrue(enc != 0L)
        assertTrue(dec != 0L)
        val pcm = ShortArray(OpusBridge.FRAME_SAMPLES) // 静音
        val out = ByteArray(4096)
        val n = OpusBridge.encoderEncode(enc, pcm, out)
        assertTrue(n > 0)
        val back = ShortArray(OpusBridge.FRAME_SAMPLES)
        val s = OpusBridge.decoderDecode(dec, out.copyOf(n), n, back)
        assertEquals(OpusBridge.FRAME_SAMPLES, s)
        OpusBridge.destroyEncoder(enc)
        OpusBridge.destroyDecoder(dec)
    }
}
