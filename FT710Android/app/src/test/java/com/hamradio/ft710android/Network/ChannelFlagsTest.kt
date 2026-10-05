package com.hamradio.ft710android.Network

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ChannelFlagsTest {
    @Test fun `add and remove track a path`() {
        val flags = ChannelFlags()
        assertFalse(flags.has("/WSradio"))
        flags.add("/WSradio")
        assertTrue(flags.has("/WSradio"))
        flags.remove("/WSradio")
        assertFalse(flags.has("/WSradio"))
    }

    @Test fun `hasAll needs every required path`() {
        val flags = ChannelFlags()
        flags.add("/WSradio"); flags.add("/WSaudioRX")
        assertFalse(flags.hasAll(setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum")))
        flags.add("/WSaudioTX"); flags.add("/WSspectrum")
        assertTrue(flags.hasAll(setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum")))
    }

    @Test fun `clear drops everything`() {
        val flags = ChannelFlags()
        flags.add("/WSradio")
        flags.clear()
        assertFalse(flags.has("/WSradio"))
    }

    /**
     * 4+1 路 OkHttp 回调线程并发 add/remove 的不变量：最后每个通道都必须在线。
     * 早期普通 mutableSet 实现会丢掉并发的 add，导致聚合永不成立（真机无声事故）。
     */
    @Test fun `concurrent updates never lose a channel`() {
        val flags = ChannelFlags()
        val paths = listOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum", "/WSatr1000")
        val threads = paths.map { p ->
            Thread {
                repeat(2_000) {
                    flags.add(p)
                    flags.remove(p)
                }
                flags.add(p)
            }
        }
        threads.forEach { it.start() }
        threads.forEach { it.join() }
        assertTrue(flags.hasAll(paths.toSet()))
        assertTrue(flags.snapshot().containsAll(paths))
    }
}
