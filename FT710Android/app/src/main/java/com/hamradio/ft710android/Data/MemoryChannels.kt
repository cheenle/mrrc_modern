package com.hamradio.ft710android.Data

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull

data class MemoryChannel(val freq: Long, val mode: String, val label: String = "")

object MemoryChannels {
    /** 解析服务端 channels 数组（6 槽、null 补空）。返回长度与入参一致，null 表示空槽。 */
    fun parse(list: List<JsonElement?>): List<MemoryChannel?> = list.map { el ->
        if (el is JsonNull || el == null) null
        else runCatching {
            val o = el.jsonObject
            MemoryChannel(
                freq = (o["freq"] as? JsonElement)?.jsonPrimitive?.longOrNull ?: 0L,
                mode = (o["mode"] as? JsonElement)?.jsonPrimitive?.contentOrNull ?: "",
                label = (o["label"] as? JsonElement)?.jsonPrimitive?.contentOrNull ?: "",
            )
        }.getOrNull()
    }

    /** 序列化用于 memSave（丢弃 null 空槽，键名 freq/mode/label 与服务端一致）。 */
    fun toJson(channels: List<MemoryChannel?>): String {
        val arr = buildJsonArray {
            for (c in channels) if (c != null) add(
                buildJsonObject {
                    put("freq", JsonPrimitive(c.freq))
                    put("mode", JsonPrimitive(c.mode))
                    put("label", JsonPrimitive(c.label))
                }
            )
        }
        return arr.toString()
    }
}
