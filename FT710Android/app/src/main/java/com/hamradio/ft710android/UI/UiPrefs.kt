package com.hamradio.ft710android.UI

import com.hamradio.ft710android.Data.SettingsStore
import com.hamradio.ft710android.Spectrum.SpectrumTiers
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine

/** MainScreen 与 SettingsScreen 共用的本地偏好快照（默认值与 SettingsStore 一致）。 */
data class UiPrefs(
    val scopeTheme: String = "jet",
    val scopeFloor: Int = 5,
    val scopeCeil: Int = 220,
    val fftHeight: Int = 40,
    val wfHeight: Int = 110,
    val afVol: Int = 128,
    val micVol: Int = 150,
    val keepScreenOn: Boolean = true,
    val backgroundRx: Boolean = true,
    /** 频谱带宽档位（服务端 AD-025）；high = 与旧服务端逐字节相同的流。 */
    val spectrumProfile: String = SpectrumTiers.DEFAULT,
)

/** 把 SettingsStore 的偏好流合成一个快照（逐层 combine，避开 5 流以上的重载限制）。 */
fun UiPrefsFlow(settings: SettingsStore): Flow<UiPrefs> {
    val base = combine(
        settings.scopeTheme, settings.scopeFloor, settings.scopeCeil,
        settings.fftHeight, settings.wfHeight,
    ) { theme, floor, ceil, fftH, wfH -> UiPrefs(theme, floor, ceil, fftH, wfH) }
    val withVol = combine(base, settings.afVol, settings.micVol, settings.scopeProfile) { p, af, mic, prof ->
        p.copy(afVol = af, micVol = mic, spectrumProfile = prof)
    }
    return combine(withVol, settings.keepScreenOn, settings.backgroundRx) { p, keepOn, bgRx ->
        p.copy(keepScreenOn = keepOn, backgroundRx = bgRx)
    }
}
