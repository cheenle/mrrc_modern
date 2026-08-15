package com.hamradio.ft710android.PTT

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * PTT 安全状态机。纯 Kotlin，全部依赖注入，JVM 可单测。
 *
 * 安全铁律（spec §7，直接修掉 iOS P0 竞态）：
 *  - release() 无条件发 ptt:false（不等服务端回显）。
 *  - release 后看门狗轮询回显，仍 TX 则重发 TX0，最多 maxRetries 次。
 *  - forceRelease() 任意状态幂等，供断连/退后台调用。
 *  - press() 仅在 Idle 且控制通道已连接时受理——避免"发不出去的乐观 TX"。
 */
class PTTManager(
    val sendPTT: (Boolean) -> Unit,
    val sendTXAudioStop: () -> Unit,
    val startTxAudio: () -> Unit,
    val stopTxAudio: () -> Unit,
    val serverTXStatus: () -> Int,
    val isCtrlConnected: () -> Boolean,
    val onStuckTX: () -> Unit,
    private val dispatcher: CoroutineDispatcher? = null,
) {
    enum class Phase { Idle, Keying, Keyed, Releasing }

    @Volatile var phase: Phase = Phase.Idle; private set
    val isTX: Boolean get() = phase == Phase.Keying || phase == Phase.Keyed

    var watchdogIntervalMs: Long = 500
    var maxRetries: Int = 3

    private val scope = CoroutineScope(SupervisorJob() + (dispatcher ?: Dispatchers.Default))
    private var watchdogJob: Job? = null
    private var retryCount = 0

    fun press() {
        // Idle 正常受理；Releasing（看门狗重试中）视为合法再发射——取消看门狗回 Keyed。
        if (phase != Phase.Idle && phase != Phase.Releasing) return
        if (!isCtrlConnected()) return // 不产生任何命令
        sendPTT(true)
        startTxAudio()
        watchdogJob?.cancel()
        retryCount = 0
        phase = Phase.Keyed // 乐观置位，不等回显
    }

    fun release() {
        if (phase == Phase.Idle) return
        sendPTT(false)
        stopTxAudio()
        sendTXAudioStop()
        phase = Phase.Releasing
        startWatchdog()
    }

    fun forceRelease() {
        sendPTT(false)
        stopTxAudio()
        sendTXAudioStop()
        watchdogJob?.cancel()
        retryCount = 0
        phase = Phase.Idle
        startWatchdog() // 幂等：重复调用无害，重试会在回显 RX 后停
    }

    /** 外部收到 tx_status 回显时的入口（看门狗与 UI 共用）。open 供测试 spy 覆写。 */
    open fun onStatusReceived(txStatus: Int) {
        if (phase == Phase.Releasing && txStatus == 0) {
            watchdogJob?.cancel()
            retryCount = 0
            phase = Phase.Idle
        }
    }

    private fun startWatchdog() {
        watchdogJob?.cancel()
        retryCount = 0
        watchdogJob = scope.launch {
            while (true) {
                delay(watchdogIntervalMs)
                if (serverTXStatus() == 0) {
                    phase = Phase.Idle; retryCount = 0; return@launch
                }
                if (retryCount >= maxRetries) {
                    onStuckTX(); phase = Phase.Idle; retryCount = 0; return@launch
                }
                sendPTT(false)
                retryCount++
            }
        }
    }
}
