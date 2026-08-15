package com.hamradio.ft710android.Network

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString

/** 单路 WebSocket 封装：文本/二进制回调和状态回调；重连策略由 ConnectionManager 负责。 */
class WebSocketConnection(
    private val client: OkHttpClient,
    val url: String,
    private val onText: (String) -> Unit,
    private val onBinary: (ByteArray) -> Unit,
    private val onStateChange: (State) -> Unit,
) {
    enum class State { Idle, Connecting, Connected, Failed }

    @Volatile private var socket: WebSocket? = null

    fun connect() {
        onStateChange(State.Connecting)
        val request = Request.Builder().url(url).build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                onStateChange(State.Connected)
            }
            override fun onMessage(webSocket: WebSocket, text: String) { onText(text) }
            override fun onMessage(webSocket: WebSocket, bytes: okio.ByteString) { onBinary(bytes.toByteArray()) }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                onStateChange(State.Failed)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onStateChange(State.Failed)
            }
        })
    }

    fun sendText(text: String): Boolean = socket?.send(text) ?: false
    fun sendBinary(data: ByteArray): Boolean = socket?.send(data.toByteString()) ?: false
    fun close(code: Int = 1000, reason: String = "") {
        socket?.close(code, reason)
        socket = null
    }
}
