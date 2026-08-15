package com.hamradio.ft710android.Network

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class ConnectionManagerTest {
    @Test fun `sendSet routes by value type`() {
        val sent = mutableListOf<String>()
        val cm = ConnectionManager(
            client = OkHttpClient(),
            scope = CoroutineScope(Dispatchers.Unconfined),
            onRadioEvent = {}, onAudioRx = {}, onSpectrum = {}, onAudioTxText = {},
            onAtrEvent = {}, onConnectionChange = {},
            sendOverride = { sent.add(it) },
        )
        cm.sendSet("freq", 7050000)
        cm.sendSet("mode", "USB")
        cm.sendSet("ptt", true)
        assertEquals(listOf(
            """{"type":"set","field":"freq","value":7050000}""",
            """{"type":"set","field":"mode","value":"USB"}""",
            """{"type":"set","field":"ptt","value":true}""",
        ), sent)
    }

    @Test fun `wsUrl converts scheme`() {
        assertEquals("wss://radio.vlsc.net:8888/WSradio?token=abc",
            ConnectionManager.wsUrl("https://radio.vlsc.net:8888", "/WSradio", "abc"))
        assertEquals("ws://192.168.1.10:8888/WSspectrum?token=t",
            ConnectionManager.wsUrl("http://192.168.1.10:8888", "/WSspectrum", "t"))
    }

    @Test fun `isConnected false before start`() {
        val cm = ConnectionManager(OkHttpClient(), CoroutineScope(Dispatchers.Unconfined),
            {}, {}, {}, {}, {}, {}, sendOverride = {})
        assertFalse(cm.isConnected)
    }
}
