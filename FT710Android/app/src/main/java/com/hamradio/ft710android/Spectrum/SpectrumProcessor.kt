package com.hamradio.ft710android.Spectrum

import java.util.ArrayDeque

/** 频谱行缓冲：解析 1701B 帧 → 850 点瀑布环（120 行）。 */
class SpectrumProcessor {
    private val rows = ArrayDeque<IntArray>()
    @Volatile private var latest: IntArray = IntArray(850)

    val waterfall: List<IntArray> get() = synchronized(rows) { rows.toList() }
    val fft: IntArray get() = latest

    fun onFrame(frame: ByteArray) {
        val sf = parseSpectrumFrame(frame) ?: return
        synchronized(rows) {
            rows.addLast(sf.wf1)
            if (rows.size > MAX_ROWS) rows.removeFirst()
        }
        latest = sf.wf1
    }

    companion object { const val MAX_ROWS = 120 }
}
