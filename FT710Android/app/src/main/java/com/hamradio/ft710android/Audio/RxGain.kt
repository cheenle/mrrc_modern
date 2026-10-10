package com.hamradio.ft710android.Audio

import kotlin.math.min

/** Web `_applyAfGainToAudioNode` 的纯函数版：min(10, vol/255 × boost)，TX/TUNE 时 0。 */
object RxGain {
    const val MAX = 10f

    fun target(volume: Int, boost: Float, transmitting: Boolean): Float {
        if (transmitting) return 0f
        val v = volume.coerceIn(0, 255) / 255f
        return min(MAX, v * boost)
    }
}
