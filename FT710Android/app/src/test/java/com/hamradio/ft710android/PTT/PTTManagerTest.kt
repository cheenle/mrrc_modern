package com.hamradio.ft710android.PTT

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PTTManagerTest {
    private class Harness(dispatcher: CoroutineDispatcher) {
        val sentPTT = mutableListOf<Boolean>()
        var txAudioStopCalls = 0      // stopTxAudio
        var txAudioStopFrameCalls = 0 // sendTXAudioStop（'s:' 帧）
        var txAudioStartCalls = 0
        var connected = true
        var txStatus = 0
        var stuck = 0

        val manager = PTTManager(
            sendPTT = { sentPTT.add(it) },
            sendTXAudioStop = { txAudioStopFrameCalls++ },
            startTxAudio = { txAudioStartCalls++ },
            stopTxAudio = { txAudioStopCalls++ },
            serverTXStatus = { txStatus },
            isCtrlConnected = { connected },
            onStuckTX = { stuck++ },
            dispatcher = dispatcher,
        ).also { it.watchdogIntervalMs = 500; it.maxRetries = 3 }
    }

    @Test fun `press while disconnected is rejected without commands`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler)); h.connected = false
        h.manager.press()
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
        assertTrue(h.sentPTT.isEmpty())
        assertEquals(0, h.txAudioStartCalls)
    }

    @Test fun `press then release always sends ptt false`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        h.manager.press()
        assertTrue(h.manager.isTX)
        assertEquals(listOf(true), h.sentPTT)
        h.manager.release()
        assertEquals(listOf(true, false), h.sentPTT)
        assertEquals(1, h.txAudioStopCalls)       // stopTxAudio
        assertEquals(1, h.txAudioStopFrameCalls)  // 's:' 帧
    }

    @Test fun `watchdog resends and gives up after maxRetries`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        h.manager.press(); h.txStatus = 1
        h.manager.release()
        // t=500/1000/1500 各重发一次（advanceTimeBy 不含边界）；t=2000 用 runCurrent 触发耗尽检查
        advanceTimeBy(2000)
        runCurrent()
        assertEquals(4, h.sentPTT.count { !it }) // 1 release + 3 重发
        assertEquals(1, h.stuck)
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
    }

    @Test fun `watchdog exits when echo goes RX`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        h.manager.press(); h.txStatus = 1
        h.manager.release()
        advanceTimeBy(499)
        h.txStatus = 0
        advanceTimeBy(1)
        runCurrent() // 推进到 t=500，运行看门狗首轮，回显已 RX → Idle
        assertEquals(1, h.sentPTT.count { !it }) // 只有首次 release 那条
        assertEquals(0, h.stuck)
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
    }

    @Test fun `press during watchdog cancels retries`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        h.manager.press(); h.txStatus = 1
        h.manager.release()
        advanceTimeBy(500)
        runCurrent() // 看门狗首轮重发完成
        h.manager.press() // Releasing → Keyed，取消看门狗
        advanceTimeBy(2000)
        assertEquals(PTTManager.Phase.Keyed, h.manager.phase)
        assertEquals(0, h.stuck)
    }

    @Test fun `forceRelease is idempotent from any state`() = runTest {
        val h = Harness(StandardTestDispatcher(testScheduler))
        h.manager.press(); h.txStatus = 1
        h.manager.forceRelease()
        h.manager.forceRelease()
        assertEquals(2, h.sentPTT.count { !it })
        assertEquals(2, h.txAudioStopCalls)
        assertEquals(2, h.txAudioStopFrameCalls)
        assertEquals(PTTManager.Phase.Idle, h.manager.phase)
    }
}
