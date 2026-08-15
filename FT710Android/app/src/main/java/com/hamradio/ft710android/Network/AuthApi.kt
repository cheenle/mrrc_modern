package com.hamradio.ft710android.Network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

sealed class AuthResult {
    data class Success(val token: String) : AuthResult()
    data class Failure(val status: Int, val message: String) : AuthResult()
}

class AuthApi(private val client: OkHttpClient) {

    suspend fun login(baseUrl: String, password: String): AuthResult = withContext(Dispatchers.IO) {
        val body = """{"password":"$password"}""".toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("$baseUrl/api/auth/login")
            .post(body)
            .build()
        runCatching { client.newCall(req).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (resp.code == 200 && text.contains("\"ok\":true")) {
                val token = regexToken.find(text)?.groupValues?.get(1) ?: ""
                AuthResult.Success(token)
            } else {
                AuthResult.Failure(resp.code, text)
            }
        } }.getOrElse { AuthResult.Failure(0, it.message ?: "network error") }
    }

    suspend fun logout(baseUrl: String, token: String) = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("$baseUrl/api/auth/logout")
            .header("Cookie", "ft710_auth=$token")
            .post("{}".toRequestBody("application/json".toMediaType()))
            .build()
        runCatching { client.newCall(req).execute().close() }
    }

    companion object {
        private val regexToken = Regex(""""token":"([^"]+)"""")

        /** 接受自签证书的 OkHttpClient（spec §8；本机/局域网自签服务端）。 */
        fun selfSignedOkHttpClient(): OkHttpClient {
            val trustAll = object : X509TrustManager {
                override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
                override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
                override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
            }
            val sslContext = SSLContext.getInstance("TLS")
            sslContext.init(null, arrayOf<TrustManager>(trustAll), SecureRandom())
            return OkHttpClient.Builder()
                .sslSocketFactory(sslContext.socketFactory, trustAll)
                .hostnameVerifier { _, _ -> true }
                .build()
        }
    }
}
