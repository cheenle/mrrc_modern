package com.hamradio.ft710android.Audio

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * TX 采集：AudioRecord 48k 单声道 → 960 样本块 → Opus 编码 → [0x01+payload] 回调。
 * 服务端 TX 链路是 48k 域；设备只给 44.1k 时的重采样留作后续增强（TODO(remap)）。
 */
class TxAudioCapture(
    private val context: Context,
    private val sendFrame: (ByteArray) -> Unit,
) {
    private val encoder = OpusBridge.encoderCreate(OpusBridge.SAMPLE_RATE, OpusBridge.CHANNELS, OpusBridge.BITRATE)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var job: Job? = null
    private var record: AudioRecord? = null
    var onError: ((String) -> Unit)? = null

    fun start() {
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            onError?.invoke("Missing RECORD_AUDIO permission"); return
        }
        val buf = ShortArray(OpusBridge.FRAME_SAMPLES)
        val minBuf = AudioRecord.getMinBufferSize(
            OpusBridge.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val rec = AudioRecord(
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
            OpusBridge.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf, OpusBridge.FRAME_SAMPLES * 2 * 4))
        record = rec
        rec.startRecording()
        job = scope.launch {
            val out = ByteArray(4096)
            while (isActive) {
                val n = rec.read(buf, 0, buf.size)
                if (n <= 0) continue
                val written = OpusBridge.encoderEncode(encoder, buf.copyOf(n), out)
                if (written > 0) sendFrame(byteArrayOf(0x01) + out.copyOf(written))
            }
        }
    }

    fun stop() {
        job?.cancel()
        runCatching { record?.stop() }
        record?.release()
        record = null
    }

    fun release() { stop(); OpusBridge.destroyEncoder(encoder); scope.cancel() }
}
