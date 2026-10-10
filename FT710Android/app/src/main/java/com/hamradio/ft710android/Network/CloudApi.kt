package com.hamradio.ft710android.Network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

private val cloudJson = Json { ignoreUnknownKeys = true }

@Serializable
data class CloudAutoDto(val status: String = "idle", val at: Long = 0, val error: String? = null)

/**
 * Cloud Hub 端点的响应（server.py:4317+）。state 与 refresh/apply 的连接结果同形，
 * 未用到的字段全部有默认值；`error` 用于把服务端原文带回界面。
 */
@Serializable
data class CloudStateDto(
    val connected: Boolean = false,
    val callsign: String = "",
    @SerialName("has_token") val hasToken: Boolean = false,
    val label: String = "",
    val entry: String = "",
    val cert: String = "",
    val portal: String = "",
    @SerialName("tunnel_running") val tunnelRunning: Boolean = false,
    @SerialName("tunnel_error") val tunnelError: String = "",
    @SerialName("cert_reload_required") val certReloadRequired: Boolean = false,
    val autoconnect: CloudAutoDto? = null,
    val status: String = "",
    val submitted: Boolean = false,
    val restarting: Boolean = false,
    val error: String? = null,
)

sealed class CloudResult<out T> {
    data class Ok<T>(val value: T) : CloudResult<T>()
    data class Err(val message: String) : CloudResult<Nothing>()
}

@Serializable
private data class CloudApplyBody(val callsign: String, val contact: String, val secret: String)

/** Cloud Hub REST（认证 = 登录 Cookie，与 RecordingsApi 一致）。 */
class CloudApi(private val client: OkHttpClient) {
    suspend fun state(base: String, token: String): CloudResult<CloudStateDto> =
        execute(request("$base/api/cloud/state", token))

    suspend fun refresh(base: String, token: String): CloudResult<CloudStateDto> =
        execute(request("$base/api/cloud/refresh", token, "{}"))

    suspend fun restart(base: String, token: String): CloudResult<CloudStateDto> =
        execute(request("$base/api/cloud/restart", token, "{}"))

    suspend fun apply(
        base: String,
        token: String,
        callsign: String,
        contact: String,
        secret: String,
    ): CloudResult<CloudStateDto> =
        execute(request("$base/api/cloud/apply", token,
            cloudJson.encodeToString(CloudApplyBody(callsign, contact, secret))))

    private fun request(url: String, token: String, body: String? = null): Request {
        val type = "application/json".toMediaType()
        return Request.Builder().url(url).header("Cookie", "ft710_auth=$token")
            .apply { if (body != null) post(body.toRequestBody(type)) }
            .build()
    }

    private suspend fun execute(req: Request): CloudResult<CloudStateDto> = withContext(Dispatchers.IO) {
        runCatching {
            client.newCall(req).execute().use { resp ->
                val text = resp.body?.string().orEmpty()
                val dto = runCatching { cloudJson.decodeFromString<CloudStateDto>(text) }.getOrNull()
                when {
                    dto?.error != null -> CloudResult.Err(dto.error!!)
                    dto == null -> CloudResult.Err("接口 ${req.url.encodedPath} 返回了非 JSON（HTTP ${resp.code}）")
                    !resp.isSuccessful -> CloudResult.Err("HTTP ${resp.code}")
                    else -> CloudResult.Ok(dto)
                }
            }
        }.getOrElse { CloudResult.Err(it.message ?: "网络错误") }
    }
}
