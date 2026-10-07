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
    /** 顶栏（= S 表）高度；状态行折行时要传行数，顶栏会跟着长高。 */
    fun headerHeight(statusLines: Int = 1): Dp =
        ScreenFit.headerHeight(compact, screenWidthDp, statusLines).dp

    /** 状态行占几行（按**显示屏内容宽**算，不是页宽）。 */
    fun statusLines(extraChips: Int): Int = ScreenFit.statusLines(screenWidthDp, extraChips)
    val meterCellHeight: Dp get() = ScreenFit.meterCellHeight(compact).dp
    val padBtnHeight: Dp get() = ScreenFit.padBtnHeight(compact).dp
    val chipHeight: Dp get() = ScreenFit.chipHeight(compact).dp
    val sliderRowHeight: Dp get() = ScreenFit.sliderRowHeight(compact).dp
    val pttHeight: Dp get() = ScreenFit.pttHeight(compact).dp
    val auxButtonSize: Dp get() = ScreenFit.auxButtonSize(compact).dp
    val memoryColumns: Int get() = ScreenFit.memoryColumns(compact)
    val memoryRowsCount: Int get() = ScreenFit.memoryRows(compact)
    val memoryCellHeight: Dp get() = ScreenFit.memoryCellHeight(compact).dp
    val memoryCellWidth: Float get() = ScreenFit.memoryCellWidth(screenWidthDp, compact)
    val memoryInnerGap: Dp get() = ScreenFit.innerGap(compact).dp
    val memoryManageWidth: Dp get() = ScreenFit.MEMORY_MANAGE_W.dp

    /** 记忆格字号：按单元格宽度反推，保证单行不裁字（只显示频率）。 */
    fun memoryFreqFont(text: String): Float = ScreenFit.memoryFreqFontSize(text, memoryCellWidth, compact)
    val panelPadH: Dp get() = ScreenFit.panelPadH(compact).dp
    val panelPadV: Dp get() = ScreenFit.panelPadV(compact).dp
    val gap: Dp get() = ScreenFit.gap(compact).dp
    val statusItemHeight: Dp get() = ScreenFit.statusItemHeight(compact).dp
    val headerIconTap: Dp get() = ScreenFit.headerIconTap(compact).dp
    val headerIconGlyph: Dp get() = ScreenFit.headerIconGlyph(compact).dp
    val innerGap: Dp get() = ScreenFit.innerGap(compact).dp
    val bottomBarPadV: Dp get() = ScreenFit.bottomBarPadV(compact).dp
    val spectrumScale: Float get() = ScreenFit.spectrumScale(compact)

    /** 用户设的频谱高度按档位缩放（紧凑档压一档，仍然尊重设置里的滑条）。 */
    fun spectrumHeight(fftH: Int, wfH: Int): Dp = ScreenFit.spectrumHeight(compact, fftH, wfH).dp

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
