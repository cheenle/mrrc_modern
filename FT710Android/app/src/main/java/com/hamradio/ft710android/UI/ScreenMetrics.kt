package com.hamradio.ft710android.UI

import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.hamradio.ft710android.Data.ScreenFit

/**
 * 当前屏幕档位的尺寸下发。
 *
 * 数值全部来自 [ScreenFit]（单一数据源，`ScreenFitTest` 用真机档位验证"一屏放得下"），
 * Composable 里不要就地写死 dp，否则预算测试会和真实布局脱节。
 */
class ScreenMetrics(val compact: Boolean, val screenWidthDp: Int, val screenHeightDp: Int) {
    val meterWidth: Dp get() = ScreenFit.meterWidth(screenWidthDp).dp
    val headerHeight: Dp get() = ScreenFit.headerHeight(compact, screenWidthDp).dp
    val meterCellHeight: Dp get() = ScreenFit.meterCellHeight(compact).dp
    val padBtnHeight: Dp get() = ScreenFit.padBtnHeight(compact).dp
    val chipHeight: Dp get() = ScreenFit.chipHeight(compact).dp
    val sliderRowHeight: Dp get() = ScreenFit.sliderRowHeight(compact).dp
    val pttHeight: Dp get() = ScreenFit.pttHeight(compact).dp
    val auxButtonSize: Dp get() = ScreenFit.auxButtonSize(compact).dp
    val memoryColumns: Int get() = ScreenFit.memoryColumns(compact)
    val memoryRowsCount: Int get() = ScreenFit.memoryRows(compact)
    val memoryCellHeight: Dp get() = ScreenFit.memoryCellHeight(compact).dp
    val panelPadH: Dp get() = ScreenFit.panelPadH(compact).dp
    val panelPadV: Dp get() = ScreenFit.panelPadV(compact).dp
    val showSectionLabels: Boolean get() = ScreenFit.showSectionLabels(compact)
    val gap: Dp get() = ScreenFit.gap(compact).dp
    val statusItemHeight: Dp get() = ScreenFit.STATUS_ITEM_H.dp
    val innerGap: Dp get() = ScreenFit.innerGap(compact).dp
    val bottomBarPadV: Dp get() = ScreenFit.bottomBarPadV(compact).dp
    val spectrumScale: Float get() = ScreenFit.spectrumScale(compact)

    /** 用户设的频谱高度按档位缩放（紧凑档压一档，仍然尊重设置里的滑条）。 */
    fun spectrumHeight(fftH: Int, wfH: Int): Dp =
        (ScreenFit.spectrumHeight(compact, fftH, wfH) - ScreenFit.PAGE_PAD_V).dp

    /**
     * 屏幕余量（分给频谱，封顶 [ScreenFit.SPECTRUM_BONUS_CAP_DP]）。
     * 入参必须和 [ScreenFit.budget] 一致，否则预算测试就和真实布局脱节。
     */
    fun spectrumBonus(fftH: Int, wfH: Int, hasAtr: Boolean, hasVdId: Boolean, bottomBar: Boolean, extraChips: Int): Int =
        ScreenFit.budget(
            screenWidthDp, screenHeightDp, fftH, wfH, hasAtr, hasVdId, bottomBar, extraChips,
        ).spectrumBonus
}

val LocalScreenMetrics = staticCompositionLocalOf { ScreenMetrics(false, 412, 892) }

/** 按当前 Configuration 建立档位并下发（在根部包一次即可）。 */
@Composable
fun ProvideScreenMetrics(content: @Composable () -> Unit) {
    val cfg = LocalConfiguration.current
    // screenHeightDp 已扣除状态栏/导航栏，正好是布局可用高度
    val metrics = ScreenMetrics(
        compact = ScreenFit.isCompact(cfg.screenHeightDp),
        screenWidthDp = cfg.screenWidthDp,
        screenHeightDp = cfg.screenHeightDp,
    )
    CompositionLocalProvider(LocalScreenMetrics provides metrics, content = content)
}
