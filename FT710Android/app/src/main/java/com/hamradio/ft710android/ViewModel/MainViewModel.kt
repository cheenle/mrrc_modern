package com.hamradio.ft710android.ViewModel

import com.hamradio.ft710android.Data.Capabilities
import com.hamradio.ft710android.Data.FreqInput
import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Data.MemoryChannels
import com.hamradio.ft710android.Data.RadioCaps
import com.hamradio.ft710android.Data.RadioState
import com.hamradio.ft710android.Network.AuthApi
import com.hamradio.ft710android.Network.AuthResult
import com.hamradio.ft710android.Network.AtrEvent
import com.hamradio.ft710android.Network.AtrStateDto
import com.hamradio.ft710android.Network.AtrText
import com.hamradio.ft710android.Network.BandDto
import com.hamradio.ft710android.Network.CloudApi
import com.hamradio.ft710android.Network.CloudResult
import com.hamradio.ft710android.Network.CloudStateDto
import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.CqStatusDto
import com.hamradio.ft710android.Network.RecordingRow
import com.hamradio.ft710android.Network.RecordingStatusDto
import com.hamradio.ft710android.Network.RecordingsApi
import com.hamradio.ft710android.Network.WsEvent
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.Spectrum.SpectrumProcessor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import java.io.File

/**
 * 总协调器：登录→4 路连接→事件分发→状态/音频/频谱/PTT。普通类，Compose 内 remember 创建。
 * ConnectionManager 回调由 ServiceLocator（Task 17）在构造时接到 onWsEvent / onAudioRxFrame / onSpectrumFrame。
 */
