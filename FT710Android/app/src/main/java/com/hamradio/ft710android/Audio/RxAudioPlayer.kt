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
 * RX 播放：Opus/PCM 帧 → AudioTrack。
 *
 * 抖动缓冲是**时间水位**的（对齐 web `rx_worklet_processor.js`）：
 *  - 冷启动攒到 PREBUFFER_MS 才出声，欠载后只攒 RECOVERY_MS 就恢复（滞后，避免长停顿）；
 *  - 队列超过 MAX_MS 丢最旧帧 → **延迟有界**（早期无上限队列在卡过一次后永远落后，真机表现为"跑一段时间比 Web 慢好几秒"）；
 *  - 进入 TX 时 flush：服务端 TX 期间不发帧，旧帧不该在 unkey 后播放。
 * onFrame 由网络线程调用（解码入队）；播放循环在独立线程消费写 AudioTrack。
 */
class RxAudioPlayer : MainViewModel.RxPlayerLike {
    private val decoder = OpusBridge.decoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS)
    private val jitter = ArrayDeque<ShortArray>()
    private var queuedSamples = 0
    private var priming = true
    private var gateMs = PREBUFFER_MS
    private val samplesPerMs = OpusBridge.SAMPLE_RATE / 1000

    @Volatile private var running = false
    @Volatile var rms = 0f; private set

    // 本机播放音量（web 🔊 Vol 语义）：0..255，再乘 capabilities.audio_gain_boost（FT-710 = 10×），
    // TX/TUNE 期间置 0 杀死自噪回环（web AUDIO_TX_DIM_FACTOR = 0）。
    @Volatile private var volume = 128
    @Volatile private var boost = 10f
    @Volatile private var transmitting = false

    // 设备侧诊断计数（2026-10-05 真机无声事故）：把音频链路每段边界暴露给状态行
    @Volatile var framesIn = 0L; private set
    @Volatile var samplesOut = 0L; private set
    @Volatile var decodeErrors = 0L; private set
    @Volatile var trackWrites = 0L; private set
    @Volatile var trackWriteErrors = 0L; private set
    @Volatile var drops = 0L; private set
    @Volatile var underruns = 0L; private set

    /**
     * 一行设备侧诊断：A=播放器开关，F=收到的音频帧，D=解码样本，J=抖动缓冲 ms，
     * G=当前增益，T=AudioTrack 状态，W/E=写成功/失败，Dr=超上限丢帧，Un=欠载。
     */
    override fun stats(): String {
        val gain = "%.2f".format(java.util.Locale.US, RxGain.target(volume, boost, transmitting))
        return "A:${if (running) "on" else "off"} F:$framesIn D:$samplesOut J:$bufferMs " +
            "G:$gain T:${track?.playState ?: -1} W:$trackWrites E:$trackWriteErrors " +
            "Dr:$drops Un:$underruns"
    }

    /** 抖动缓冲深度（毫秒，按样本数），状态行 J 值。 */
    override val bufferMs: Int get() = synchronized(jitter) { queuedSamples } / samplesPerMs

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
            // 缓冲给到水位以上（约 12 帧 ≈ 240ms），避免写入侧把水位饿空
            .setBufferSizeInBytes(max(minBuf, OpusBridge.FRAME_SAMPLES * 2 * (PREBUFFER_MS / FRAME_MS + 2)))
            .build()
        track?.play()
        thread = Thread(::playLoop, "rx-player").apply { isDaemon = true; start() }
    }

    /** 本机音量（0..255，DataStore afVol）。 */
    override fun setVolume(v: Int) { volume = v.coerceIn(0, 255) }

    /** 每种电台的 RX 播放增益（capabilities.audio_gain_boost；FT-710 = 10）。 */
    override fun setBoost(b: Float) { boost = b }

    /** TX/TUNE 时静音 RX 播放（防自噪回环）；进入 TX 清空队列，避免 unkey 后播放旧音频。 */
    override fun setTransmitting(t: Boolean) {
        transmitting = t
        if (t) flush()
    }

    /** Web 的 flush/reset：清队列、回到冷启动水位。 */
    private fun flush() {
        synchronized(jitter) { jitter.clear(); queuedSamples = 0 }
        priming = true
        gateMs = PREBUFFER_MS
    }

    /** WS 帧入口：1B tag + payload。tag 0x01 Opus 解码，0x00 PCM 直通。 */
    override fun onFrame(frame: ByteArray) {
        if (!running || frame.isEmpty()) return
        framesIn++
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
        if (samples > 0) {
            samplesOut += samples
            val decoded = pcm.copyOf(samples)
            synchronized(jitter) {
                jitter.addLast(decoded)
                queuedSamples += decoded.size
                // Web 语义：超过硬上限丢最旧的 → 延迟有界
                val maxSamples = MAX_MS * samplesPerMs
                while (queuedSamples > maxSamples && jitter.size > 1) {
                    queuedSamples -= jitter.removeFirst().size
                    drops++
                }
            }
        } else {
            decodeErrors++
        }
    }

    private fun playLoop() {
        // 音频线程优先级：中端机（荣耀）主线程被瀑布/重组占满时，默认优先级会被饿死
        // → AudioTrack 欠载 → "卡顿 + 声音几乎出不来"。必须在**本线程**上设置才生效。
        android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_URGENT_AUDIO)
        val silence = ShortArray(OpusBridge.FRAME_SAMPLES)
        while (running) {
            val buf: ShortArray = synchronized(jitter) {
                if (priming) {
                    // 冷启动/欠载后：攒够水位再出声（web 的 priming 门）
                    if (queuedSamples < gateMs * samplesPerMs) return@synchronized silence
                    priming = false
                }
                if (jitter.isNotEmpty()) {
                    val f = jitter.removeFirst()
                    queuedSamples -= f.size
                    f
                } else {
                    underruns++
                    priming = true
                    gateMs = RECOVERY_MS
                    silence
                }
            }
            val g = RxGain.target(volume, boost, transmitting)
            val out = when {
                g == 0f -> silence
                g == 1f -> buf
                else -> ShortArray(buf.size) {
                    (buf[it] * g).roundToInt().coerceIn(-32768, 32767).toShort()
                }
            }
            val w = track?.write(out, 0, out.size) ?: -1
            if (w >= 0) trackWrites++ else trackWriteErrors++
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
        flush()
        track?.pause(); track?.flush(); track?.release()
        track = null
    }

    fun release() { stop(); OpusBridge.destroyDecoder(decoder) }

    companion object {
        const val FRAME_MS = 20

        /** 与 web rx_worklet_processor.js 相同的水位：冷启动 220ms / 欠载恢复 90ms / 硬上限 800ms。 */
        const val PREBUFFER_MS = 220
        const val RECOVERY_MS = 90
        const val MAX_MS = 800
    }
}
