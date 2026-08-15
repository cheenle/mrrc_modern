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
data class FullStateDto(
    val type: String = "fullState",
    val data: JsonObject = JsonObject(emptyMap()),
    val bands: List<String> = emptyList(),
    val modes: List<String> = emptyList(),
    val memChannels: List<JsonElement?> = emptyList(),
    @SerialName("filterTables") val filterTables: FilterTables? = null,
    @SerialName("atr1000Enabled") val atr1000Enabled: Boolean = false,
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
    ) : WsEvent()

    data class StateUpdate(val fields: JsonObject, val dirty: List<String>) : WsEvent()
    data class MemChannels(val channels: List<JsonElement?>) : WsEvent()
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
            WsEvent.FullState(d.data, d.bands, d.modes, d.memChannels, d.filterTables, d.atr1000Enabled)
        }.getOrElse { WsEvent.Unknown }
        "stateUpdate" -> runCatching {
            val d = json.decodeFromString<StateUpdateDto>(text)
            WsEvent.StateUpdate(d.fields, d.dirty)
        }.getOrElse { WsEvent.Unknown }
        "memChannels" -> runCatching {
            val d = json.decodeFromString<MemChannelsDto>(text)
            WsEvent.MemChannels(d.channels)
        }.getOrElse { WsEvent.Unknown }
        "error" -> runCatching {
            val d = json.decodeFromString<ServerErrorDto>(text)
            WsEvent.ErrorEvent(d.message)
        }.getOrElse { WsEvent.Unknown }
        "pong" -> WsEvent.Pong
        else -> WsEvent.Unknown
    }
}
