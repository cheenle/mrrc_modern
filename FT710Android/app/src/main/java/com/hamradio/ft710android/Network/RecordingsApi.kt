package com.hamradio.ft710android.Network

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File

@Serializable
data class RecordingRow(
    val name: String = "",
    @SerialName("freq_hz") val freqHz: Long = 0,
    @SerialName("started_at") val startedAt: String = "",
    val duration: Double = 0.0,
    val bytes: Long = 0,
    val recording: Boolean = false,
)

@Serializable
data class RecordingsListDto(
    val recordings: List<RecordingRow> = emptyList(),
    val count: Int = 0,
    @SerialName("total_bytes") val totalBytes: Long = 0,
)

private val recordingsJson = Json { ignoreUnknownKeys = true }

/** 纯解析（JVM 可测）：畸形响应返回空列表而不是抛异常。 */
fun parseRecordingsList(text: String): List<RecordingRow> =
    runCatching { recordingsJson.decodeFromString<RecordingsListDto>(text).recordings }
        .getOrElse { emptyList() }

/** 纯解析：列表 + count/total_bytes（L3 的合计行用）。 */
fun parseRecordingsSummary(text: String): RecordingsListDto =
    runCatching { recordingsJson.decodeFromString<RecordingsListDto>(text) }
        .getOrElse { RecordingsListDto() }

/** 录音 REST（AD-017）：列表 / 下载 / 删除。认证用登录 Cookie（与 AuthApi.logout 一致）。 */
class RecordingsApi(private val client: OkHttpClient) {
    suspend fun list(baseUrl: String, token: String): List<RecordingRow> = listSummary(baseUrl, token).recordings

    /** 列表 + 合计（/api/recordings 的 count/total_bytes 直接来自服务端）。 */
    suspend fun listSummary(baseUrl: String, token: String): RecordingsListDto = withContext(Dispatchers.IO) {
        val req = Request.Builder().url("$baseUrl/api/recordings")
            .header("Cookie", "ft710_auth=$token").get().build()
        runCatching {
            client.newCall(req).execute().use { resp ->
                if (resp.code == 200) parseRecordingsSummary(resp.body?.string().orEmpty()) else RecordingsListDto()
            }
        }.getOrElse { RecordingsListDto() }
    }

    suspend fun delete(baseUrl: String, token: String, name: String): Boolean = withContext(Dispatchers.IO) {
        val req = Request.Builder().url("$baseUrl/api/recordings/$name")
            .header("Cookie", "ft710_auth=$token").delete().build()
        runCatching { client.newCall(req).execute().use { it.code == 200 } }.getOrElse { false }
    }

    suspend fun download(baseUrl: String, token: String, name: String, dest: File): File? =
        withContext(Dispatchers.IO) {
            val req = Request.Builder().url("$baseUrl/api/recordings/$name")
                .header("Cookie", "ft710_auth=$token").get().build()
            runCatching {
                client.newCall(req).execute().use { resp ->
                    if (resp.code != 200) return@use null
                    dest.parentFile?.mkdirs()
                    resp.body?.byteStream()?.use { input -> dest.outputStream().use { input.copyTo(it) } }
                    dest
                }
            }.getOrNull()
        }
}
