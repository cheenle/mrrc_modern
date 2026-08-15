package com.hamradio.ft710android.Spectrum

data class SpectrumFrame(val version: Int, val wf1: IntArray, val wf2: IntArray) {
    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is SpectrumFrame) return false
        return version == other.version && wf1.contentEquals(other.wf1) && wf2.contentEquals(other.wf2)
    }
    override fun hashCode(): Int = version * 31 + wf1.contentHashCode() + wf2.contentHashCode()
}

/** 1701B = 1B version(0x01) + 850B wf1 + 850B wf2；非法帧返回 null。 */
fun parseSpectrumFrame(frame: ByteArray): SpectrumFrame? {
    if (frame.size != 1701) return null
    if (frame[0] != 0x01.toByte()) return null
    val wf1 = IntArray(850)
    val wf2 = IntArray(850)
    for (i in 0 until 850) wf1[i] = frame[i + 1].toInt() and 0xFF
    for (i in 0 until 850) wf2[i] = frame[i + 851].toInt() and 0xFF
    return SpectrumFrame(version = 1, wf1 = wf1, wf2 = wf2)
}
