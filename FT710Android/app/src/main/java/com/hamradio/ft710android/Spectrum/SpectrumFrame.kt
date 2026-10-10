package com.hamradio.ft710android.Spectrum

/**
 * 一帧频谱。[wf2] 在 851B 短帧（服务端的 `wf1` 档位，AD-025）时为 null ——
 * 它本来就没被发过来，用全零数组冒充会让"第二瀑布存在"这件事看起来是真的。
 */
data class SpectrumFrame(val version: Int, val wf1: IntArray, val wf2: IntArray?) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is SpectrumFrame) return false
        return version == other.version && wf1.contentEquals(other.wf1) && wf2.contentEquals(other.wf2)
    }
    override fun hashCode(): Int = version * 31 + wf1.contentHashCode() + (wf2?.contentHashCode() ?: 0)
}

/**
 * 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；851B = 1B version(0x01) + 850B wf1。
 * 851B 是服务端 `mid`/`low` 档发的短帧（它是满帧的 `full[:851]` 切片，所以 wf1 逐元素相同）。
 * 其它长度一律返回 null：半截帧被当成有效数据会画进瀑布，而多出来的字节被默默忽略
 * 则会掩盖服务端的格式漂移。非法帧返回 null。
 */
fun parseSpectrumFrame(frame: ByteArray): SpectrumFrame? {
    if (frame.size != 1701 && frame.size != 851) return null
    if (frame[0] != 0x01.toByte()) return null
    val wf1 = IntArray(850)
    for (i in 0 until 850) wf1[i] = frame[i + 1].toInt() and 0xFF
    val wf2 = if (frame.size == 1701) {
        IntArray(850).also { for (i in 0 until 850) it[i] = frame[i + 851].toInt() and 0xFF }
    } else {
        null
    }
    return SpectrumFrame(version = 1, wf1 = wf1, wf2 = wf2)
}
