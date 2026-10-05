package com.hamradio.ft710android.Network

import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

class CloudApiTest {
    private lateinit var server: MockWebServer
    private lateinit var api: CloudApi

    @Before fun setUp() { server = MockWebServer(); server.start(); api = CloudApi(OkHttpClient()) }
    @After fun tearDown() { server.shutdown() }

    @Test fun `state parses the full payload`() = runTest {
        server.enqueue(MockResponse().setBody(
            """{"connected":true,"callsign":"BG1SB","has_token":true,"label":"bg1sb","entry":"https://bg1sb.mrrc.vlsc.net",
                "cert":"/data/certs/server.crt","portal":"https://cloud.vlsc.net","tunnel_running":true,"tunnel_error":"",
                "cert_reload_required":false,"autoconnect":{"status":"connected","at":1759600000}}"""
        ))
        val st = (api.state(server.url("/").toString().trimEnd('/'), "t") as CloudResult.Ok).value
        assertTrue(st.connected)
        assertEquals("BG1SB", st.callsign)
        assertEquals("connected", st.autoconnect?.status)
        assertEquals("https://bg1sb.mrrc.vlsc.net", st.entry)
        val req = server.takeRequest()
        assertEquals("/api/cloud/state", req.path)
        assertEquals("ft710_auth=t", req.getHeader("Cookie"))
    }

    @Test fun `apply posts callsign contact secret`() = runTest {
        server.enqueue(MockResponse().setBody("""{"submitted":true,"status":"applied","callsign":"BG1SB"}"""))
        val r = api.apply(server.url("/").toString().trimEnd('/'), "t", "bg1sb", "a@b.c", "SECRET")
        assertTrue(r is CloudResult.Ok)
        val body = server.takeRequest().body.readUtf8()
        assertTrue(body.contains("\"callsign\":\"bg1sb"))
        assertTrue(body.contains("\"secret\":\"SECRET\""))
    }

    @Test fun `error payload becomes Err with the server message`() = runTest {
        server.enqueue(MockResponse().setResponseCode(502).setBody("""{"error":"hub 拒绝: 呼号已被占用"}"""))
        val r = api.apply(server.url("/").toString().trimEnd('/'), "t", "x", "", "")
        assertEquals("hub 拒绝: 呼号已被占用", (r as CloudResult.Err).message)
    }

    @Test fun `non-json response becomes Err naming the status`() = runTest {
        server.enqueue(MockResponse().setResponseCode(404).setBody("<html>nope</html>"))
        val r = api.state(server.url("/").toString().trimEnd('/'), "t")
        assertTrue((r as CloudResult.Err).message.contains("404"))
    }

    @Test fun `restart accepts the ack payload`() = runTest {
        server.enqueue(MockResponse().setBody("""{"restarting":true}"""))
        val r = api.restart(server.url("/").toString().trimEnd('/'), "t")
        assertTrue(r is CloudResult.Ok)
    }
}
