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
    private val connectedFlags = mutableSetOf<String>()
    private val stats = NetworkStats(nowMs)

    @Volatile var isConnected: Boolean = false; private set
    @Volatile var listenOnly: Boolean = false; private set

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
        atr = connect(baseUrl, "/WSatr1000", token, onText = {
            stats.onReceived(it.length)
            parseAtrEvent(it)?.let(onAtrEvent)
        }, onBinary = {}, onClosedCode = ::handleCloseCode)
        heartbeat?.cancel()
        heartbeat = scope.launch { while (isActive) { sendPing(); delay(2000) } }
    }

    fun stopAll() {
        heartbeat?.cancel()
        listOfNotNull(radio, audioRx, audioTx, spectrum, atr).forEach { it.close() }
        radio = null; audioRx = null; audioTx = null; spectrum = null; atr = null
        connectedFlags.clear(); updateConnected(); listenOnly = false
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
        val url = wsUrl(baseUrl, path, token)
        val conn = WebSocketConnection(
            client, url, onText, onBinary,
            onStateChange = { state ->
                if (state == WebSocketConnection.State.Connected) connectedFlags.add(path)
                else connectedFlags.remove(path)
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

    private fun updateConnected() {
        val required = if (listenOnly) setOf("/WSradio", "/WSaudioRX", "/WSspectrum")
                       else setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum")
        val all = required.all { it in connectedFlags }
        if (all != isConnected) { isConnected = all; onConnectionChange(all) }
    }

    private fun dispatch(cmd: String) {
        stats.onSent(cmd.length)
        if (sendOverride != null) { sendOverride(cmd); return }
        radio?.sendText(cmd)
    }
}
