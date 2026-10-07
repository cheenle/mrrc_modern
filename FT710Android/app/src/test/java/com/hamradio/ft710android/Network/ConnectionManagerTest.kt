package com.hamradio.ft710android.Network

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
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

    @Test fun `sendHeartbeat routes txhb on the control channel`() {
        val sent = mutableListOf<String>()
        val cm = ConnectionManager(OkHttpClient(), CoroutineScope(Dispatchers.Unconfined),
            {}, {}, {}, {}, {}, {}, sendOverride = { sent.add(it) })
        cm.sendHeartbeat()
        assertEquals(listOf("""{"type":"txhb"}"""), sent)
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

    /**
     * ATR-1000 是**可选选件**：配置文件里没有（服务端 `atr is None` → `atr1000Enabled:false`）
     * 就不该去连 `/WSatr1000` —— 服务端会 accept 后立刻以 4000 "ATR1000 disabled" 关闭，
     * 白握手一次还制造无意义的关闭事件。
     */
    @Test fun `ATR channel stays closed until the server says it is enabled`() {
        val cm = ConnectionManager(
            client = OkHttpClient(),
            scope = CoroutineScope(Dispatchers.Unconfined),
            onRadioEvent = {}, onAudioRx = {}, onSpectrum = {}, onAudioTxText = {},
            onAtrEvent = {}, onConnectionChange = {},
            sendOverride = {},
        )
        cm.start("http://127.0.0.1:1", "tok")
        // 只连四路核心通道
        assertEquals(
            listOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum"),
            cm.openedPaths,
        )
        assertFalse(cm.isAtrEnabled)

        // fullState 说启用了 → 才连；重复调用不重复连
        cm.setAtrEnabled(true)
        assertTrue(cm.isAtrEnabled)
        assertTrue(cm.openedPaths.contains("/WSatr1000"))
        cm.setAtrEnabled(true)
        assertEquals("setAtrEnabled 必须幂等", 1, cm.openedPaths.count { it == "/WSatr1000" })

        // 关掉后重连：stopAll 清标志，冷启动不补连
        cm.setAtrEnabled(false)
        assertFalse(cm.isAtrEnabled)
        cm.start("http://127.0.0.1:1", "tok")
        assertFalse("新会话应从干净状态开始", cm.openedPaths.contains("/WSatr1000"))
        cm.stopAll()
    }

    @Test fun `ATR tune is a no-op while the optional channel is absent`() {
        val cm = ConnectionManager(
            client = OkHttpClient(),
            scope = CoroutineScope(Dispatchers.Unconfined),
            onRadioEvent = {}, onAudioRx = {}, onSpectrum = {}, onAudioTxText = {},
            onAtrEvent = {}, onConnectionChange = {},
            sendOverride = {},
        )
        // 没连过 ATR → sendAtrTune 返回 false，且不抛
        assertFalse(cm.sendAtrTune())
    }
}
