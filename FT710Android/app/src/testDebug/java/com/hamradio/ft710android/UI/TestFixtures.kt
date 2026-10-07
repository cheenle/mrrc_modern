package com.hamradio.ft710android.UI

import com.hamradio.ft710android.Network.ConnectionManager
import com.hamradio.ft710android.Network.parseWsEvent
import com.hamradio.ft710android.PTT.PTTManager
import com.hamradio.ft710android.ViewModel.MainViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import okhttp3.OkHttpClient

/**
 * UI 测试共用的 fixture：一个**喂过真实 fullState** 的 MainViewModel。
 *
 * fixture 的字段形状必须与服务端逐字一致（`scope_spans` 是键控字典、模式名是独立的
 * `mode_name`）——形状错了 `parseWsEvent` 会**静默**回退成 Unknown、整条 fullState 被丢掉，
 * 症状只是"频率显示 00.000.00"，非常难查（`ProtocolTest` 有专门的回归守护）。
 */
internal fun fixtureVm(
    scope: CoroutineScope,
    atrEnabled: Boolean = true,
    tx: Boolean = false,
    connected: Boolean = true,
): MainViewModel {
    val cm = ConnectionManager(OkHttpClient(), scope, {}, {}, {}, {}, {}, {}, sendOverride = {})
    val vm = MainViewModel(
        authApi = null, connectionManager = cm, rxPlayer = null, txCapture = null,
        spectrumProcessor = null, memoryChannelsStore = null,
        pttManager = PTTManager(
            sendPTT = {}, sendTXAudioStop = {}, sendHeartbeat = {},
            startTxAudio = {}, stopTxAudio = {}, serverTXStatus = { if (tx) 1 else 0 },
            isCtrlConnected = { true }, onStuckTX = {}, dispatcher = Dispatchers.Unconfined,
        ),
        scope = scope,
    )
    vm.onWsEvent(
        parseWsEvent(
            """{"type":"fullState",
               "data":{"vfo_a_freq":7116950,"vfo_b_freq":3500000,"mode":1,"mode_name":"USB",
                       "tx_status":${if (tx) 1 else 0},"scope_span":6,"band_name":"40m",
                       "s_meter":170,"s_unit":"S9","s_meter_dbm":12,
                       "power_watts":48.5,"alc_pct":35.0,"swr_ratio":1.4,
                       "id_amps":12.5,"vd_volts":13.8,"filter_hz":3000,
                       "attenuator":0,"preamp":1,"noise_reduction":true,"noise_blanker":false,
                       "auto_notch":false,"compressor":true,"tuner_status":0},
               "bands":[{"name":"40m","start":7000000,"end":7300000,"bsr":3,"default_freq":7100000}],
               "modes":["LSB","USB","CW-U","FM"],
               "memChannels":[{"freq":7116950,"mode":"USB","label":"M1"},
                              {"freq":14270000,"mode":"USB","label":"M2"},null,null,null,null],
               "filterTables":{"voice":[[1,300],[2,500],[3,3000]],"narrow":[[1,50]],"narrowModes":["CW-U"]},
               "radioDisplayName":"Yaesu FT-710",
               "capabilities":{"model_name":"ft710","display_name":"Yaesu FT-710","verified":true,
                 "has_atu":true,"has_auto_notch":true,"has_vd_id_meters":true,
                 "filter_model":"width_table","att_steps":[0,6,12,18],
                 "preamp_steps":["OFF","AMP1","AMP2"],"scope_type":"ft4222","audio_gain_boost":10.0,
                 "scope_spans":{"0":{"name":"1 kHz","freq":1000},
                                "6":{"name":"100 kHz","freq":100000},
                                "9":{"name":"1 MHz","freq":1000000}}},
               "atr1000Enabled":$atrEnabled}""".trimIndent(),
        ),
    )
    if (connected) vm.onConnectionChange(true)
    return vm
}
