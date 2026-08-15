package com.hamradio.ft710android.Network

import okhttp3.OkHttpClient
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class WebSocketConnectionTest {
    private lateinit var server: MockWebServer

    @Before fun setUp() { server = MockWebServer() }

    @After fun tearDown() { server.shutdown() }

    private fun serverWebSocket(messages: List<String>) {
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse {
                if (request.path?.startsWith("/WSradio") == true) {
                    return MockResponse().withWebSocketUpgrade(object : WebSocketListener() {
                        override fun onOpen(webSocket: WebSocket, response: Response) {
                            messages.forEach { webSocket.send(it) }
                        }
                        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                            webSocket.close(code, reason) // 对端发起 close → 回执，完成握手
                        }
                    })
                }
                return MockResponse().setResponseCode(404)
            }
        }
    }

    @Test fun `connects receives text and reports Connected`() {
        val texts = Collections.synchronizedList(mutableListOf<String>())
        val states = Collections.synchronizedList(mutableListOf<WebSocketConnection.State>())
        val latch = CountDownLatch(1)
        serverWebSocket(listOf("""{"type":"pong"}"""))
        val conn = WebSocketConnection(
            client = OkHttpClient(),
            url = server.url("/WSradio?token=t").toString(),
            onText = { texts.add(it); latch.countDown() },
            onBinary = {},
            onStateChange = { states.add(it) },
        )
        conn.connect()
        assertTrue(latch.await(3, TimeUnit.SECONDS))
        assertTrue(states.contains(WebSocketConnection.State.Connected))
        assertEquals(listOf("""{"type":"pong"}"""), texts.toList())
        conn.close()
        Thread.sleep(300) // 让 close 握手完成，否则 MockWebServer.shutdown 卡队列
    }
}
