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
data class FilterTables(
    val voice: List<Int> = emptyList(),
    val narrow: List<Int> = emptyList(),
    @SerialName("narrowModes") val narrowModes: List<String> = emptyList(),
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
    val bands: List<String> = emptyList(),
    val modes: List<String> = emptyList(),
    val memChannels: List<JsonElement?> = emptyList(),
    @SerialName("filterTables") val filterTables: FilterTables? = null,
    @SerialName("atr1000Enabled") val atr1000Enabled: Boolean = false,
    val recording: RecordingStatusDto? = null,
    val cq: CqStatusDto? = null,
    val radioModel: String? = null,
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
        val bands: List<String>,
        val modes: List<String>,
        val memChannels: List<JsonElement?>,
        val filterTables: FilterTables?,
        val atr1000Enabled: Boolean,
        val recording: RecordingStatusDto?,
        val cq: CqStatusDto?,
        val radioModel: String?,
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
                d.recording, d.cq, d.radioModel)
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
