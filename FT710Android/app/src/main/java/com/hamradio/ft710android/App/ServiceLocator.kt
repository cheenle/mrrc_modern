package com.hamradio.ft710android.App

import com.hamradio.ft710android.Audio.RxAudioPlayer
import com.hamradio.ft710android.Audio.TxAudioCapture
import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.Network.AuthApi
import com.hamradio.ft710android.Network.CloudApi
import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.RecordingsApi
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.Spectrum.SpectrumProcessor
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * 单例装配：构造依赖闭环（ConnectionManager 回调 → MainViewModel，MainViewModel 又持有 ConnectionManager）。
 * 用 lateinit vm + 闭包捕获解决循环引用；无 DI 框架的最小装配点。
 */
object ServiceLocator {
    lateinit var authApi: AuthApi
    lateinit var vmFactory: () -> MainViewModel

    fun assemble() {
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
        val client = AuthApi.selfSignedOkHttpClient()
        authApi = AuthApi(client)

        lateinit var vm: MainViewModel
        val rx = RxAudioPlayer()
        val tx = TxAudioCapture(FT710App.instance) { bytes -> vm.connectionManager.sendTxAudioBinary(bytes) }
        val spectrum = SpectrumProcessor()
        val recordingsApi = RecordingsApi(client)
        val cloudApi = CloudApi(client)

        val cm = ConnectionManager(
            client, scope,
            onRadioEvent = { vm.onWsEvent(it) },
            onAudioRx = { vm.onAudioRxFrame(it) },
            onSpectrum = { vm.onSpectrumFrame(it) },
            onAudioTxText = {},
            onAtrEvent = { vm.onAtrEvent(it) },
            onConnectionChange = { vm.onConnectionChange(it) },
            onListenOnly = { vm.onListenOnly() },
        )
        vm = MainViewModel(
            authApi = authApi,
            connectionManager = cm,
            rxPlayer = rx,
            txCapture = tx,
            spectrumProcessor = spectrum,
            memoryChannelsStore = null,
            recordingsApi = recordingsApi,
            cloudApi = cloudApi,
            pttManager = PTTManager(
                sendPTT = { on -> cm.sendSet("ptt", on) },
                sendTXAudioStop = { cm.sendTxAudioText("s:") },
                sendHeartbeat = { cm.sendHeartbeat() },
                startTxAudio = { tx.start() },
                stopTxAudio = { tx.stop() },
                serverTXStatus = { vm.state.txStatus },
                isCtrlConnected = { cm.isConnected },
                onStuckTX = {},
            ),
            scope = scope,
        )
        vmFactory = { vm }

        // 本地偏好 → 运行时：音量/麦克风增益/回推的 mic_gain（web cookie 同义）。
        val settings = SettingsStore(FT710App.instance)
        scope.launch { settings.afVol.collect { vm.setAfVol(it) } }
        scope.launch { settings.micVol.collect { vm.setMicVol(it) } }
        scope.launch { settings.micGain.collect { vm.setSavedMicGain(it) } }
    }
}
