package com.hamradio.ft710android.ViewModel

import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Data.MemoryChannels
import com.hamradio.ft710android.Data.RadioState
import com.hamradio.ft710android.Network.AuthApi
import com.hamradio.ft710android.Network.AuthResult
import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.CqStatusDto
import com.hamradio.ft710android.Network.RecordingRow
import com.hamradio.ft710android.Network.RecordingStatusDto
import com.hamradio.ft710android.Network.RecordingsApi
import com.hamradio.ft710android.Network.WsEvent
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.Spectrum.SpectrumProcessor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
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
    private val _bands = MutableStateFlow<List<String>>(emptyList())
    val bands: StateFlow<List<String>> = _bands
    private val _modes = MutableStateFlow<List<String>>(emptyList())
    val modes: StateFlow<List<String>> = _modes
    private val _mem = MutableStateFlow<List<MemoryChannel?>>(emptyList())
    val memChannels: StateFlow<List<MemoryChannel?>> = _mem
    private val _atr = MutableStateFlow(false)
    val atr1000Enabled: StateFlow<Boolean> = _atr
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

    fun onWsEvent(ev: WsEvent) {
        when (ev) {
            is WsEvent.FullState -> {
                state.apply(ev.data)
                _version.value++
                _bands.value = ev.bands
                _modes.value = ev.modes
                _atr.value = ev.atr1000Enabled
                _recordingsAvailable.value = ev.recording != null
                _cqAvailable.value = ev.cq != null
                ev.recording?.let { _recordingState.value = it }
                ev.cq?.let { _cq.value = it }
                onMemChannels(ev.memChannels)
            }
            is WsEvent.StateUpdate -> {
                val dirty = state.apply(ev.fields)
                _version.value++
                if ("tx_status" in dirty) pttManager?.onStatusReceived(state.txStatus)
            }
            is WsEvent.MemChannels -> onMemChannels(ev.channels)
            is WsEvent.RecordingState -> _recordingState.value = ev.status
            is WsEvent.CqState -> _cq.value = ev.status
            is WsEvent.ErrorEvent -> _error.value = ev.message
            else -> Unit
        }
    }

    fun onAudioRxFrame(frame: ByteArray) { rxPlayer?.onFrame(frame) }
    fun onSpectrumFrame(frame: ByteArray) { spectrumProcessor?.onFrame(frame) }

    /** ConnectionManager 四路聚合状态透传（修复：此前无人写入 _connected）。 */
    fun onConnectionChange(connected: Boolean) { _connected.value = connected }

    /** 只读登录（服务端以 4003 关闭 TX/ATR 通道）——隐藏发射类 UI。 */
    fun onListenOnly() { _listenOnly.value = true }

    private fun onMemChannels(list: List<JsonElement?>) {
        _mem.value = MemoryChannels.parse(list)
    }

    suspend fun connect(host: String, port: String, password: String): AuthResult {
        val api = authApi ?: return AuthResult.Failure(0, "auth not configured")
        val base = "https://$host:$port"
        _listenOnly.value = false
        val res = api.login(base, password)
        if (res is AuthResult.Success) {
            baseUrl = base; token = res.token
            connectionManager.start(base, res.token)
        }
        return res
    }

    suspend fun logout() {
        connectionManager.stopAll()
        _connected.value = false
        _listenOnly.value = false
        baseUrl = null; token = null
        _recordingsAvailable.value = false; _cqAvailable.value = false
    }

    fun sendSet(field: String, value: Any) = connectionManager.sendSet(field, value)

    fun setFrequencyStep(deltaHz: Long) { sendSet("freq", state.activeFrequency + deltaHz) }

    fun setMode(mode: String) = sendSet("mode", mode)
    fun setBand(freqHz: Long) = sendSet("freq", freqHz)
    fun cycleFilter() = sendSet("filter", (state.filterWidth + 1) % 23)

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

    fun disconnect() {
        connectionManager.stopAll(); _connected.value = false; _listenOnly.value = false
        baseUrl = null; token = null
        _recordingsAvailable.value = false; _cqAvailable.value = false
    }

    fun setScopeSpan(span: Int) = sendSet("scope_span", span)
    fun setRfPower(w: Int) = sendSet("rf_power", w)

    // ── 录音（AD-017）与 CQ（AD-020）───────────────────────────────
    fun startRecording() = sendSet("recording", true)
    fun stopRecording() = sendSet("recording", false)
    fun startCq() = sendSet("cq", true)
    fun abortCq() = sendSet("cq", false)

    fun refreshRecordings() {
        val api = recordingsApi ?: return
        val base = baseUrl ?: return
        val t = token ?: return
        scope.launch { _recordings.value = api.list(base, t) }
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

    // 轻量接口，便于测试注入与对音频/频谱的强类型
    interface RxPlayerLike { fun onFrame(frame: ByteArray) }
    interface TxCaptureLike { fun start(); fun stop() }
    interface MemoryStore
}
