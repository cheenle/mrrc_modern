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
 *
 * 采集源按 Web 语义优先"未处理"输入（浏览器明确关掉 echoCancellation/noiseSuppression/autoGainControl）：
 * `UNPROCESSED@48k → MIC@48k → UNPROCESSED@44.1k → MIC@44.1k`。
 * **不把 `VOICE_COMMUNICATION` 放进候选**：它强制 AGC/降噪，会把话音压到"极微弱"。
 * 每个候选先探测 700ms 内是否有数据——某些机型 `UNPROCESSED` 能初始化但永远不给样本，
 * 不探测就会表现为"电台键控了、一点声音都没有"。
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

    @Volatile var micVol: Int = 150
        private set
    @Volatile var samplesRead = 0L; private set
    @Volatile var framesSent = 0L; private set
    @Volatile var activeRate = 0; private set
    @Volatile var activeSource = 0; private set
    @Volatile var lastPeak = 0; private set

    /** 本机麦克风软件增益（0..400，100 = 原始）。手机麦普遍比 PC 耳麦低十几 dB，默认给 1.5×。 */
    override fun setMicVol(v: Int) { micVol = v.coerceIn(0, MAX_MIC_VOL) }

    override fun peak(): Int = lastPeak

    private fun hasMic(): Boolean =
        context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    override fun stats(): String =
        "TX[${if (job != null) "rec" else "off"} ${if (hasMic()) "mic:ok" else "mic:NO"} " +
        "${activeRate} src:$activeSource vol:$micVol pk:$lastPeak R:$samplesRead X:$framesSent]"

    override fun start() {
        if (!hasMic()) {
            onError?.invoke("缺少麦克风权限：发射没有话音（系统设置 → 应用 → MRRC Modern → 权限 → 麦克风）")
            return
        }
        if (job != null) return
        job = scope.launch {
            for (cand in candidates()) {
                if (probeAndRun(cand)) return@launch   // 有数据 → 采集循环跑到底（直到 stop）
            }
            record = null
            onError?.invoke("麦克风采集失败：UNPROCESSED/MIC × 48k/44.1k 都读不到数据")
        }
    }

    private data class Candidate(val source: Int, val rate: Int)

    private fun candidates(): List<Candidate> {
        val sources = if (android.os.Build.VERSION.SDK_INT >= 24) {
            intArrayOf(MediaRecorder.AudioSource.UNPROCESSED, MediaRecorder.AudioSource.MIC)
        } else {
            intArrayOf(MediaRecorder.AudioSource.MIC)
        }
        return sources.flatMap { s -> intArrayOf(48000, 44100).map { r -> Candidate(s, r) } }
    }

    /** 打开 → 探测 700ms → 有数据就把读循环跑下去；没数据就释放并返回 false 试下一个。 */
    private suspend fun probeAndRun(cand: Candidate): Boolean {
        val rec = open(cand) ?: return false
        record = rec
        activeRate = cand.rate
        activeSource = cand.source
        val frame = TxFraming.frameSamples(cand.rate)
        val read = ShortArray(frame)
        rec.startRecording()
        val deadline = System.currentTimeMillis() + PROBE_MS
        var first = 0
        while (first == 0 && System.currentTimeMillis() < deadline && scope.isActive) {
            val n = rec.read(read, 0, read.size)
            if (n > 0) first = n
        }
        if (first <= 0) {
            runCatching { rec.stop() }
            rec.release()
            record = null
            return false
        }
        readLoop(rec, cand.rate, read, first)
        return true
    }

    /** 累积到整帧后：Mic Vol → （44.1k 时 882→960）→ Opus → 发帧。 */
    private suspend fun readLoop(rec: AudioRecord, rate: Int, firstBuf: ShortArray, firstN: Int) {
        val frame = TxFraming.frameSamples(rate)
        val out = ByteArray(4096)
        val acc = ShortArray(frame)
        var have = 0
        var pending: ShortArray? = firstBuf
        var pendingN = firstN
        while (scope.isActive) {
            val buf: ShortArray
            val n: Int
            if (pending != null) {
                buf = pending; n = pendingN; pending = null
            } else {
                buf = ShortArray(frame)
                n = rec.read(buf, 0, buf.size)
            }
            if (n <= 0) continue
            samplesRead += n
            var off = 0
            while (off < n) {
                val take = minOf(frame - have, n - off)
                System.arraycopy(buf, off, acc, have, take)
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

    /** 单个候选源/采样率：getMinBufferSize 或 state 不合法就算失败。 */
    private fun open(cand: Candidate): AudioRecord? {
        // lint MissingPermission：即使 start() 已查过，AudioRecord(...) 所在方法也要自证
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED) {
            return null
        }
        val minBuf = AudioRecord.getMinBufferSize(
            cand.rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) return null
        val r = AudioRecord(
            cand.source, cand.rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
            maxOf(minBuf, TxFraming.frameSamples(cand.rate) * 2 * 4))
        if (r.state == AudioRecord.STATE_INITIALIZED) return r
        runCatching { r.release() }
        return null
    }

    override fun stop() {
        job?.cancel()
        job = null
        runCatching { record?.stop() }
        record?.release()
        record = null
        lastPeak = 0
    }

    fun release() { stop(); OpusBridge.destroyEncoder(encoder); scope.cancel() }

    companion object {
        /** 探测窗口：起播后多久没数据就换下一个候选源。 */
        const val PROBE_MS = 700L

        /** Mic Vol 上限（web 是 200=2×；手机麦更弱，放到 4×）。 */
        const val MAX_MIC_VOL = 400
    }
}
