package com.hamradio.ft710android.Network

import com.hamradio.ft710android.Spectrum.SpectrumTiers
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient

/** 4 路 (+可选 ATR1000) WS 编排：认证 token 注入、心跳、命令路由、连接状态聚合。 */
class ConnectionManager(
    private val client: OkHttpClient,
    private val scope: CoroutineScope,
    private val onRadioEvent: (WsEvent) -> Unit,
    private val onAudioRx: (ByteArray) -> Unit,
    private val onSpectrum: (ByteArray) -> Unit,
    private val onAudioTxText: (String) -> Unit,
    private val onAtrEvent: (AtrEvent) -> Unit,
    private val onConnectionChange: (Boolean) -> Unit,
    private val onAudioRxChange: (Boolean) -> Unit = {},
    private val onListenOnly: () -> Unit = {},
    private val sendOverride: ((String) -> Unit)? = null,
    nowMs: () -> Long = { System.currentTimeMillis() },
    /** 测试用：拦下频谱通道上的文本发送（caps）。
     *
     *  与 [sendOverride] 同一个道理：`WebSocketConnection` 是 final class、没法 fake，
     *  而“到底往频谱通道发了什么”正是档位协商唯一可测的部分。
     *  放在最后是为了不惊动任何按位置传参的调用点。 */
    private val spectrumSendOverride: ((String) -> Unit)? = null,
) {
    private var radio: WebSocketConnection? = null
    private var audioRx: WebSocketConnection? = null
    private var audioTx: WebSocketConnection? = null
    private var spectrum: WebSocketConnection? = null
    private var atr: WebSocketConnection? = null
    private var heartbeat: Job? = null

    /** 服务端是否启用了 ATR-1000（来自 fullState.atr1000Enabled）。未启用就**不连** /WSatr1000。 */
    private var atrEnabled = false
    private val openedPathsLog = mutableListOf<String>()

    /**
     * 本次会话已请求打开的 WS 路径（诊断 + 测试断言用）。
     * ATR 是可选选件：没配 `MRRC_ATR1000_HOST` 的部署，这里**不该**出现 `/WSatr1000`
     * （否则每次连接都白握手一次、被服务端以 4000 立即关闭）。
     */
    val openedPaths: List<String> get() = synchronized(openedPathsLog) { openedPathsLog.toList() }
    private val connectedFlags = ChannelFlags()

    /** 频谱通道是否因退后台被主动暂停（不是故障）。 */
    private var spectrumPaused = false
    private val stats = NetworkStats(nowMs)

    @Volatile var isConnected: Boolean = false; private set
    @Volatile var listenOnly: Boolean = false; private set

    /** 控制通道在线（PTT 的唯一闸门：不需要等音频通道）。 */
    val isRadioConnected: Boolean get() = connectedFlags.has("/WSradio")

    /** 通道状态摘要（诊断行用）：R=radio A=audioRX T=audioTX S=spectrum（`S(p)` = 后台主动暂停）。 */
    fun channelsSummary(): String {
        fun m(path: String) = if (connectedFlags.has(path)) "+" else "-"
        val sMark = if (isSpectrumPaused) "(p)" else m("/WSspectrum")
        return "R${m("/WSradio")} A${m("/WSaudioRX")} T${m("/WSaudioTX")} S$sMark"
    }

    private var _baseUrl: String? = null
    private var _token: String? = null

    companion object {
        /**
         * 连接判据需要哪些通道（纯函数，可 JVM 测）。
         *
         * - 只读会话不要求 `/WSaudioTX`（服务端以 4003 关闭）
         * - **后台暂停频谱时不要求 `/WSspectrum`**：否则 `isConnected` 变 false，
         *   VM 的 `syncBackground()` 会把后台接收一起关掉（用户以为在收音，其实已断）
         */
        fun requiredChannels(listenOnly: Boolean, spectrumPaused: Boolean): Set<String> = buildSet {
            add("/WSradio")
            add("/WSaudioRX")
            if (!listenOnly) add("/WSaudioTX")
            if (!spectrumPaused) add("/WSspectrum")
        }

        fun wsUrl(baseUrl: String, path: String, token: String): String {
            val scheme = if (baseUrl.startsWith("https")) "wss" else "ws"
            val host = baseUrl.removePrefix("https://").removePrefix("http://")
            return "$scheme://$host$path?token=$token"
        }
    }

    fun start(baseUrl: String, token: String) {
        _baseUrl = baseUrl; _token = token
        stopAll()
        radio = connect(baseUrl, "/WSradio", token,
            onText = { stats.onReceived(it.length); onRadioEvent(parseWsEvent(it)) }, onBinary = {})
        audioRx = connect(baseUrl, "/WSaudioRX", token, onText = { stats.onReceived(it.length) },
            onBinary = { stats.onReceived(it.size); onAudioRx(it) })
        audioTx = connect(baseUrl, "/WSaudioTX", token,
            onText = { stats.onReceived(it.length); onAudioTxText(it) }, onBinary = {}, onClosedCode = ::handleCloseCode) // 上行二进制由 sendTxAudioBinary 发送
        // 后台状态下重连（网络切换等）不要把频谱又拉起来
        if (!spectrumPaused) connectSpectrum()
        // ATR-1000 是**可选选件**：只有服务端 fullState 说启用了才连（见 setAtrEnabled）。
        // 这里仅在重连且上一次已知启用时立即补连，冷启动时等 fullState。
        if (atrEnabled) connectAtr()
        heartbeat?.cancel()
        heartbeat = scope.launch { while (isActive) { sendPing(); delay(2000) } }
    }

    fun stopAll() {
        heartbeat?.cancel()
        listOfNotNull(radio, audioRx, audioTx, spectrum, atr).forEach { it.close() }
        radio = null; audioRx = null; audioTx = null; spectrum = null; atr = null
        connectedFlags.clear(); updateConnected(); listenOnly = false
        atrEnabled = false
        synchronized(openedPathsLog) { openedPathsLog.clear() }
    }

    /**
     * 服务端 fullState 到达后由 VM 调用：ATR-1000 启用才开 `/WSatr1000`，禁用则关掉。
     *
     * 未配置的部署服务端会 `accept` 后立刻以 **4000 "ATR1000 disabled"** 关闭
     * （只读会话是 4003），所以"不连"才是正确行为：省一次握手，也不制造无意义的关闭事件。
     */
    @Synchronized
    fun setAtrEnabled(enabled: Boolean) {
        if (atrEnabled == enabled) return
        atrEnabled = enabled
        if (enabled) {
            if (atr == null) connectAtr()
        } else {
            atr?.close()
            atr = null
        }
    }

    /** ATR 是否已启用（fullState 给的，不是"是否连上"）。 */
    val isAtrEnabled: Boolean get() = synchronized(this) { atrEnabled }

    private fun connectAtr() {
        val b = _baseUrl ?: return
        val t = _token ?: return
        if (b.isEmpty() || t.isEmpty()) return
        atr = connect(b, "/WSatr1000", t, onText = {
            stats.onReceived(it.length)
            parseAtrEvent(it)?.let(onAtrEvent)
        }, onBinary = {}, onClosedCode = ::handleCloseCode)
    }

    fun sendSet(field: String, value: Any) {
        val cmd = when (value) {
            is Boolean -> WsCommands.setBool(field, value)
            is String -> WsCommands.setString(field, value)
            is Number -> WsCommands.setNumber(field, value)
            else -> WsCommands.setNumber(field, value.toString().toLongOrNull() ?: 0L)
        }
        dispatch(cmd)
    }

    fun sendPing() { stats.onPingSent(); dispatch(WsCommands.ping()) }
    fun sendHeartbeat() = dispatch(WsCommands.txhb())
    fun sendMemSave(channelsJson: String) = dispatch(WsCommands.memSaveJson(channelsJson))
    fun sendTxAudioBinary(data: ByteArray) { stats.onSent(data.size); audioTx?.sendBinary(data) }
    fun sendTxAudioText(text: String) { stats.onSent(text.length); audioTx?.sendText(text) }

    /** ATR 手动调谐（服务端自带 TX2 载波 + 比对回滚）；未连接时返回 false。 */
    fun sendAtrTune(): Boolean {
        val cmd = """{"type":"atrTune"}"""
        stats.onSent(cmd.length)
        return atr?.sendText(cmd) ?: false
    }

    /** 收到 /WSradio 的 pong（由 MainViewModel 在 WsEvent.Pong 时调用）。 */
    fun onPong() = stats.onPong()

    fun lastRttMs(): Long? = stats.lastRttMs
    fun drainRx(): Long = stats.drainRx()
    fun drainTx(): Long = stats.drainTx()

    /** M7 连接开关的"关"：停全部通道（重连由 reconnectAll）。 */
    fun disconnect() = stopAll()

    fun reconnectAll() {
        val token = _token ?: return
        val base = _baseUrl ?: return
        stopAll(); start(base, token)
    }

    private fun connect(
        baseUrl: String,
        path: String,
        token: String,
        onText: (String) -> Unit,
        onBinary: (ByteArray) -> Unit,
        onClosedCode: (Int) -> Unit = {},
    ): WebSocketConnection {
        // 记录"本次会话请求打开过哪些通道"：诊断与测试都靠它
        // （ATR 是可选选件，未启用时这里不该出现 /WSatr1000）
        synchronized(openedPathsLog) { openedPathsLog.add(path) }
        val url = wsUrl(baseUrl, path, token)
        val conn = WebSocketConnection(
            client, url, onText, onBinary,
            onStateChange = { state ->
                if (state == WebSocketConnection.State.Connected) connectedFlags.add(path)
                else connectedFlags.remove(path)
                if (path == "/WSaudioRX") onAudioRxChange(connectedFlags.has(path))
                updateConnected()
            },
            onClosedCode = onClosedCode,
        )
        conn.connect()
        return conn
    }

    private fun handleCloseCode(code: Int) {
        if (code == 4003 && !listenOnly) {
            listenOnly = true
            connectedFlags.remove("/WSaudioTX")
            updateConnected()
            onListenOnly()
        }
    }

    @Synchronized
    private fun updateConnected() {
        val all = connectedFlags.hasAll(requiredChannels(listenOnly, spectrumPaused))
        if (all != isConnected) { isConnected = all; onConnectionChange(all) }
    }

    // ── 频谱通道的后台暂停（省带宽）──────────────────────────────

    /**
     * 后台时停掉 `/WSspectrum`：服务端满档（high）推 1701B 的瀑布帧，广播 tick 是 30 Hz，
     * 但真频谱只在硬件帧计数前进时才出帧 —— **实测 11.1 fps ≈ 151 kbps ≈ 68 MB/小时**；
     * S-meter 回退态才是每 tick 重造一帧（≈408 kbps ≈ 180 MB/小时）。
     * 旧注释写的 “~30fps ≈ 51 KB/s ≈ 180 MB/小时” 把两个态揉成了一个数字：
     * 51 KB/s 只对回退态成立，而那个态本来就不是常态。
     * 档位（AD-025）能把 high 降到 mid/low（≈38 / ≈19 kbps），但后台一律直接停：
     * 省流量最狠的一档是“根本不连”。退到后台还在收就是纯浪费流量（也白费 CPU 解析）。
     * 回前台自动重连。
     *
     * 暂停期间 `/WSspectrum` **不计入连接判据**（见 [requiredChannels]），
     * 否则 `isConnected` 会变 false → VM 的 `syncBackground()` 依赖它 →
     * 后台接收会被自己关掉，用户以为还在收音其实已经断了。
     */
    @Synchronized
    fun setSpectrumPaused(paused: Boolean) {
        if (spectrumPaused == paused) return
        spectrumPaused = paused
        if (paused) {
            spectrum?.close()
            spectrum = null
            connectedFlags.remove("/WSspectrum")
        } else {
            connectSpectrum()
        }
        updateConnected()
    }

    /** 频谱通道是否被主动暂停（后台省电），区别于"断了"。 */
    val isSpectrumPaused: Boolean get() = synchronized(this) { spectrumPaused }

    private fun connectSpectrum() {
        val b = _baseUrl ?: return
        val t = _token ?: return
        if (b.isEmpty() || t.isEmpty()) return
        spectrum = connect(b, "/WSspectrum", t, onText = { stats.onReceived(it.length) },
            onBinary = { stats.onReceived(it.size); onSpectrum(it) })
        // 连接后立即声明档位。OkHttp 会把 onOpen 之前 send() 的消息排队，所以这里不需
        // 等 Connected 状态；而服务端在收到 caps 之前一直发 high（1701B 满帧），
        // 因此晚到不会丢帧，只是前几帧贵一点。不声明就永远拿 high：手机侧一点流量也省不下来
        // （OkHttp 4.12 不提供 permessage-deflate，1701B 真的逐个走出电台）。
        sendSpectrumText(SpectrumTiers.capsJson(spectrumProfile))
    }

    /** 往频谱通道发一条文本；测试可以用 [spectrumSendOverride] 拦下（同 [dispatch]）。 */
    private fun sendSpectrumText(text: String) {
        if (spectrumSendOverride != null) { spectrumSendOverride(text); return }
        spectrum?.sendText(text)
    }

    // ── 频谱带宽档位（服务端 spectrum_profile / AD-025）───────────

    /** 当前声明的档位。默认 high = 服务端未收到 caps 时的行为，逐字节兼容。 */
    private var spectrumProfile: String = SpectrumTiers.DEFAULT

    /**
     * 换频谱带宽档位（high/mid/low）。已在传就**在同一条 socket 上补发一条 caps**，
     * 服务端下一个广播 tick 就换闸门（~33ms），不用重连。
     *
     * 重连是错的：它会惊动 [requiredChannels]，而后台暂停期间把 `/WSspectrum` 拉回来
     * 正是 [setSpectrumPaused] 明确禁止的事（用户以为在收音其实断了 / 白耗流量）。
     * 暂停时只存值，等 resume 时由 [connectSpectrum] 带上去。
     */
    @Synchronized
    fun setSpectrumProfile(name: String) {
        val normalized = SpectrumTiers.normalize(name)
        if (spectrumProfile == normalized) return
        spectrumProfile = normalized
        if (!spectrumPaused) sendSpectrumText(SpectrumTiers.capsJson(normalized))
    }

    /** 诊断与测试可读。 */
    val currentSpectrumProfile: String get() = synchronized(this) { spectrumProfile }

    private fun dispatch(cmd: String) {
        stats.onSent(cmd.length)
        if (sendOverride != null) { sendOverride(cmd); return }
        radio?.sendText(cmd)
    }
}
