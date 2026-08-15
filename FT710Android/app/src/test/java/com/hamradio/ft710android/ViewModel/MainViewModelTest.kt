package com.hamradio.ft710android.ViewModel

import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.PTT.PTTManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
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
        vm.onWsEvent(
            """{"type":"fullState","data":{"vfo_a_freq":7050000,"mode":1},"bands":["20m"],"modes":["USB"],"memChannels":[null,null,null,null,null,null]}"""
        )
        assertEquals(7050000L, vm.state.vfoAFreq)
        assertEquals(listOf("20m"), vm.bands.value)
        assertEquals(1L, vm.version.value) // apply 后版本递增，驱动 Compose 重组
    }

    @Test fun `stateUpdate with tx_status feeds ptt manager`() = runTest(UnconfinedTestDispatcher()) {
        val scope = CoroutineScope(UnconfinedTestDispatcher())
        var fed = -1
        val spy = object : PTTManager(
            sendPTT = {}, sendTXAudioStop = {}, startTxAudio = {}, stopTxAudio = {},
            serverTXStatus = { 0 }, isCtrlConnected = { true }, onStuckTX = {},
            dispatcher = UnconfinedTestDispatcher(),
        ) {
            override fun onStatusReceived(txStatus: Int) { fed = txStatus }
        }
        val vm = MainViewModel(null, cm(scope), null, null, null, null, spy, scope)
        vm.onWsEvent("""{"type":"stateUpdate","fields":{"tx_status":1},"dirty":["tx_status"]}""")
        assertEquals(1, fed)
    }
}
