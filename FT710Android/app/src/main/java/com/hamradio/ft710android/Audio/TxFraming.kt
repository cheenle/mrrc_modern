package com.hamradio.ft710android.Audio

import kotlin.math.roundToInt

/** TX 20ms 帧的纯数学：帧长与 Mic Vol 缩放（vol 100 = 原始；上限 400 = 4×，手机麦补偿）。 */
object TxFraming {
    const val FRAME_48 = 960
    const val FRAME_44 = 882

    fun frameSamples(rate: Int): Int = if (rate == 44100) FRAME_44 else FRAME_48

    fun applyMicVol(samples: ShortArray, vol: Int): ShortArray {
        if (vol == 100) return samples
        val f = vol.coerceIn(0, 400) / 100f
        return ShortArray(samples.size) {
            (samples[it] * f).roundToInt().coerceIn(-32768, 32767).toShort()
        }
    }
}
