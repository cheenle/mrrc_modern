package com.hamradio.ft710android.Spectrum

import org.junit.Assert.assertArrayEquals
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
        assertEquals(850, sf.wf2!!.size)
        assertEquals(0x55, sf.wf1[0] and 0xFF)
        assertEquals(1, sf.wf2!![0] and 0xFF)
    }

    private fun makeShortFrame(wf1Value: Byte = 0x55): ByteArray {
        val f = ByteArray(851)
        f[0] = 0x01
        for (i in 1..850) f[i] = wf1Value
        return f
    }

    @Test fun `parses the 851-byte wf1-only frame (server wf1 tier, AD-025)`() {
        val sf = parseSpectrumFrame(makeShortFrame())!!
        assertEquals(1, sf.version)
        assertEquals(850, sf.wf1.size)
        assertEquals(0x55, sf.wf1[0] and 0xFF)
        assertNull("短帧没有 wf2，不该假装有一个全零的第二瀑布", sf.wf2)
    }

    @Test fun `short frame wf1 equals the full frame wf1`() {
        // 服务端的短帧就是满帧的 full[:851] 切片，所以两者的 wf1 必须逐元素相等。
        val full = parseSpectrumFrame(makeFrame())!!
        val short = parseSpectrumFrame(makeShortFrame())!!
        assertArrayEquals(full.wf1, short.wf1)
    }

    @Test fun `still rejects every other length`() {
        // 只放行 851 与 1701：半截帧被当成有效数据会画进瀑布，
        // 而多出来的字节被默默忽略则会掩盖服务端的格式漂移。
        listOf(0, 1, 100, 850, 852, 1700, 1702, 3402).forEach { n ->
            val buf = ByteArray(n).also { if (n > 0) it[0] = 0x01 }
            assertNull("length $n", parseSpectrumFrame(buf))
        }
    }

    @Test fun `rejects wrong version byte on a short frame too`() {
        val bad = makeShortFrame().also { it[0] = 0x02 }
        assertNull(parseSpectrumFrame(bad))
    }

    @Test fun `rejects wrong length`() { assertNull(parseSpectrumFrame(ByteArray(100))) }

    @Test fun `rejects wrong version byte`() {
        val bad = makeFrame().also { it[0] = 0x02 }
        assertNull(parseSpectrumFrame(bad))
    }

    @Test fun `handles empty input`() { assertNull(parseSpectrumFrame(ByteArray(0))) }
}
