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

    /**
     * 连接判据是"后台停频谱"的安全带：
     * `/WSspectrum` 一旦仍被算作必需通道，退后台暂停频谱就会让 `isConnected` 变 false，
     * 而 VM 的 `syncBackground()` 依赖它 → **后台接收被自己关掉**，
     * 用户以为还在收音，其实早就断了。
     */
    @Test fun `paused spectrum is not a required channel`() {
        assertEquals(
            setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum"),
            ConnectionManager.requiredChannels(listenOnly = false, spectrumPaused = false),
        )
        // 后台暂停频谱：判据里不能有它，否则连接会被判死
        assertEquals(
            setOf("/WSradio", "/WSaudioRX", "/WSaudioTX"),
            ConnectionManager.requiredChannels(listenOnly = false, spectrumPaused = true),
        )
        // 只读会话本来就没有 TX 通道（服务端 4003 关闭）
        assertEquals(
            setOf("/WSradio", "/WSaudioRX", "/WSspectrum"),
            ConnectionManager.requiredChannels(listenOnly = true, spectrumPaused = false),
        )
        // 两个条件同时成立
        assertEquals(
            setOf("/WSradio", "/WSaudioRX"),
            ConnectionManager.requiredChannels(listenOnly = true, spectrumPaused = true),
        )
    }

    @Test fun `spectrum channel is closed while paused and reopened on resume`() {
        val cm = ConnectionManager(
            client = OkHttpClient(),
            scope = CoroutineScope(Dispatchers.Unconfined),
            onRadioEvent = {}, onAudioRx = {}, onSpectrum = {}, onAudioTxText = {},
            onAtrEvent = {}, onConnectionChange = {},
            sendOverride = {},
        )
        cm.start("http://127.0.0.1:1", "tok")
        assertTrue("前台应当连着频谱", cm.openedPaths.contains("/WSspectrum"))
        assertFalse(cm.isSpectrumPaused)

        // 退后台 → 暂停
        cm.setSpectrumPaused(true)
        assertTrue(cm.isSpectrumPaused)
        assertTrue("诊断摘要要能看出是主动暂停而不是断了", cm.channelsSummary().contains("S(p)"))

        // 回前台 → 重连
        cm.setSpectrumPaused(false)
        assertFalse(cm.isSpectrumPaused)
        assertEquals("恢复时应重连一次频谱", 2, cm.openedPaths.count { it == "/WSspectrum" })

        // 幂等：重复设置不重复连
        cm.setSpectrumPaused(false)
        assertEquals(2, cm.openedPaths.count { it == "/WSspectrum" })

        // 暂停状态下重连会话（网络切换等）不该把频谱又拉起来。
        // 注意 start() 会先 stopAll()，而 stopAll() 清空 openedPaths 日志，
        // 所以这里看的是"新会话里有没有频谱"——0 才是对的（有则说明白耗流量）。
        cm.setSpectrumPaused(true)
        cm.start("http://127.0.0.1:1", "tok")
        assertEquals(
            "后台重连不应恢复频谱（四路核心通道照连）",
            listOf("/WSradio", "/WSaudioRX", "/WSaudioTX"),
            cm.openedPaths,
        )
        cm.stopAll()
    }

    // ── 频谱带宽档位声明（服务端 AD-025）────────────────────

    private fun spectrumCm(caps: MutableList<String>) = ConnectionManager(
        client = OkHttpClient(),
        scope = CoroutineScope(Dispatchers.Unconfined),
        onRadioEvent = {}, onAudioRx = {}, onSpectrum = {}, onAudioTxText = {},
        onAtrEvent = {}, onConnectionChange = {},
        sendOverride = {},
        spectrumSendOverride = { caps.add(it) },
    )

    @Test fun `the tier is declared on the spectrum channel at connect`() {
        val caps = mutableListOf<String>()
        val cm = spectrumCm(caps)
        cm.start("http://127.0.0.1:1", "tok")
        // 默认档必须主动声明：不发 caps 的话服务端永远发 high，手机一点流量也省不下来。
        assertEquals(listOf("""{"type":"spectrumCaps","profile":"high"}"""), caps)
        cm.stopAll()
    }

    @Test fun `changing the tier re-declares on the same socket without reconnecting`() {
        val caps = mutableListOf<String>()
        val cm = spectrumCm(caps)
        cm.start("http://127.0.0.1:1", "tok")
        cm.setSpectrumProfile("low")
        assertEquals(listOf(
            """{"type":"spectrumCaps","profile":"high"}""",
            """{"type":"spectrumCaps","profile":"low"}""",
        ), caps)
        // 关键不变量：换档不得重连。重连会惊动 requiredChannels，
        // 而后台暂停期间把 /WSspectrum 拉回来正是 setSpectrumPaused 禁止的事。
        assertEquals("换档不该重连频谱通道", 1, cm.openedPaths.count { it == "/WSspectrum" })
        cm.stopAll()
    }

    @Test fun `setting the same tier twice is idempotent`() {
        val caps = mutableListOf<String>()
        val cm = spectrumCm(caps)
        cm.start("http://127.0.0.1:1", "tok")
        cm.setSpectrumProfile("mid")
        cm.setSpectrumProfile("mid")
        assertEquals(2, caps.size)   // 初始 high + 一次 mid，重复设置不再发
        assertEquals("mid", cm.currentSpectrumProfile)
        cm.stopAll()
    }

    @Test fun `an unknown or internal tier never reaches the wire`() {
        val caps = mutableListOf<String>()
        val cm = spectrumCm(caps)
        cm.start("http://127.0.0.1:1", "tok")
        // 服务端白名单只有 high/mid/low；listen 是它给只读会话的内部默认档。
        // 发非法值不会报错，只会被人忽略——于是 UI 显示“已省流量”而实际在拿满帧。
        cm.setSpectrumProfile("turbo")
        cm.setSpectrumProfile("listen")
        cm.setSpectrumProfile("")
        assertEquals("high", cm.currentSpectrumProfile)
        assertEquals("""{"type":"spectrumCaps","profile":"high"}""", caps.last())
        cm.stopAll()
    }

    @Test fun `a tier change while paused does not wake the spectrum channel`() {
        val caps = mutableListOf<String>()
        val cm = spectrumCm(caps)
        cm.start("http://127.0.0.1:1", "tok")
        cm.setSpectrumPaused(true)
        assertEquals(1, cm.openedPaths.count { it == "/WSspectrum" })

        cm.setSpectrumProfile("low")
        assertEquals("后台改档不得重连频谱", 1, cm.openedPaths.count { it == "/WSspectrum" })
        assertEquals("low", cm.currentSpectrumProfile)

        // 回到前台：重连一次，并且带的是新档位
        cm.setSpectrumPaused(false)
        assertEquals(2, cm.openedPaths.count { it == "/WSspectrum" })
        assertEquals("""{"type":"spectrumCaps","profile":"low"}""", caps.last())
        cm.stopAll()
    }
}
