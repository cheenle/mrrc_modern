package com.hamradio.ft710android.Network

/**
 * 5 路 WS 的收发字节计数 + RTT。时钟可注入，纯 JVM 可测。
 * 计数器是"自上次 drain 以来"的增量：MainViewModel 每秒取一次算 kbps。
 */
class NetworkStats(private val nowMs: () -> Long = { System.currentTimeMillis() }) {
    private var rx = 0L
    private var tx = 0L
    private var pingAt: Long? = null

    @Volatile var lastRttMs: Long? = null
        private set

    fun onReceived(bytes: Int) { if (bytes > 0) rx += bytes }
    fun onSent(bytes: Int) { if (bytes > 0) tx += bytes }
    fun onPingSent() { pingAt = nowMs() }
    fun onPong() { pingAt?.let { lastRttMs = nowMs() - it }; pingAt = null }

    fun drainRx(): Long = rx.also { rx = 0 }
    fun drainTx(): Long = tx.also { tx = 0 }
}
