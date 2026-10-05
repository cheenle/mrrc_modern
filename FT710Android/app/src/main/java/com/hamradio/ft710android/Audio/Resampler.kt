package com.hamradio.ft710android.Audio

import kotlin.math.roundToInt
import kotlin.math.roundToLong

/** 44.1k↔48k 线性插值（与 `audio_resample.py` 同算法）；882@44.1k = 960@48k = 20ms。 */
object Resampler {
    fun resample(input: ShortArray, inRate: Int, outRate: Int): ShortArray {
        if (input.isEmpty() || inRate <= 0 || outRate <= 0) return ShortArray(0)
        val outLen = (input.size.toLong() * outRate / inRate.toDouble()).roundToLong().toInt().coerceAtLeast(1)
        val out = ShortArray(outLen)
        for (i in 0 until outLen) {
            val t = i.toDouble() * inRate / outRate
            val i0 = t.toInt().coerceIn(0, input.size - 1)
            val i1 = (i0 + 1).coerceAtMost(input.size - 1)
            val v = input[i0] * (1.0 - (t - i0)) + input[i1] * (t - i0)
            out[i] = v.roundToInt().coerceIn(-32768, 32767).toShort()
        }
        return out
    }

    fun resample882To960(input: ShortArray): ShortArray = resample(input, 44100, 48000)
}
