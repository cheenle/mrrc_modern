package com.hamradio.ft710android.Network

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

private val json = Json { ignoreUnknownKeys = true }

@Serializable
data class BandDto(
    val name: String = "",
    val start: Long = 0,
    val end: Long = 0,
    @SerialName("default_freq") val defaultFreq: Long = 0,
)

@Serializable
data class ScopeSpanDto(val name: String = "", val freq: Long = 0)

/** 服务端 capabilities（backends/base.py:RadioCapabilities.to_dict）的本轮消费子集。 */
@Serializable
data class CapabilitiesDto(
    @SerialName("model_name") val modelName: String = "ft710",
    @SerialName("display_name") val displayName: String = "Yaesu FT-710",
    val verified: Boolean = true,
    @SerialName("tx_gated") val txGated: Boolean = false,
    @SerialName("has_atu") val hasAtu: Boolean = true,
    @SerialName("has_auto_notch") val hasAutoNotch: Boolean = true,
    @SerialName("has_vd_id_meters") val hasVdIdMeters: Boolean = true,
    @SerialName("filter_model") val filterModel: String = "width_table",
    @SerialName("att_steps") val attSteps: List<Int> = emptyList(),
    @SerialName("preamp_steps") val preampSteps: List<String> = emptyList(),
    @SerialName("scope_type") val scopeType: String = "ft4222",
    @SerialName("scope_spans") val scopeSpans: Map<String, ScopeSpanDto> = emptyMap(),
    @SerialName("scope_speeds") val scopeSpeeds: List<String> = emptyList(),
    @SerialName("audio_gain_boost") val audioGainBoost: Double = 10.0,
)

@Serializable
data class FilterTables(
    val voice: List<List<Int>> = emptyList(),
    val narrow: List<List<Int>> = emptyList(),
    @SerialName("narrowModes") val narrowModes: List<String> = emptyList(),
    val model: String? = null,
    val filDefaults: Map<String, List<Int>> = emptyMap(),
)

@Serializable
data class RecordingStatusDto(
    val recording: Boolean = false,
    @SerialName("freq_hz") val freqHz: Long = 0,
    @SerialName("started_at") val startedAt: String? = null,
    val duration: Double = 0.0,
    val name: String? = null,
    val bytes: Long = 0,
    val dropped: Int = 0,
)

@Serializable
data class CqStatusDto(
    val state: String = "idle",
    @SerialName("duration_s") val durationS: Double = 0.0,
    @SerialName("elapsed_s") val elapsedS: Double = 0.0,
    @SerialName("frames_total") val framesTotal: Int = 0,
    @SerialName("frames_sent") val framesSent: Int = 0,
    @SerialName("started_by") val startedBy: String? = null,
    val reason: String? = null,
    val ready: Boolean = false,
)

@Serializable
data class FullStateDto(
    val type: String = "fullState",
    val data: JsonObject = JsonObject(emptyMap()),
    val bands: List<BandDto> = emptyList(),
    val modes: List<String> = emptyList(),
    val memChannels: List<JsonElement?> = emptyList(),
    @SerialName("filterTables") val filterTables: FilterTables? = null,
    @SerialName("atr1000Enabled") val atr1000Enabled: Boolean = false,
    val recording: RecordingStatusDto? = null,
    val cq: CqStatusDto? = null,
    val radioModel: String? = null,
    val radioDisplayName: String? = null,
    val capabilities: CapabilitiesDto? = null,
)

@Serializable
data class StateUpdateDto(
    val type: String = "stateUpdate",
    val fields: JsonObject = JsonObject(emptyMap()),
    val dirty: List<String> = emptyList(),
)

@Serializable
data class MemChannelsDto(val type: String = "memChannels", val channels: List<JsonElement?> = emptyList())

@Serializable
data class RecordingStateDto(val type: String = "recordingState", val recording: RecordingStatusDto = RecordingStatusDto())

@Serializable
data class CqStateDto(val type: String = "cqState", val cq: CqStatusDto = CqStatusDto())

@Serializable
data class ServerErrorDto(val type: String = "error", val message: String = "")

@Serializable
data class PongDto(val type: String = "pong")

sealed class WsEvent {
    data class FullState(
        val data: JsonObject,
        val bands: List<BandDto>,
        val modes: List<String>,
        val memChannels: List<JsonElement?>,
        val filterTables: FilterTables?,
        val atr1000Enabled: Boolean,
        val recording: RecordingStatusDto?,
        val cq: CqStatusDto?,
        val radioModel: String?,
        val radioDisplayName: String?,
        val capabilities: CapabilitiesDto?,
    ) : WsEvent()

    data class StateUpdate(val fields: JsonObject, val dirty: List<String>) : WsEvent()
    data class MemChannels(val channels: List<JsonElement?>) : WsEvent()
    data class RecordingState(val status: RecordingStatusDto) : WsEvent()
    data class CqState(val status: CqStatusDto) : WsEvent()
    data class ErrorEvent(val message: String) : WsEvent()
    object Pong : WsEvent()
    object Unknown : WsEvent()
}

