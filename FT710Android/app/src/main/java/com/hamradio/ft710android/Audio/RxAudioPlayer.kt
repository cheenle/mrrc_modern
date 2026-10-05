package com.hamradio.ft710android.Audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import com.hamradio.ft710android.ViewModel.MainViewModel
import java.util.ArrayDeque
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * RX 播放：Opus/PCM 帧 → AudioTrack，带抖动缓冲与静音填充。
 * onFrame 由网络线程调用（解码入队）；播放循环在独立线程消费写 AudioTrack。
 */
class RxAudioPlayer : MainViewModel.RxPlayerLike {
    private val decoder = OpusBridge.decoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS)
    private val jitter = ArrayDeque<ShortArray>()
    private val framesPerTarget = max(1, TARGET_PREBUFFER_MS / FRAME_MS)

    @Volatile private var running = false
    @Volatile var rms = 0f; private set

    // 本机播放音量（web 🔊 Vol 语义）：0..255，再乘 capabilities.audio_gain_boost（FT-710 = 10×），
    // TX/TUNE 期间置 0 杀死自噪回环（web AUDIO_TX_DIM_FACTOR = 0）。
    @Volatile private var volume = 128
    @Volatile private var boost = 10f
    @Volatile private var transmitting = false

    private var track: AudioTrack? = null
    private var thread: Thread? = null

    override fun start() {
        if (running) return   // 幂等：重连/重复回调不得重建 AudioTrack
        running = true
        val minBuf = AudioTrack.getMinBufferSize(
            OpusBridge.SAMPLE_RATE, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        track = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_MEDIA)
                .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC).build())
            .setAudioFormat(AudioFormat.Builder()
                .setSampleRate(OpusBridge.SAMPLE_RATE)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setChannelMask(AudioFormat.CHANNEL_OUT_MONO).build())
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setBufferSizeInBytes(max(minBuf, OpusBridge.FRAME_SAMPLES * 2 * framesPerTarget))
            .build()
        track?.play()
        thread = Thread(::playLoop, "rx-player").apply { isDaemon = true; start() }
    }

    /** 本机音量（0..255，DataStore afVol）。 */
    override fun setVolume(v: Int) { volume = v.coerceIn(0, 255) }

    /** 每种电台的 RX 播放增益（capabilities.audio_gain_boost；FT-710 = 10）。 */
    override fun setBoost(b: Float) { boost = b }

    /** TX/TUNE 时静音 RX 播放（防自噪回环）。 */
    override fun setTransmitting(t: Boolean) { transmitting = t }

    /** 抖动缓冲深度（毫秒），状态行 J 值。 */
    override val bufferMs: Int get() = synchronized(jitter) { jitter.size } * FRAME_MS

    /** WS 帧入口：1B tag + payload。tag 0x01 Opus 解码，0x00 PCM 直通。 */
    override fun onFrame(frame: ByteArray) {
        if (!running) return
        val tag = frame[0].toInt() and 0xFF
        val payload = frame.copyOfRange(1, frame.size)
        val pcm = ShortArray(OpusBridge.FRAME_SAMPLES)
        val samples = when (tag) {
            0x01 -> OpusBridge.decoderDecode(decoder, payload, payload.size, pcm)
            0x00 -> {
                val n = min(pcm.size, payload.size / 2)
                for (i in 0 until n) {
                    pcm[i] = ((payload[i * 2 + 1].toInt() shl 8) or (payload[i * 2].toInt() and 0xFF)).toShort()
                }
                n
            }
            else -> 0
        }
        if (samples > 0) synchronized(jitter) { jitter.addLast(pcm.copyOf(samples)) }
    }

    private fun playLoop() {
        val silence = ShortArray(OpusBridge.FRAME_SAMPLES)
        while (running) {
            val buf: ShortArray = synchronized(jitter) {
                if (jitter.isNotEmpty()) jitter.removeFirst() else silence
            }
            val g = RxGain.target(volume, boost, transmitting)
            val out = when {
                g == 0f -> silence
                g == 1f -> buf
                else -> ShortArray(buf.size) {
                    (buf[it] * g).roundToInt().coerceIn(-32768, 32767).toShort()
                }
            }
            track?.write(out, 0, out.size)
            var sum = 0L
            for (s in buf) sum += s.toLong() * s
            rms = sqrt((sum / buf.size).toDouble() / (32768.0 * 32768.0)).toFloat()
        }
    }

    override fun stop() {
        if (!running && track == null && thread == null) return   // 幂等
        running = false
        thread?.join(500)
        thread = null
        synchronized(jitter) { jitter.clear() }
        track?.pause(); track?.flush(); track?.release()
        track = null
    }

    fun release() { stop(); OpusBridge.destroyDecoder(decoder) }

    companion object {
        const val TARGET_PREBUFFER_MS = 180
        const val FRAME_MS = 20
    }
}
