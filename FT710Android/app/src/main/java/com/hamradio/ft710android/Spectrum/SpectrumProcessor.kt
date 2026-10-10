package com.hamradio.ft710android.Spectrum

import java.util.ArrayDeque

/** 频谱行缓冲：解析 1701B 或 851B 帧（wf1 档，AD-025）→ 850 点瀑布环（120 行）。
 *
 * 只用 `wf1`：第二瀑布（wf2）在服务端两个机型上都是全零，Web 也只把它存进
 * `window._lastWf2` 备用、iOS 直接忽略，所以 `mid`/`low` 档不发它对本类没有任何影响。
 */
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
