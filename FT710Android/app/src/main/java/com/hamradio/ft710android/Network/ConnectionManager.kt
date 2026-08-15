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
    private val onAtrEvent: (String) -> Unit,
    private val onConnectionChange: (Boolean) -> Unit,
    private val sendOverride: ((String) -> Unit)? = null,
) {
    private var radio: WebSocketConnection? = null
    private var audioRx: WebSocketConnection? = null
    private var audioTx: WebSocketConnection? = null
    private var spectrum: WebSocketConnection? = null
    private var atr: WebSocketConnection? = null
    private var heartbeat: Job? = null
    private val connectedFlags = mutableSetOf<String>()

    @Volatile var isConnected: Boolean = false; private set

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
            onText = { onRadioEvent(parseWsEvent(it)) }, onBinary = {})
        audioRx = connect(baseUrl, "/WSaudioRX", token, onText = {}, onBinary = { onAudioRx(it) })
        audioTx = connect(baseUrl, "/WSaudioTX", token,
            onText = { onAudioTxText(it) }, onBinary = {}) // 上行二进制由 sendTxAudioBinary 发送
        spectrum = connect(baseUrl, "/WSspectrum", token, onText = {}, onBinary = { onSpectrum(it) })
        atr = connect(baseUrl, "/WSatr1000", token, onText = { onAtrEvent(it) }, onBinary = {})
        heartbeat?.cancel()
        heartbeat = scope.launch { while (isActive) { sendPing(); delay(2000) } }
    }

    fun stopAll() {
        heartbeat?.cancel()
        listOfNotNull(radio, audioRx, audioTx, spectrum, atr).forEach { it.close() }
        radio = null; audioRx = null; audioTx = null; spectrum = null; atr = null
        connectedFlags.clear(); updateConnected()
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

    fun sendPing() = dispatch(WsCommands.ping())
    fun sendMemSave(channelsJson: String) = dispatch(WsCommands.memSaveJson(channelsJson))
    fun sendTxAudioBinary(data: ByteArray) { audioTx?.sendBinary(data) }
    fun sendTxAudioText(text: String) { audioTx?.sendText(text) }

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
    ): WebSocketConnection {
        val url = wsUrl(baseUrl, path, token)
        val conn = WebSocketConnection(client, url, onText, onBinary) { state ->
            if (state == WebSocketConnection.State.Connected) connectedFlags.add(path)
            else connectedFlags.remove(path)
            updateConnected()
        }
        conn.connect()
        return conn
    }

    private fun updateConnected() {
        val all = setOf("/WSradio", "/WSaudioRX", "/WSaudioTX", "/WSspectrum").all { it in connectedFlags }
        if (all != isConnected) { isConnected = all; onConnectionChange(all) }
    }

    private fun dispatch(cmd: String) {
        if (sendOverride != null) { sendOverride(cmd); return }
        radio?.sendText(cmd)
    }
}
