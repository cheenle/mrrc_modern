package com.hamradio.ft710android.ViewModel

import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.parseAtrEvent
import com.hamradio.ft710android.Network.parseWsEvent
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.Spectrum.SpectrumProcessor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class MainViewModelTest {
    private fun cm(scope: CoroutineScope) =
        ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = {})

    @Test fun `fullState applies to state and exposes bands`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(
            authApi = null, connectionManager = cm(scope), rxPlayer = null,
            txCapture = null, spectrumProcessor = null, memoryChannelsStore = null,
            pttManager = null, scope = scope,
        )
        vm.onWsEvent(parseWsEvent(
            """{"type":"fullState","data":{"vfo_a_freq":7050000,"mode":1},"bands":[{"name":"20m","default_freq":14270000}],"modes":["USB"],"memChannels":[null,null,null,null,null,null]}"""
        ))
        assertEquals(7050000L, vm.state.vfoAFreq)
        assertEquals("20m", vm.bands.value[0].name)
        assertEquals(14270000L, vm.bands.value[0].defaultFreq)
        assertEquals(1L, vm.version.value) // apply 后版本递增，驱动 Compose 重组
    }

    @Test fun `connection change updates connected flow`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        assertFalse(vm.connected.value)
        vm.onConnectionChange(true)
        assertTrue(vm.connected.value)
        vm.onConnectionChange(false)
        assertFalse(vm.connected.value)
    }

    @Test fun `listen only event flips the flow`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        assertFalse(vm.listenOnly.value)
        vm.onListenOnly()
        assertTrue(vm.listenOnly.value)
    }

    @Test fun `fullState with recording and cq marks them available`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        vm.onWsEvent(parseWsEvent(
            """{"type":"fullState","data":{},"bands":[],"modes":[],"memChannels":[],"recording":{"recording":false},"cq":{"state":"idle"}}"""
        ))
        assertTrue(vm.recordingsAvailable.value)
        assertTrue(vm.cqAvailable.value)
    }

    @Test fun `stateUpdate with tx_status feeds ptt manager`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        var fed = -1
        val spy = object : PTTManager(
            sendPTT = {}, sendTXAudioStop = {}, sendHeartbeat = {}, startTxAudio = {}, stopTxAudio = {},
            serverTXStatus = { 0 }, isCtrlConnected = { true }, onStuckTX = {},
            dispatcher = UnconfinedTestDispatcher(),
        ) {
            override fun onStatusReceived(txStatus: Int) { fed = txStatus }
        }
        val vm = MainViewModel(null, cm(scope), null, null, null, null, spy, scope)
        vm.onWsEvent(parseWsEvent("""{"type":"stateUpdate","fields":{"tx_status":1},"dirty":["tx_status"]}"""))
        assertEquals(1, fed)
    }

    @Test fun `fullState exposes capabilities and display name`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        vm.onWsEvent(parseWsEvent(
            """{"type":"fullState","data":{},"bands":[{"name":"40m","default_freq":7050000}],"modes":["USB"],
                "radioDisplayName":"Yaesu FT-710","capabilities":{"scope_type":"civ27","audio_gain_boost":1.0}}"""
        ))
        assertEquals("Yaesu FT-710", vm.displayName.value)
        assertEquals(1f, vm.caps.value.audioBoost)
        assertEquals("civ27", vm.caps.value.scopeType)
    }

    @Test fun `saved mic gain is pushed back when it differs`() = runTest(UnconfinedTestDispatcher()) {
        val sent = mutableListOf<String>()
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = { sent.add(it) })
        val vm = MainViewModel(null, cm, null, null, null, null, null, scope)
        vm.setSavedMicGain(55)
        vm.onWsEvent(parseWsEvent("""{"type":"fullState","data":{"mic_gain":20},"bands":[],"modes":[],"memChannels":[]}"""))
        assertTrue(sent.any { it.contains("\"mic_gain\"") && it.contains("55") })
    }

    @Test fun `qsy targets the active vfo field`() = runTest(UnconfinedTestDispatcher()) {
        val sent = mutableListOf<String>()
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = { sent.add(it) })
        val vm = MainViewModel(null, cm, null, null, null, null, null, scope)
        vm.onWsEvent(parseWsEvent(
            """{"type":"fullState","data":{"vfo_a_freq":14270000,"active_vfo":"A","scope_span":6},"bands":[],"modes":[],"memChannels":[]}"""
        ))
        vm.qsy(0.5f)
        assertTrue(sent.last().contains("\"field\":\"freq\"") && sent.last().contains("14270000"))
        vm.onWsEvent(parseWsEvent("""{"type":"stateUpdate","fields":{"active_vfo":"B"},"dirty":["active_vfo"]}"""))
        vm.qsy(0f)
        assertTrue(sent.last().contains("\"field\":\"vfo_b_freq\""))
    }

    @Test fun `disconnect marks the user-off state`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = {})
        val vm = MainViewModel(null, cm, null, null, null, null, null, scope)
        vm.disconnect()
        assertTrue(vm.userOff.value)
    }

    @Test fun `atr state flows into the view model`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val vm = MainViewModel(null, cm(scope), null, null, null, null, null, scope)
        vm.onAtrEvent(parseAtrEvent(
            """{"type":"atrState","connected":true,"power":30.0,"swr":1.2,"sw":0,"ind":5,"cap":9,"tuning":false}"""
        )!!)
        assertEquals(30.0, vm.atrState.value!!.power, 0.001)
        assertEquals(5, vm.atrState.value!!.ind)
    }

    @Test fun `spectrum frames reach the waterfall and fft flows`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val sp = SpectrumProcessor()
        val vm = MainViewModel(null, cm(scope), null, null, sp, null, null, scope)
        assertTrue(vm.waterfall.value.isEmpty())
        val frame = ByteArray(1701).also { it[0] = 0x01; for (i in 1..850) it[i] = 42 }
        vm.onSpectrumFrame(frame)
        assertEquals(1, vm.waterfall.value.size)
        assertEquals(42, vm.waterfall.value[0][0])
        assertEquals(42, vm.fft.value[0])
    }

    @Test fun `the audio player follows its own channel, not the aggregate`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        val fake = FakeRxPlayer()
        val vm = MainViewModel(null, cm(scope), fake, null, null, null, null, scope)
        vm.onAudioRxChange(true)
        assertEquals(1, fake.startCount)
        vm.onAudioRxChange(false)
        assertEquals(1, fake.stopCount)
        vm.onAudioRxChange(true)
        assertEquals(2, fake.startCount) // 重连后重新启动（RxAudioPlayer.start() 自身幂等）
    }

    private class FakeRxPlayer : MainViewModel.RxPlayerLike {
        var startCount = 0
        var stopCount = 0
        override fun onFrame(frame: ByteArray) {}
        override fun start() { startCount++ }
        override fun stop() { stopCount++ }
        override fun setVolume(v: Int) {}
        override fun setBoost(b: Float) {}
        override fun setTransmitting(t: Boolean) {}
        override val bufferMs: Int get() = 0
        override fun stats(): String = "fake"
    }
}
