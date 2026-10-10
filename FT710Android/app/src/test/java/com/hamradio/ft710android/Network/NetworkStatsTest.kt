package com.hamradio.ft710android.Network

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NetworkStatsTest {
    @Test fun `byte counters drain per second`() {
        val s = NetworkStats(nowMs = { 0 })
        s.onReceived(100); s.onReceived(50); s.onSent(20)
        assertEquals(150L, s.drainRx())
        assertEquals(0L, s.drainRx())   // 自上次取走
        assertEquals(20L, s.drainTx())
        assertEquals(0L, s.drainTx())
    }

    @Test fun `rtt is measured from ping to pong with the injected clock`() {
        var now = 1_000L
        val s = NetworkStats(nowMs = { now })
        s.onPingSent()
        now = 1_042L
        s.onPong()
        assertEquals(42L, s.lastRttMs)
    }

    @Test fun `pong without a ping leaves rtt untouched`() {
        val s = NetworkStats(nowMs = { 0 })
        s.onPong()
        assertNull(s.lastRttMs)
    }
}
