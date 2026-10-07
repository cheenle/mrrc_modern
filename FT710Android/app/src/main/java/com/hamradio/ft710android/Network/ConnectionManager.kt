package com.hamradio.ft710android.Network

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/** 4 路 (+可选 ATR1000) WS 编排：认证 token 注入、心跳、命令路由、连接状态聚合。 */
class ConnectionManager(
    private val client: OkHttpClient,
    private val scope: CoroutineScope,
    private val onRadioEvent: (WsEvent) -> Unit,
    private val onAudioRx: (ByteArray) -> Unit,
    private val onSpectrum: (ByteArray) -> Unit,
    private val onAudioTxText: (String) -> Unit,
    private val onAtrEvent: (AtrEvent) -> Unit,
    private val onConnectionChange: (Boolean) -> Unit,
    private val onAudioRxChange: (Boolean) -> Unit = {},
    private val onListenOnly: () -> Unit = {},
    private val sendOverride: ((String) -> Unit)? = null,
    nowMs: () -> Long = { System.currentTimeMillis() },
) {
    private var radio: WebSocketConnection? = null
    private var audioRx: WebSocketConnection? = null
    private var audioTx: WebSocketConnection? = null
    private var spectrum: WebSocketConnection? = null
    private var atr: WebSocketConnection? = null
    private var heartbeat: Job? = null

    /** 服务端是否启用了 ATR-1000（来自 fullState.atr1000Enabled）。未启用就**不连** /WSatr1000。 */
    private var atrEnabled = false
    private val openedPathsLog = mutableListOf<String>()

    /**
     * 本次会话已请求打开的 WS 路径（诊断 + 测试断言用）。
     * ATR 是可选选件：没配 `MRRC_ATR1000_HOST` 的部署，这里**不该**出现 `/WSatr1000`
     * （否则每次连接都白握手一次、被服务端以 4000 立即关闭）。
     */
    val openedPaths: List<String> get() = synchronized(openedPathsLog) { openedPathsLog.toList() }
    private val connectedFlags = ChannelFlags()
    private val stats = NetworkStats(nowMs)

    @Volatile var isConnected: Boolean = false; private set
    @Volatile var listenOnly: Boolean = false; private set

    /** 控制通道在线（PTT 的唯一闸门：不需要等音频通道）。 */
    val isRadioConnected: Boolean get() = connectedFlags.has("/WSradio")

    /** 通道状态摘要（诊断行用）：R=radio A=audioRX T=audioTX S=spectrum。 */
    fun channelsSummary(): String {
        fun m(path: String) = if (connectedFlags.has(path)) "+" else "-"
        return "R${m("/WSradio")} A${m("/WSaudioRX")} T${m("/WSaudioTX")} S${m("/WSspectrum")}"
    }

    private var _baseUrl: String? = null
    private var _token: String? = null

    companion object {
        fun wsUrl(baseUrl: String, path: String, token: String): String {
            val scheme = if (baseUrl.startsWith("https")) "wss" else "ws"
            val host = baseUrl.removePrefix("https://").removePrefix("http://")
            return "$scheme://$host$path?token=$token"
        }
    }

    fun start(baseUrl: String, token: String) {
        _baseUrl = baseUrl; _token = token
        stopAll()
        radio = connect(baseUrl, "/WSradio", token,
            onText = { stats.onReceived(it.length); onRadioEvent(parseWsEvent(it)) }, onBinary = {})
        audioRx = connect(baseUrl, "/WSaudioRX", token, onText = { stats.onReceived(it.length) },
            onBinary = { stats.onReceived(it.size); onAudioRx(it) })
        audioTx = connect(baseUrl, "/WSaudioTX", token,
            onText = { stats.onReceived(it.length); onAudioTxText(it) }, onBinary = {}, onClosedCode = ::handleCloseCode) // 上行二进制由 sendTxAudioBinary 发送
        spectrum = connect(baseUrl, "/WSspectrum", token, onText = { stats.onReceived(it.length) },
            onBinary = { stats.onReceived(it.size); onSpectrum(it) })
        // ATR-1000 是**可选选件**：只有服务端 fullState 说启用了才连（见 setAtrEnabled）。
        // 这里仅在重连且上一次已知启用时立即补连，冷启动时等 fullState。
        if (atrEnabled) connectAtr()
        heartbeat?.cancel()
        heartbeat = scope.launch { while (isActive) { sendPing(); delay(2000) } }
    }

    fun stopAll() {
        heartbeat?.cancel()
        listOfNotNull(radio, audioRx, audioTx, spectrum, atr).forEach { it.close() }
        radio = null; audioRx = null; audioTx = null; spectrum = null; atr = null
        connectedFlags.clear(); updateConnected(); listenOnly = false
        atrEnabled = false
        synchronized(openedPathsLog) { openedPathsLog.clear() }
    }

    /**
     * 服务端 fullState 到达后由 VM 调用：ATR-1000 启用才开 `/WSatr1000`，禁用则关掉。
     *
     * 未配置的部署服务端会 `accept` 后立刻以 **4000 "ATR1000 disabled"** 关闭
     * （只读会话是 4003），所以"不连"才是正确行为：省一次握手，也不制造无意义的关闭事件。
     */
    @Synchronized
    fun setAtrEnabled(enabled: Boolean) {
        if (atrEnabled == enabled) return
        atrEnabled = enabled
        if (enabled) {
            if (atr == null) connectAtr()
        } else {
            atr?.close()
            atr = null
        }
    }

    /** ATR 是否已启用（fullState 给的，不是"是否连上"）。 */
    val isAtrEnabled: Boolean get() = synchronized(this) { atrEnabled }

    private fun connectAtr() {
        val b = _baseUrl ?: return
        val t = _token ?: return
        if (b.isEmpty() || t.isEmpty()) return
        atr = connect(b, "/WSatr1000", t, onText = {
            stats.onReceived(it.length)
            parseAtrEvent(it)?.let(onAtrEvent)
        }, onBinary = {}, onClosedCode = ::handleCloseCode)
    }

    fun sendSet(field: String, value: Any) {
        val cmd = when (value) {
            is Boolean -> WsCommands.setBool(field, value)
            is String -> WsCommands.setString(field, value)
            is Number -> WsCommands.setNumber(field, value)
            else -> WsCommands.setNumber(field, value.toString().toLongOrNull() ?: 0L)
        }
        dispatch(cmd)
    }

    fun sendPing() { stats.onPingSent(); dispatch(WsCommands.ping()) }
    fun sendHeartbeat() = dispatch(WsCommands.txhb())
    fun sendMemSave(channelsJson: String) = dispatch(WsCommands.memSaveJson(channelsJson))
    fun sendTxAudioBinary(data: ByteArray) { stats.onSent(data.size); audioTx?.sendBinary(data) }
    fun sendTxAudioText(text: String) { stats.onSent(text.length); audioTx?.sendText(text) }

    /** ATR 手动调谐（服务端自带 TX2 载波 + 比对回滚）；未连接时返回 false。 */
    fun sendAtrTune(): Boolean {
        val cmd = """{"type":"atrTune"}"""
        stats.onSent(cmd.length)
        return atr?.sendText(cmd) ?: false
    }

    /** 收到 /WSradio 的 pong（由 MainViewModel 在 WsEvent.Pong 时调用）。 */
    fun onPong() = stats.onPong()

    fun lastRttMs(): Long? = stats.lastRttMs
    fun drainRx(): Long = stats.drainRx()
    fun drainTx(): Long = stats.drainTx()

    /** M7 连接开关的"关"：停全部通道（重连由 reconnectAll）。 */
    fun disconnect() = stopAll()

    fun reconnectAll() {
        val token = _token ?: return
        val base = _baseUrl ?: return
        stopAll(); start(base, token)
    }

    private fun connect(
        baseUrl: String,
        path: String,
        token: String,
        onText: (String) -> Unit,
        onBinary: (ByteArray) -> Unit,
        onClosedCode: (Int) -> Unit = {},
    ): WebSocketConnection {
        // 记录"本次会话请求打开过哪些通道"：诊断与测试都靠它
        // （ATR 是可选选件，未启用时这里不该出现 /WSatr1000）
        synchronized(openedPathsLog) { openedPathsLog.add(path) }
        val url = wsUrl(baseUrl, path, token)
        val conn = WebSocketConnection(
            client, url, onText, onBinary,
            onStateChange = { state ->
                if (state == WebSocketConnection.State.Connected) connectedFlags.add(path)
                else connectedFlags.remove(path)
                if (path == "/WSaudioRX") onAudioRxChange(connectedFlags.has(path))
                updateConnected()
            },
            onClosedCode = onClosedCode,
        )
        conn.connect()
        return conn
    }

    private fun handleCloseCode(code: Int) {
        if (code == 4003 && !listenOnly) {
            listenOnly = true
            connectedFlags.remove("/WSaudioTX")
            updateConnected()
            onListenOnly()
        }
    }

    @Synchronized
    private fun updateConnected() {
        val required = if (listenOnly) setOf("/WSradio", "/WSaudioRX", "/WSspectrum")
                       else setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum")
        val all = connectedFlags.hasAll(required)
        if (all != isConnected) { isConnected = all; onConnectionChange(all) }
    }

    private fun dispatch(cmd: String) {
        stats.onSent(cmd.length)
        if (sendOverride != null) { sendOverride(cmd); return }
        radio?.sendText(cmd)
    }
}
