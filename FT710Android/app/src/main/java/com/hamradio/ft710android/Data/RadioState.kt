package com.hamradio.ft710android.Data

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.longOrNull

/**
 * 服务端字段镜像（radio_state.py:to_dict 的 key 逐字对应）。
 * fullState.data 与 stateUpdate.fields 共用同一套 key，apply() 统一处理。
 */
class RadioState {
    // Core
    var vfoAFreq: Long = 0
    var vfoBFreq: Long = 0
    var activeVfo: String = "A"
    var activeFreq: Long = 0
    var mode: Int = 0
    var txStatus: Int = 0
    // Meters（原始 0-255 int + 派生 float）
    var sMeter: Int = 0
    var compMeter: Int = 0
    var alcMeter: Int = 0
    var powerMeter: Int = 0
    var swrMeter: Int = 0
    var idMeter: Int = 0
    var vdMeter: Int = 0
    // Settings
    var afGain: Int = 0
    var rfGain: Int = 0
    var rfPower: Int = 0
    var filterWidth: Int = 0
    var preamp: Int = 0
    var attenuator: Int = 0
    var noiseBlanker: Boolean = false
    var noiseReduction: Boolean = false
    var autoNotch: Boolean = false
    var compressor: Boolean = false
    var compressorLevel: Int = 0
    var nrLevel: Int = 0
    var nbLevel: Int = 0
    var tunerStatus: Int = 0
    var powerOn: Boolean = false
    var squelch: Int = 0
    var micGain: Int = 0
    var split: Boolean = false
    var vox: Boolean = false
    var breakIn: Boolean = false
    // Scope
    var scopeOn: Boolean = false
    var scopeSpan: Int = 0
    var scopeSpeed: Int = 0
    var scopeMode: Int = 0
    var scopeStartFreq: Long = 0
    // Extended DSP
    var antenna: Int = 0
    var agc: Int = 0
    var dnrLevel: Int = 0
    var contourLevel: Int = 0
    // Connection
    var serialConnected: Boolean = false
    var rxAudioSilent: Boolean = false
    var lastUpdate: Double = 0.0
    // Derived（fullState 附带，UI 渲染用）
    var modeName: String = ""
    var modeDisplay: String = ""
    var bandName: String = ""
    var sMeterDbm: Double = 0.0
    var sUnit: Int = 0
    var filterHz: Int = 0
    var preampLabel: String = ""
    var attenuatorLabel: String = ""
    var isTransmitting: Boolean = false
    var powerWatts: Double = 0.0
    var swrRatio: Double = 0.0
    var vdVolts: Double = 0.0
    var idAmps: Double = 0.0
    var alcPct: Double = 0.0

    /** 应用一批字段；返回实际被应用的 key 集合。 */
    fun apply(data: Map<String, JsonElement>): Set<String> {
        val applied = mutableSetOf<String>()
        for ((key, el) in data) if (applyField(key, el)) applied.add(key)
        return applied
    }

    private fun applyField(key: String, el: JsonElement): Boolean {
        fun b(): Boolean = (el as? JsonPrimitive)?.booleanOrNull ?: false
        fun i(): Int = (el as? JsonPrimitive)?.intOrNull ?: 0
        fun l(): Long = (el as? JsonPrimitive)?.longOrNull ?: 0L
        fun d(): Double = (el as? JsonPrimitive)?.doubleOrNull ?: 0.0
        fun s(): String = (el as? JsonPrimitive)?.contentOrNull ?: ""
        when (key) {
            "vfo_a_freq" -> vfoAFreq = l()
            "vfo_b_freq" -> vfoBFreq = l()
            "active_vfo" -> activeVfo = s()
            "active_freq" -> activeFreq = l()
            "mode" -> mode = i()
            "tx_status" -> txStatus = i()
            "s_meter" -> sMeter = i()
            "comp_meter" -> compMeter = i()
            "alc_meter" -> alcMeter = i()
            "power_meter" -> powerMeter = i()
            "swr_meter" -> swrMeter = i()
            "id_meter" -> idMeter = i()
            "vd_meter" -> vdMeter = i()
            "af_gain" -> afGain = i()
            "rf_gain" -> rfGain = i()
            "rf_power" -> rfPower = i()
            "filter_width" -> filterWidth = i()
            "preamp" -> preamp = i()
            "attenuator" -> attenuator = i()
            "noise_blanker" -> noiseBlanker = b()
            "noise_reduction" -> noiseReduction = b()
            "auto_notch" -> autoNotch = b()
            "compressor" -> compressor = b()
            "compressor_level" -> compressorLevel = i()
            "nr_level" -> nrLevel = i()
            "nb_level" -> nbLevel = i()
            "tuner_status" -> tunerStatus = i()
            "power_on" -> powerOn = b()
            "squelch" -> squelch = i()
            "mic_gain" -> micGain = i()
            "split" -> split = b()
            "vox" -> vox = b()
            "break_in" -> breakIn = b()
            "scope_on" -> scopeOn = b()
            "scope_span" -> scopeSpan = i()
            "scope_speed" -> scopeSpeed = i()
            "scope_mode" -> scopeMode = i()
            "scope_start_freq" -> scopeStartFreq = l()
            "antenna" -> antenna = i()
            "agc" -> agc = i()
            "dnr_level" -> dnrLevel = i()
            "contour_level" -> contourLevel = i()
            "serial_connected" -> serialConnected = b()
            "rx_audio_silent" -> rxAudioSilent = b()
            "last_update" -> lastUpdate = d()
            "mode_name" -> modeName = s()
            "mode_display" -> modeDisplay = s()
            "band_name" -> bandName = s()
            "s_meter_dbm" -> sMeterDbm = d()
            "s_unit" -> sUnit = i()
            "filter_hz" -> filterHz = i()
            "preamp_label" -> preampLabel = s()
            "attenuator_label" -> attenuatorLabel = s()
            "is_transmitting" -> isTransmitting = b()
            "power_watts" -> powerWatts = d()
            "swr_ratio" -> swrRatio = d()
            "vd_volts" -> vdVolts = d()
            "id_amps" -> idAmps = d()
            "alc_pct" -> alcPct = d()
            else -> return false
        }
        return true
    }

    /** 活跃 VFO 频率。 */
    val activeFrequency: Long get() = if (activeVfo == "B") vfoBFreq else vfoAFreq
}
