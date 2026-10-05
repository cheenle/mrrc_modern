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

    /** 本机麦克风软件增益（0..200，web 🎙 Vol 语义）。 */
    override fun setMicVol(v: Int) { micVol = v.coerceIn(0, 200) }

    override fun start() {
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            onError?.invoke("Missing RECORD_AUDIO permission"); return
        }
        if (job != null) return
        val rec = openPreferred()
        if (rec == null) { onError?.invoke("无法打开麦克风（48k/44.1k 均失败）"); return }
        record = rec
        val rate = rec.sampleRate
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
                var off = 0
                while (off < n) {
                    val take = minOf(frame - have, n - off)
                    System.arraycopy(read, off, acc, have, take)
                    have += take; off += take
                    if (have == frame) {
                        val pcm = TxFraming.applyMicVol(acc, micVol)
                        val pcm48 = if (rate == 44100) Resampler.resample882To960(pcm) else pcm
                        val written = OpusBridge.encoderEncode(encoder, pcm48, out)
                        if (written > 0) sendFrame(byteArrayOf(0x01) + out.copyOf(written))
                        have = 0
                    }
                }
            }
        }
    }

    /** 48k 优先；getMinBufferSize 或 state 不合法时回退 44.1k；都失败返回 null。 */
    private fun openPreferred(): AudioRecord? {
        for (rate in intArrayOf(48000, 44100)) {
            val minBuf = AudioRecord.getMinBufferSize(
                rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            if (minBuf <= 0) continue
            val r = AudioRecord(
                MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuf, TxFraming.frameSamples(rate) * 2 * 4))
            if (r.state == AudioRecord.STATE_INITIALIZED) return r
            runCatching { r.release() }
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
