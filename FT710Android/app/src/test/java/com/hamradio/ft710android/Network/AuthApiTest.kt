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

class AuthApiTest {
    private lateinit var server: MockWebServer
    private lateinit var api: AuthApi

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        api = AuthApi(OkHttpClient())
    }

    @After fun tearDown() { server.shutdown() }

    @Test fun `login success returns token`() = runTest {
        server.enqueue(MockResponse()
            .setResponseCode(200)
            .setHeader("Set-Cookie", "ft710_auth=abc123; Path=/; HttpOnly")
            .setBody("""{"ok":true,"token":"abc123"}"""))
        val res = api.login(server.url("/").toString().trimEnd('/'), "secret")
        assertTrue(res is AuthResult.Success)
        assertEquals("abc123", (res as AuthResult.Success).token)
        val recorded = server.takeRequest()
        assertEquals("/api/auth/login", recorded.path)
        assertTrue(recorded.body.readUtf8().contains("\"password\":\"secret\""))
    }

    @Test fun `login wrong password returns 401 failure`() = runTest {
        server.enqueue(MockResponse().setResponseCode(401).setBody("""{"error":"Invalid password"}"""))
        val res = api.login(server.url("/").toString().trimEnd('/'), "bad")
        assertTrue(res is AuthResult.Failure)
        assertEquals(401, (res as AuthResult.Failure).status)
    }

    @Test fun `login rate limited returns 429 failure`() = runTest {
        server.enqueue(MockResponse().setResponseCode(429).setBody("""{"error":"Too many login attempts. Please try again later."}"""))
        val res = api.login(server.url("/").toString().trimEnd('/'), "secret")
        assertTrue(res is AuthResult.Failure)
        assertEquals(429, (res as AuthResult.Failure).status)
    }

    @Test fun `logout posts and clears cookie`() = runTest {
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"ok":true}"""))
        api.logout(server.url("/").toString().trimEnd('/'), "abc123")
        val recorded = server.takeRequest()
        assertEquals("/api/auth/logout", recorded.path)
        assertEquals("ft710_auth=abc123", recorded.getHeader("Cookie"))
    }
}