fun parseWsEvent(text: String): WsEvent {
    val root = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull() ?: return WsEvent.Unknown
    val type = (root["type"] as? JsonElement)?.jsonPrimitive?.contentOrNull ?: return WsEvent.Unknown
    return when (type) {
        "fullState" -> runCatching {
            val d = json.decodeFromString<FullStateDto>(text)
            WsEvent.FullState(d.data, d.bands, d.modes, d.memChannels, d.filterTables, d.atr1000Enabled,
                d.recording, d.cq, d.radioModel, d.radioDisplayName, d.capabilities)
        }.getOrElse { WsEvent.Unknown }
        "stateUpdate" -> runCatching {
            val d = json.decodeFromString<StateUpdateDto>(text)
            WsEvent.StateUpdate(d.fields, d.dirty)
        }.getOrElse { WsEvent.Unknown }
        "memChannels" -> runCatching {
            val d = json.decodeFromString<MemChannelsDto>(text)
            WsEvent.MemChannels(d.channels)
        }.getOrElse { WsEvent.Unknown }
        "recordingState" -> runCatching {
            WsEvent.RecordingState(json.decodeFromString<RecordingStateDto>(text).recording)
        }.getOrElse { WsEvent.Unknown }
        "cqState" -> runCatching {
            WsEvent.CqState(json.decodeFromString<CqStateDto>(text).cq)
        }.getOrElse { WsEvent.Unknown }
        "error" -> runCatching {
            val d = json.decodeFromString<ServerErrorDto>(text)
            WsEvent.ErrorEvent(d.message)
        }.getOrElse { WsEvent.Unknown }
        "pong" -> WsEvent.Pong
        else -> WsEvent.Unknown
    }
}

// ── ATR1000（/WSatr1000）────────────────────────────────────────────

@Serializable
data class AtrStateDto(
    val connected: Boolean = false,
    val power: Double = 0.0,
    val swr: Double = 0.0,
    val sw: Int = 0,
    val ind: Int = 0,
    val cap: Int = 0,
    @SerialName("ind_uh") val indUh: Double = 0.0,
    @SerialName("cap_pf") val capPf: Double = 0.0,
    val tuning: Boolean = false,
    val tx: Boolean = false,
    val freq: Long = 0,
    @SerialName("last_update") val lastUpdate: Double = 0.0,
)

@Serializable
data class AtrTuneResultDto(
    val phase: String = "",
    @SerialName("swr_before") val swrBefore: Double? = null,
    @SerialName("swr_after") val swrAfter: Double? = null,
    val message: String? = null,
    val auto: Boolean = false,
)

sealed class AtrEvent {
    data class State(val s: AtrStateDto) : AtrEvent()
    data class TuneResult(val r: AtrTuneResultDto) : AtrEvent()
    data class Error(val message: String) : AtrEvent()
}

/** /WSatr1000 文本 → 事件；非 ATR 消息返回 null。 */
fun parseAtrEvent(text: String): AtrEvent? {
    val root = runCatching { json.parseToJsonElement(text).jsonObject }.getOrNull() ?: return null
    val type = root["type"]?.jsonPrimitive?.contentOrNull ?: return null
    return when (type) {
        "atrState" -> runCatching { AtrEvent.State(json.decodeFromString<AtrStateDto>(text)) }.getOrNull()
        "atrTuneResult" -> runCatching { AtrEvent.TuneResult(json.decodeFromString<AtrTuneResultDto>(text)) }.getOrNull()
        "error" -> AtrEvent.Error(
            runCatching { json.decodeFromString<ServerErrorDto>(text).message }.getOrElse { "ATR error" }
        )
        else -> null
    }
}

/** Web `tuneResultText` 的中文文案（跳过/成功/回滚/自动六阶段/错误回退）。 */
object AtrText {
    fun result(r: AtrTuneResultDto): String = when (r.phase) {
        "skipped" -> "ATR: SWR ${r.swrBefore ?: "?"} 已达标，无需调谐"
        "success" -> "ATR 调谐完成: SWR ${r.swrBefore} → ${r.swrAfter}"
        "rollback" -> "ATR 调谐无改善，已回滚 (SWR ${r.swrBefore})"
        "auto_success" -> "ATR 自动调谐完成: SWR ${r.swrBefore} → ${r.swrAfter}"
        "auto_no_improve" -> "ATR 自动调谐无改善 (SWR ${r.swrBefore} → ${r.swrAfter})"
        "auto_timeout" -> "ATR 自动调谐超时 (SWR ${r.swrBefore})"
        "auto_aborted" -> "ATR 自动调谐中断: ${r.message ?: "天调断开"}"
        "auto_giveup" -> "ATR 连续 3 次无改善，已放弃该频点自动调谐"
        else -> "ATR 调谐失败: ${r.message ?: r.phase}"
    }
}
