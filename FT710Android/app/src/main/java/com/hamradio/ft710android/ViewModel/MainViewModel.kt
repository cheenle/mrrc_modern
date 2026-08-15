package com.hamradio.ft710android.ViewModel

import com.hamradio.ft710android.Data.MemoryChannel
import com.hamradio.ft710android.Data.MemoryChannels
import com.hamradio.ft710android.Data.RadioState
import com.hamradio.ft710android.Network.AuthApi
import com.hamradio.ft710android.Network.AuthResult
import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.WsEvent
import com.hamradio.ft710android.Network.parseWsEvent
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.Spectrum.SpectrumProcessor
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

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

    fun onWsEvent(text: String) {
        when (val ev = parseWsEvent(text)) {
            is WsEvent.FullState -> {
                state.apply(ev.data)
                _version.value++
                _bands.value = ev.bands
                _modes.value = ev.modes
                _atr.value = ev.atr1000Enabled
                onMemChannels(ev.memChannels)
            }
            is WsEvent.StateUpdate -> {
                val dirty = state.apply(ev.fields)
                _version.value++
                if ("tx_status" in dirty) pttManager?.onStatusReceived(state.txStatus)
            }
            is WsEvent.MemChannels -> onMemChannels(ev.channels)
            is WsEvent.ErrorEvent -> _error.value = ev.message
            else -> Unit
        }
    }

    fun onAudioRxFrame(frame: ByteArray) { rxPlayer?.onFrame(frame) }
    fun onSpectrumFrame(frame: ByteArray) { spectrumProcessor?.onFrame(frame) }

    private fun onMemChannels(list: List<JsonElement?>) {
        _mem.value = MemoryChannels.parse(list)
    }

    suspend fun connect(host: String, port: String, password: String): AuthResult {
        val api = authApi ?: return AuthResult.Failure(0, "auth not configured")
        val base = "https://$host:$port"
        val res = api.login(base, password)
        if (res is AuthResult.Success) {
            connectionManager.start(base, res.token)
        }
        return res
    }

    suspend fun logout() {
        connectionManager.stopAll()
        _connected.value = false
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

    fun disconnect() { connectionManager.stopAll(); _connected.value = false }

    fun setScopeSpan(span: Int) = sendSet("scope_span", span)
    fun setRfPower(w: Int) = sendSet("rf_power", w)

    // 轻量接口，便于测试注入与对音频/频谱的强类型
    interface RxPlayerLike { fun onFrame(frame: ByteArray) }
    interface TxCaptureLike { fun start(); fun stop() }
    interface MemoryStore
}