class MainViewModel(
    private val authApi: AuthApi?,
    val connectionManager: ConnectionManager,
    private val rxPlayer: RxPlayerLike?,
    private val txCapture: TxCaptureLike?,
    private val spectrumProcessor: SpectrumProcessor?,
    private val memoryChannelsStore: MemoryStore?,
    val pttManager: PTTManager?,
    private val scope: CoroutineScope,
    private val recordingsApi: RecordingsApi? = null,
    private val cloudApi: CloudApi? = null,
    private val background: BackgroundRxController? = null,
) {
    val state = RadioState()

    /** 状态版本号：每次 state.apply 后 +1，Compose 读 vm.state.* 并以 version 订阅重组（RadioState 是可变普通类）。 */
    private val _version = MutableStateFlow(0L)
    val version: StateFlow<Long> = _version

    private val _waterfall = MutableStateFlow<List<IntArray>>(emptyList())
    val waterfall: StateFlow<List<IntArray>> = _waterfall
    private val _fft = MutableStateFlow(IntArray(850))
    val fft: StateFlow<IntArray> = _fft
    private val _connected = MutableStateFlow(false)
    val connected: StateFlow<Boolean> = _connected
    private val _listenOnly = MutableStateFlow(false)
    val listenOnly: StateFlow<Boolean> = _listenOnly
    private val _bands = MutableStateFlow<List<BandDto>>(emptyList())
    val bands: StateFlow<List<BandDto>> = _bands
    private val _modes = MutableStateFlow<List<String>>(emptyList())
    val modes: StateFlow<List<String>> = _modes
    private val _mem = MutableStateFlow<List<MemoryChannel?>>(emptyList())
    val memChannels: StateFlow<List<MemoryChannel?>> = _mem
    private val _atr1000Enabled = MutableStateFlow(false)
    val atr1000Enabled: StateFlow<Boolean> = _atr1000Enabled
    private val _atrState = MutableStateFlow<AtrStateDto?>(null)
    val atrState: StateFlow<AtrStateDto?> = _atrState
    private val _atrTuning = MutableStateFlow(false)
    val atrTuning: StateFlow<Boolean> = _atrTuning
    private val _caps = MutableStateFlow(RadioCaps())
    val caps: StateFlow<RadioCaps> = _caps
    private val _displayName = MutableStateFlow("")
    val displayName: StateFlow<String> = _displayName
    private val _notice = MutableStateFlow<String?>(null)
    val notice: StateFlow<String?> = _notice
    private val _userOff = MutableStateFlow(false)
    val userOff: StateFlow<Boolean> = _userOff
    private val _rttMs = MutableStateFlow<Long?>(null)
    val rttMs: StateFlow<Long?> = _rttMs
    private val _rxKbps = MutableStateFlow(0L)
    val rxKbps: StateFlow<Long> = _rxKbps
    private val _txKbps = MutableStateFlow(0L)
    val txKbps: StateFlow<Long> = _txKbps
    private val _diag = MutableStateFlow("")
    val diag: StateFlow<String> = _diag
    private val _recordingsCount = MutableStateFlow(0)
    val recordingsCount: StateFlow<Int> = _recordingsCount
    private val _recordingsBytes = MutableStateFlow(0L)
    val recordingsBytes: StateFlow<Long> = _recordingsBytes
    private val _cloud = MutableStateFlow<CloudStateDto?>(null)
    val cloud: StateFlow<CloudStateDto?> = _cloud
    private val _cloudMsg = MutableStateFlow<String?>(null)
    val cloudMsg: StateFlow<String?> = _cloudMsg
    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error
    private val _recordings = MutableStateFlow<List<RecordingRow>>(emptyList())
    val recordings: StateFlow<List<RecordingRow>> = _recordings
    private val _recordingState = MutableStateFlow(RecordingStatusDto())
    val recordingState: StateFlow<RecordingStatusDto> = _recordingState
    private val _recordingsAvailable = MutableStateFlow(false)
    val recordingsAvailable: StateFlow<Boolean> = _recordingsAvailable
    private val _cq = MutableStateFlow<CqStatusDto?>(null)
    val cq: StateFlow<CqStatusDto?> = _cq
    private val _cqAvailable = MutableStateFlow(false)
    val cqAvailable: StateFlow<Boolean> = _cqAvailable
    private var baseUrl: String? = null
    private var token: String? = null
    private var savedMicGain: Int? = null
    private var statsJob: Job? = null
    private var backgroundRxPref = true

    fun onWsEvent(ev: WsEvent) {
        when (ev) {
            is WsEvent.FullState -> {
                state.apply(ev.data)
                _version.value++
                _bands.value = ev.bands
                _modes.value = ev.modes
                _caps.value = RadioCaps.from(ev.capabilities)
                _displayName.value = ev.radioDisplayName ?: _caps.value.displayName
                _atr1000Enabled.value = ev.atr1000Enabled
                _recordingsAvailable.value = ev.recording != null
                _cqAvailable.value = ev.cq != null
                ev.recording?.let { _recordingState.value = it }
                ev.cq?.let { _cq.value = it }
                onMemChannels(ev.memChannels)
                syncAudio()
                applySavedMicGain()
            }
            is WsEvent.StateUpdate -> {
                val dirty = state.apply(ev.fields)
                _version.value++
                if ("tx_status" in dirty) pttManager?.onStatusReceived(state.txStatus)
                syncAudio()
            }
            is WsEvent.MemChannels -> onMemChannels(ev.channels)
            is WsEvent.RecordingState -> _recordingState.value = ev.status
            is WsEvent.CqState -> _cq.value = ev.status
            is WsEvent.ErrorEvent -> _error.value = ev.message
            is WsEvent.Pong -> connectionManager.onPong()
            else -> Unit
        }
    }

    /** /WSatr1000 事件：数据面 + 调谐进行中 + 结果提示（Web atr1000.js 语义）。 */
    fun onAtrEvent(ev: AtrEvent) {
        when (ev) {
            is AtrEvent.State -> _atrState.value = ev.s
            is AtrEvent.TuneResult -> {
                _atrTuning.value = ev.r.phase == "start" || ev.r.phase == "auto_start"
                if (!_atrTuning.value) _notice.value = AtrText.result(ev.r)
            }
            is AtrEvent.Error -> _error.value = ev.message
        }
    }

    /** ATR 手动调谐（服务端自带 TX2 载波 + 比对回滚）；未连接时提示。 */
    fun atrTune() {
        if (_atrTuning.value) return
        if (!connectionManager.sendAtrTune()) _notice.value = "ATR1000 未连接"
        else _atrTuning.value = true
    }

    /** RX 播放增益与 TX 静音跟随电台状态（web _applyAfGainToAudioNode 语义）。 */
    private fun syncAudio() {
        rxPlayer?.setBoost(_caps.value.audioBoost)
        rxPlayer?.setTransmitting(state.txStatus != 0)
    }

    fun onAudioRxFrame(frame: ByteArray) { rxPlayer?.onFrame(frame) }

    /** 频谱帧 → 处理器 → 推给 UI 的瀑布/FFT 流（此前只喂了处理器，UI 永远空）。 */
    fun onSpectrumFrame(frame: ByteArray) {
        val p = spectrumProcessor ?: return
        p.onFrame(frame)
        _waterfall.value = p.waterfall
        _fft.value = p.fft
    }

    /** ConnectionManager 四路聚合状态透传（驱动连接指示点与后台服务）。 */
    fun onConnectionChange(connected: Boolean) {
        _connected.value = connected
        if (connected) syncAudio()
        syncBackground()
    }

    /** RX 音频通道自身的在线状态：播放器只依赖它，不等四路全齐（真机 2026-10-05）。 */
    fun onAudioRxChange(up: Boolean) {
        if (up) { rxPlayer?.start(); syncAudio() } else rxPlayer?.stop()
    }

    /** 只读登录（服务端以 4003 关闭 TX/ATR 通道）——隐藏发射类 UI。 */
    fun onListenOnly() { _listenOnly.value = true; syncBackground() }

    /** 「后台接收」开关（DataStore）；开启且连接存在时前台服务常驻。 */
    fun setBackgroundRxPref(v: Boolean) { backgroundRxPref = v; syncBackground() }

    private fun syncBackground() {
        background?.setEnabled(_connected.value && backgroundRxPref && !_listenOnly.value)
    }

    private fun onMemChannels(list: List<JsonElement?>) {
        _mem.value = MemoryChannels.parse(list)
    }

    suspend fun connect(host: String, port: String, password: String): AuthResult {
        val api = authApi ?: return AuthResult.Failure(0, "auth not configured")
        val base = "https://$host:$port"
        _listenOnly.value = false
        _userOff.value = false
        val res = api.login(base, password)
        if (res is AuthResult.Success) {
            baseUrl = base; token = res.token
            connectionManager.start(base, res.token)
            startStats()
        }
        return res
    }

    suspend fun logout() {
        stopStats()
        rxPlayer?.stop()
        connectionManager.stopAll()
        _connected.value = false
        _listenOnly.value = false
        _userOff.value = false
        baseUrl = null; token = null
        _recordingsAvailable.value = false; _cqAvailable.value = false
        syncBackground()
    }

    /** M7 连接开关的"关"：停全部通道 + 强制释放 TX，保留 base/token 供重连。 */
    fun disconnect() {
        pttManager?.forceRelease()
        stopStats()
        rxPlayer?.stop()
        connectionManager.disconnect()
        _connected.value = false
        _userOff.value = true
        syncBackground()
    }

    /** M7 连接开关的"开"：用保存的会话重连（无会话时由登录页接管）。 */
    fun reconnect() {
        _userOff.value = false
        connectionManager.reconnectAll()
        startStats()
    }

    /** 供 UI 显示/浏览器入口使用；未登录时回默认地址。 */
    fun baseUrlForUi(): String = baseUrl
        ?: "https://${com.hamradio.ft710android.Data.SettingsStore.DEFAULT_HOST}:${com.hamradio.ft710android.Data.SettingsStore.DEFAULT_PORT}"

    private fun startStats() {
        statsJob?.cancel()
        statsJob = scope.launch {
            while (isActive) {
                delay(1000)
                _rxKbps.value = connectionManager.drainRx() * 8 / 1000
                _txKbps.value = connectionManager.drainTx() * 8 / 1000
                _rttMs.value = connectionManager.lastRttMs()
                // 设备侧音频链路边界（真机无声事故的取证行）
                _diag.value = (rxPlayer?.stats() ?: "A:none") +
                    " S:${_waterfall.value.size} ch:${connectionManager.channelsSummary()}" +
                    " ${txCapture?.stats() ?: "TX[off]"} tx:${state.txStatus}"
            }
        }
    }

    private fun stopStats() {
        statsJob?.cancel(); statsJob = null
        _rxKbps.value = 0; _txKbps.value = 0; _rttMs.value = null
    }

    fun sendSet(field: String, value: Any) = connectionManager.sendSet(field, value)

    fun setFrequencyStep(deltaHz: Long) { sendSet("freq", state.activeFrequency + deltaHz) }

    fun setMode(mode: String) = sendSet("mode", mode)
    fun setBand(freqHz: Long) = sendSet("freq", freqHz)
    fun cycleFilter() = sendSet(
        "filter",
        Capabilities.nextFilter(state.filterWidth, state.modeName, null, _caps.value.filterModel),
    )

    /** 点击瀑布 QSY：按当前量程与 VFO 把 x 比例换算成目标频率。 */
    fun qsy(fraction: Float) {
        val hz = FreqInput.qsy(state.activeFrequency, _caps.value.spanHz(state.scopeSpan), fraction)
        sendSet(if (state.activeVfo == "B") "vfo_b_freq" else "freq", hz)
    }

    /** 频率输入框提交（MHz）。 */
    fun sendFreqHz(hz: Long) {
        sendSet(if (state.activeVfo == "B") "vfo_b_freq" else "freq", hz)
    }

    /** 下一档滤波（D6）：voice/narrow 策划表 / fil123 轮转。 */
    fun cycleFilterWith(tables: com.hamradio.ft710android.Network.FilterTables?) = sendSet(
        "filter", Capabilities.nextFilter(state.filterWidth, state.modeName, tables, _caps.value.filterModel),
    )

    fun onPttGesture() { pttManager?.press() }
    fun onPttRelease() { pttManager?.forceRelease() }

    fun recallMemory(index: Int) {
        val c = _mem.value.getOrNull(index) ?: return
        if (c == null) return
        sendSet("freq", c.freq); sendSet("mode", c.mode)
    }

    fun saveMemory(index: Int) {
        val list = _mem.value.toMutableList()
        while (list.size < 6) list.add(null)
        list[index] = MemoryChannel(state.activeFrequency, state.modeName, "M${index + 1}")
        _mem.value = list
        connectionManager.sendMemSave(MemoryChannels.toJson(list))
    }

    fun clearMemory(index: Int) {
        val list = _mem.value.toMutableList()
        if (index in list.indices) list[index] = null
        _mem.value = list
        connectionManager.sendMemSave(MemoryChannels.toJson(list))
    }

    fun setScopeSpan(span: Int) = sendSet("scope_span", span)
    fun setRfPower(w: Int) = sendSet("rf_power", w)
    fun setRfGain(raw: Int) = sendSet("rf_gain", raw)
    fun setMicGain(v: Int) = sendSet("mic_gain", v)
    fun setNrLevel(v: Int) = sendSet("nr_level", v)
    fun setNbLevel(v: Int) = sendSet("nb_level", v)
    fun setScopeSpeed(v: Int) = sendSet("scope_speed", v)

    /** 本机音量（0..255，DataStore afVol 同步进来）。 */
    fun setAfVol(v: Int) { rxPlayer?.setVolume(v) }

    /** 本机麦克风增益（0..200，DataStore micVol 同步进来）。 */
    fun setMicVol(v: Int) { txCapture?.setMicVol(v) }

    /** 回退值：DataStore 里的 mic_gain；每次 fullState 与服务端不一致时回推一次。 */
    fun setSavedMicGain(v: Int?) { savedMicGain = v; applySavedMicGain() }

    private fun applySavedMicGain() {
        val v = savedMicGain ?: return
        if (v != state.micGain) sendSet("mic_gain", v)
    }

    /** 状态行统计：RTT + 抖动缓冲（毫秒）。 */
    fun audioBufferMs(): Int = rxPlayer?.bufferMs ?: 0

    fun showNotice(message: String) { _notice.value = message }
    fun clearNotice() { _notice.value = null }

    // ── 录音（AD-017）与 CQ（AD-020）───────────────────────────────
    fun startRecording() = sendSet("recording", true)
    fun stopRecording() = sendSet("recording", false)
    fun startCq() = sendSet("cq", true)
    fun abortCq() = sendSet("cq", false)

    fun refreshRecordings() {
        val api = recordingsApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        scope.launch {
            val summary = api.listSummary(base, t)
            _recordings.value = summary.recordings
            _recordingsCount.value = summary.count
            _recordingsBytes.value = summary.totalBytes
        }
    }

    fun deleteRecording(name: String) {
        val api = recordingsApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        scope.launch { if (api.delete(base, t, name)) refreshRecordings() }
    }

    suspend fun downloadRecording(name: String, destDir: File): File? {
        val api = recordingsApi ?: return null
        val base = baseUrl ?: return null
        val t = token ?: return null
        return api.download(base, t, name, File(destDir, name))
    }

    fun showError(message: String) { _error.value = message }
    fun clearError() { _error.value = null }

    // ── Cloud Hub（S6；server.py:4317+ 的四个端点）───────────────────
    fun cloudRefresh() {
        val api = cloudApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        scope.launch {
            when (val r = api.state(base, t)) {
                is CloudResult.Ok -> {
                    _cloud.value = r.value
                    if (r.value.hasToken && !r.value.connected) {
                        when (val rr = api.refresh(base, t)) {
                            is CloudResult.Ok -> {
                                _cloud.value = rr.value
                                if (rr.value.connected) onCloudConnected(rr.value)
                            }
                            is CloudResult.Err -> _cloudMsg.value = rr.message
                        }
                    }
                }
                is CloudResult.Err -> _cloudMsg.value = r.message
            }
        }
    }

    fun cloudApply(callsign: String, contact: String, secret: String) {
        val api = cloudApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        if (callsign.isBlank()) { _cloudMsg.value = "请填呼号"; return }
        _cloudMsg.value = if (secret.isBlank()) "提交中…" else "接入中…"
        scope.launch {
            when (val r = api.apply(base, t, callsign.trim(), contact.trim(), secret.trim())) {
                is CloudResult.Ok -> {
                    _cloud.value = r.value
                    if (r.value.connected) onCloudConnected(r.value)
                    else _cloudMsg.value = "已提交 ✓ 等运维批准后这里会自动继续"
                }
                is CloudResult.Err -> _cloudMsg.value = r.message
            }
        }
    }

    fun cloudRestart() {
        val api = cloudApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        scope.launch {
            when (val r = api.restart(base, t)) {
                is CloudResult.Ok -> {
                    _cloudMsg.value = "实例重启中，约 6 秒后自动重连…"
                    delay(10_000)
                    reconnect()
                }
                is CloudResult.Err -> _cloudMsg.value = r.message
            }
        }
    }

    private fun onCloudConnected(s: CloudStateDto) {
        _cloudMsg.value = if (s.certReloadRequired)
            "已接入 ✓ 还需重启应用以启用新证书（否则入口会 502）" else "已接入 ✓"
    }

    fun clearCloudMsg() { _cloudMsg.value = null }

    // 轻量接口，便于测试注入与对音频/频谱的强类型
    interface RxPlayerLike {
        fun onFrame(frame: ByteArray)
        fun start()
        fun stop()
        fun setVolume(v: Int)
        fun setBoost(b: Float)
        fun setTransmitting(t: Boolean)
        val bufferMs: Int
        fun stats(): String
    }
    interface TxCaptureLike { fun start(); fun stop(); fun setMicVol(v: Int); fun stats(): String }
    interface MemoryStore

    /** 后台 RX 前台服务的可注入接口（ServiceLocator 接到 RxForegroundService）。 */
    fun interface BackgroundRxController { fun setEnabled(on: Boolean) }
}
