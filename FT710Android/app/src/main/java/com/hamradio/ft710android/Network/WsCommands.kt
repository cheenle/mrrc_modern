package com.hamradio.ft710android.Network

/**
 * /WSradio 上行命令。field 名与 server.py:_execute_set_command 逐字对齐。
 * 用字符串拼接而非序列化，保证值类型（数字/字符串/布尔）与服务端 switch 的判定完全一致。
 */
object WsCommands {
    fun setNumber(field: String, value: Number): String =
        """{"type":"set","field":"$field","value":$value}"""

    fun setString(field: String, value: String): String =
        """{"type":"set","field":"$field","value":"$value"}"""

    fun setBool(field: String, value: Boolean): String =
        """{"type":"set","field":"$field","value":$value}"""

    fun ping(): String = """{"type":"ping"}"""

    fun getFullState(): String = """{"type":"get","field":"fullState"}"""

    fun memSaveJson(channelsJson: String): String =
        """{"type":"memSave","channels":$channelsJson}"""
}
