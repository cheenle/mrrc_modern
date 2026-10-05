package com.hamradio.ft710android.Audio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * TX 采集：AudioRecord → 20ms 帧 → Mic Vol → （设备只给 44.1k 时 882→960 重采样）→ Opus → 回调。
 * 服务端 TX 链路是 48k 域；采样率探测 48k 优先、失败回退 44.1k（L2）。
 */
class TxAudioCapture(
    private val context: Context,
    private val sendFrame: (ByteArray) -> Unit,
) : MainViewModel.TxCaptureLike {
    private val encoder = OpusBridge.encoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS, OpusBridge.BITRATE)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null
    private var record: AudioRecord? = null
    var onError: ((String) -> Unit)? = null

    @Volatile var micVol: Int = 100
        private set
    @Volatile var samplesRead = 0L; private set
    @Volatile var framesSent = 0L; private set
    @Volatile var activeRate = 0; private set
    @Volatile var activeSource = 0; private set
    @Volatile var lastPeak = 0; private set

    /** 诊断行：rec=采集在跑否，rate=实际采样率，R=读到的样本，X=发出去的 Opus 帧。 */
    private fun hasMic(): Boolean =
        context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    override fun stats(): String =
        "TX[${if (job != null) "rec" else "off"} ${if (hasMic()) "mic:ok" else "mic:NO"} " +
        "${activeRate} src:$activeSource pk:$lastPeak R:$samplesRead X:$framesSent]"

    /** 本机麦克风软件增益（0..200，web 🎙 Vol 语义）。 */
    override fun setMicVol(v: Int) { micVol = v.coerceIn(0, 200) }

    override fun start() {
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            onError?.invoke("缺少麦克风权限：发射没有话音（系统设置 → 应用 → MRRC Modern → 权限 → 麦克风）"); return
        }
        if (job != null) return
        val rec = openPreferred()
        if (rec == null) { onError?.invoke("无法打开麦克风（48k/44.1k 均失败）"); return }
        record = rec
        val rate = rec.sampleRate
        activeRate = rate
        val frame = TxFraming.frameSamples(rate)
        rec.startRecording()
        job = scope.launch {
            val out = ByteArray(4096)
            val acc = ShortArray(frame)
            var have = 0
            val read = ShortArray(frame)
            while (isActive) {
                val n = rec.read(read, 0, read.size)
                if (n <= 0) continue
                samplesRead += n
                var off = 0
                while (off < n) {
                    val take = minOf(frame - have, n - off)
                    System.arraycopy(read, off, acc, have, take)
                    have += take; off += take
                    if (have == frame) {
                        var pk = 0
                        for (v in acc) { val a = if (v < 0) -v.toInt() else v.toInt(); if (a > pk) pk = a }
                        lastPeak = pk
                        val pcm = TxFraming.applyMicVol(acc, micVol)
                        val pcm48 = if (rate == 44100) Resampler.resample882To960(pcm) else pcm
                        val written = OpusBridge.encoderEncode(encoder, pcm48, out)
                        if (written > 0) {
                            framesSent++
                            sendFrame(byteArrayOf(0x01) + out.copyOf(written))
                        }
                        have = 0
                    }
                }
            }
        }
    }

    /**
     * 采集源与采样率组合，按 Web 语义优先"未处理"输入：
     * 浏览器明确 echoCancellation/noiseSuppression/autoGainControl 全关；
     * Android 的 VOICE_COMMUNICATION 会强制 AGC/降噪，把话音压得很小（真机"功率非常小"）。
     * 顺序：UNPROCESSED@48k → MIC@48k → UNPROCESSED@44.1k → MIC@44.1k。
     */
    private fun openPreferred(): AudioRecord? {
        // lint MissingPermission：即使 start() 已查过，构造函数所在方法也要自证
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            return null
        }
        val sources = if (android.os.Build.VERSION.SDK_INT >= 24) {
            intArrayOf(MediaRecorder.AudioSource.UNPROCESSED, MediaRecorder.AudioSource.MIC)
        } else {
            intArrayOf(MediaRecorder.AudioSource.MIC)
        }
        for (source in sources) {
            for (rate in intArrayOf(48000, 44100)) {
                val minBuf = AudioRecord.getMinBufferSize(
                    rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                if (minBuf <= 0) continue
                val r = AudioRecord(
                    source, rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    maxOf(minBuf, TxFraming.frameSamples(rate) * 2 * 4))
                if (r.state == AudioRecord.STATE_INITIALIZED) {
                    activeSource = source
                    return r
                }
                runCatching { r.release() }
            }
        }
        return null
    }

    override fun stop() {
        job?.cancel()
        job = null
        runCatching { record?.stop() }
        record?.release()
        record = null
    }

    fun release() { stop(); OpusBridge.destroyEncoder(encoder); scope.cancel() }
}
